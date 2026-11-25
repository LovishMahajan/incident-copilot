"""Artifact 9.4, 13.5 (M5, ADR-75): Datadog, read-only, for every source tool.

Documented Datadog APIs, on the configured site (`datadoghq.com`, `datadoghq.eu`,
`us3.datadoghq.com`, `us5.datadoghq.com`, `ap1.datadoghq.com`, `ap2.datadoghq.com`, `ddog-gov.com`):

- `get_metric`: Query timeseries points, `GET https://api.{site}/api/v1/query` (API v1,
  https://docs.datadoghq.com/api/latest/metrics/#query-timeseries-points);
- `get_logs`: Search logs, `POST https://api.{site}/api/v2/logs/events/search` (API v2,
  https://docs.datadoghq.com/api/latest/logs/#search-logs);
- `get_changes`: Get a list of events, `GET https://api.{site}/api/v1/events` (API v1,
  https://docs.datadoghq.com/api/latest/events/#get-a-list-of-events), the deployment events
  the service's pipeline sends, tagged `service:`, `env:` and `version:` (and, if it sends one,
  `previous_version:`). An event is a change with its version and time; Datadog doesn't hold the
  files or configuration keys, so those stay empty and the fact rules have less to go on.
- `get_alert`: Get all monitors, `GET https://api.{site}/api/v1/monitor` with `monitor_tags` and
  `group_states=alert,warn` (API v1, https://docs.datadoghq.com/api/latest/monitors/#get-all-monitors):
  monitors in `Alert` (firing) or `Warn` (warning) tagged `service:` with the service, fired at
  their groups' latest `last_triggered_ts`. A monitor holds its query and critical threshold,
  not the value that triggered it, so `value` and `breach_started_at` are empty.
- `get_traces`: Search spans, `POST https://api.{site}/api/v2/spans/events/search` (API v2,
  https://docs.datadoghq.com/api/latest/spans/#search-spans): the service's error spans in the
  window (`status:error` added to `spans_query`), grouped by trace ID, at most `max_traces`
  traces. Each trace holds this service's failing spans, not the whole trace; `error.type` and
  `error.message` become the span's exception.
- `get_error_groups`: Search error tracking issues, `POST
  https://api.{site}/api/v2/error-tracking/issues/search?include=issue` (API v2,
  https://docs.datadoghq.com/api/latest/error-tracking/#search-error-tracking-issues). Native:
  Error Tracking does the grouping. An issue's count is its errors in the window, its first and
  last sighting are the issue's own, its versions the first and last seen, its frame the file
  it names without a line.
- `get_dependency_health`: each configured dependency's `error_rate` and `latency_p95` metric
  query (v1 query, above) over `dependency_lookback_s` up to the call's `at`, last point against
  its limits. Derived: Datadog's service map has no documented health API.

The API key and the application key are by reference (SecretRef), sent as `DD-API-KEY` and
`DD-APPLICATION-KEY`; the application key should be scoped read-only (`timeseries_query`,
`logs_read_data`, `events_read`, `monitors_read`, `apm_read`, `error_tracking_read`). Queries are
configuration with `$service`, `$environment`, for logs `$levels` (`error OR warn`, or the one
level asked for) and for dependencies `$dependency`; a value is put in only if it is a plain tag
value (letters, digits, `_ - . : /`), else the call is a gap, so a value can't change a query.
Bounds as every source's (Artifact 9.4): the call's window, `limit` rows (MAX_ALERTS monitors,
`max_traces` traces), at most MAX_RESPONSE_BYTES read, `timeout_s` per request; the collector
retries and holds the breaker. A series, log, monitor, span or issue tagged with another service
or environment is dropped (FR-44). Tested against recorded responses only
(tests/unit/test_vendor_sources.py and test_vendor_gaps.py).
"""

from __future__ import annotations

import json
import re
import urllib.request
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from pydantic import BaseModel

from aisre.evidence.sources import (
    DEFAULT_TIMEOUT_S,
    MAX_ALERTS,
    MAX_RESPONSE_BYTES,
    MAX_TRACES,
    Record,
    SecretRef,
    SourceResult,
    SourceUnavailable,
    dependency_record,
    dependency_specs,
    http_json,
    iso_ms,
    log_record,
    metric_queries,
    metric_records,
    substitute,
    utc_text,
    wanted_levels,
)

SITES = (
    "datadoghq.com",
    "datadoghq.eu",
    "us3.datadoghq.com",
    "us5.datadoghq.com",
    "ap1.datadoghq.com",
    "ap2.datadoghq.com",
    "ddog-gov.com",
)
_TAG_VALUE = re.compile(r"^[A-Za-z0-9_.:/-]{1,200}$")
_DD_LEVEL = {"ERROR": "error", "WARNING": "warn", "WARN": "warn", "INFO": "info"}


def quote_tag(value: str) -> str:
    if not _TAG_VALUE.match(value):
        raise SourceUnavailable("a value that is not a plain tag value can't go in a Datadog query")
    return value


def _tags(tags: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for t in tags or []:
        k, sep, v = str(t).partition(":")
        if sep:
            out.setdefault(k, v)
    return out


class DatadogSource:
    """`get_metric`, `get_logs` and `get_changes` from one Datadog organisation."""

    def __init__(
        self,
        api_key: Any,
        app_key: Any,
        site: str = "datadoghq.com",
        metrics: Mapping[str, Any] | None = None,
        logs_query: str | None = None,
        events_tags: str = "service:$service,env:$environment",
        events_sources: str | None = None,
        version_tag: str = "version",
        monitor_tags: str = "service:$service,env:$environment",
        spans_query: str = "service:$service env:$environment",
        max_traces: int = MAX_TRACES,
        error_tracking_query: str = "service:$service env:$environment",
        error_tracking_track: str = "trace",
        dependencies: Mapping[str, Any] | None = None,
        dependency_lookback_s: int = 300,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_bytes: int = MAX_RESPONSE_BYTES,
        name: str = "datadog",
    ) -> None:
        if site not in SITES:
            raise ValueError(f"{name}: site must be one of {', '.join(SITES)}")
        self.name, self.url = name, f"https://api.{site}"
        self.api_key = SecretRef(api_key, f"{name} api_key")
        self.app_key = SecretRef(app_key, f"{name} app_key")
        self.credentials = (self.api_key, self.app_key)
        self.metrics = metric_queries(metrics or {}, name)
        if logs_query is not None and "$service" not in logs_query:
            raise ValueError(f"{name}: logs_query must select the service with $service")
        self.logs_query, self.events_tags, self.events_sources = (
            logs_query,
            events_tags,
            events_sources,
        )
        self.version_tag, self.timeout_s, self.max_bytes = version_tag, timeout_s, max_bytes
        for what, q in (
            ("monitor_tags", monitor_tags),
            ("spans_query", spans_query),
            ("error_tracking_query", error_tracking_query),
        ):
            if not isinstance(q, str) or "$service" not in q:
                raise ValueError(f"{name}: {what} must select the service with $service")
        if error_tracking_track not in ("trace", "logs"):
            raise ValueError(f"{name}: error_tracking_track is trace or logs")
        if not 1 <= max_traces <= 20:
            raise ValueError(f"{name}: max_traces is 1 to 20")
        if not 60 <= dependency_lookback_s <= 3600:
            raise ValueError(f"{name}: dependency_lookback_s is 60 to 3600")
        self.monitor_tags, self.spans_query, self.max_traces = (
            monitor_tags,
            spans_query,
            max_traces,
        )
        self.error_tracking_query, self.error_tracking_track = (
            error_tracking_query,
            error_tracking_track,
        )
        self.dependencies = (
            dependency_specs(dependencies, name) if dependencies is not None else None
        )
        self.dependency_lookback_s = dependency_lookback_s

    def _call(self, path: str, query: Mapping[str, str], body: Any = None) -> tuple[Any, str]:
        link = f"{self.url}{path}" + (f"?{urlencode(query)}" if query else "")
        req = urllib.request.Request(
            link,
            data=None if body is None else json.dumps(body).encode(),
            method="GET" if body is None else "POST",  # the logs search is a POST that reads
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "DD-API-KEY": self.api_key.value(),
                "DD-APPLICATION-KEY": self.app_key.value(),
            },
        )
        found = http_json(req, self.name, self.timeout_s, self.max_bytes)
        if isinstance(found, list):  # the monitors list is a bare array
            return found, link
        if not isinstance(found, dict) or found.get("status") in ("error", "warn"):
            raise SourceUnavailable(f"{self.name}: the query failed")
        return found, link

    def fetch(self, tool: str, params: BaseModel, environment: str) -> SourceResult:
        p: Any = params
        if tool == "get_metric":
            return self._metric(p, environment)
        if tool == "get_logs" and self.logs_query is not None:
            return self._logs(p, environment)
        if tool == "get_changes":
            return self._changes(p, environment)
        if tool == "get_alert":
            return self._alerts(p, environment)
        if tool == "get_traces":
            return self._traces(p, environment)
        if tool == "get_error_groups":
            return self._error_groups(p, environment)
        if tool == "get_dependency_health" and self.dependencies is not None:
            return self._dependencies(p, environment)
        raise SourceUnavailable(f"{self.name} has no {tool}")

    def _scoped(self, tags: Mapping[str, str], service: str, environment: str) -> bool:
        return tags.get("service", service) == service and tags.get("env", environment) == (
            environment
        )

    def _series(
        self, query: str, p: Any, environment: str, by_version: bool
    ) -> tuple[dict[str, dict[str, float | None]], str]:
        q = substitute(query, quote_tag, service=p.service, environment=environment)
        found, link = self._call(
            "/api/v1/query",
            {"from": str(int(p.start.timestamp())), "to": str(int(p.end.timestamp())), "query": q},
        )
        out: dict[str, dict[str, float | None]] = {}
        for series in found.get("series") or []:
            tags = _tags(series.get("tag_set"))
            if not self._scoped(tags, p.service, environment):
                continue  # FR-44
            key = tags.get(self.version_tag, "") if by_version else ""
            if by_version and not key:
                continue
            points = out.setdefault(key, {})
            for stamp, value in series.get("pointlist") or []:
                at = datetime.fromtimestamp(float(stamp) / 1000, UTC)
                if p.start <= at <= p.end:
                    points[utc_text(at)] = None if value is None else float(value)
        return out, link

    def _metric(self, p: Any, environment: str) -> SourceResult:
        spec = self.metrics.get(p.metric)
        if spec is None:
            raise SourceUnavailable(f"{self.name}: no query configured for {p.metric}")
        by_version = p.metric == "error_rate_by_version"
        value, link = self._series(spec["value"], p, environment, by_version)
        requests = (
            self._series(spec["requests"], p, environment, by_version)[0]
            if "requests" in spec
            else {}
        )
        records = metric_records(p.metric, value, requests)
        return SourceResult(
            f"{self.name}:metric", link, records[: p.limit], partial=len(records) > p.limit
        )

    def _logs(self, p: Any, environment: str) -> SourceResult:
        levels = wanted_levels(p.level)
        dd_levels = " OR ".join(sorted({_DD_LEVEL[lv] for lv in levels}))
        assert self.logs_query is not None
        q = substitute(
            self.logs_query.replace("$levels", f"({dd_levels})"),  # ours, not a caller's value
            quote_tag,
            service=p.service,
            environment=environment,
        )
        if p.contains:
            q += " " + json.dumps(p.contains)  # a quoted phrase in Datadog's search syntax
        found, link = self._call(
            "/api/v2/logs/events/search",
            {},
            {
                "filter": {"query": q, "from": utc_text(p.start), "to": utc_text(p.end)},
                "sort": "timestamp",
                "page": {"limit": p.limit},
            },
        )
        records: list[Record] = []
        for item in found.get("data") or []:
            a = item.get("attributes") or {}
            tags = _tags(a.get("tags"))
            fields = {
                **tags,
                **(a.get("attributes") or {}),
                "service": a.get("service") or tags.get("service"),
                "env": tags.get("env"),
                "host": a.get("host"),
                "message": a.get("message"),
                "status": a.get("status"),
            }
            if not self._scoped({k: v for k, v in fields.items() if v}, p.service, environment):
                continue
            ts = a.get("timestamp")
            if not ts:
                continue
            rec = log_record(fields, utc_text(datetime.fromisoformat(ts)), str(a.get("message")))
            if rec["level"] in levels:
                records.append(rec)
        more = bool(((found.get("meta") or {}).get("page") or {}).get("after"))
        return SourceResult(
            f"{self.name}:logs", link, records[: p.limit], partial=more or len(records) > p.limit
        )

    def _changes(self, p: Any, environment: str) -> SourceResult:
        query = {
            "start": str(int(p.start.timestamp())),
            "end": str(int(p.end.timestamp())),
            "tags": substitute(
                self.events_tags, quote_tag, service=p.service, environment=environment
            ),
            "unaggregated": "true",
        }
        if self.events_sources:
            query["sources"] = self.events_sources
        found, link = self._call("/api/v1/events", query)
        records: list[Record] = []
        for e in found.get("events") or []:
            tags = _tags(e.get("tags"))
            if not self._scoped(tags, p.service, environment) or "service" not in tags:
                continue  # an event not tagged with the service is not its change
            at = datetime.fromtimestamp(int(e.get("date_happened") or 0), UTC)
            if not p.start <= at <= p.end:
                continue
            records.append(
                {
                    "change_id": f"{self.name} event {e.get('id')}",
                    "version": tags.get(self.version_tag),
                    "previous_version": tags.get("previous_version"),
                    "code_changed": None,
                    "files": [],
                    "commits": [],
                    "config_changes": [],
                    "config_revision": None,
                    "stages": [{"step": str(e.get("title") or "event"), "at": utc_text(at)}],
                    "rollout": [],
                    "diff": None,
                }
            )
        records.sort(key=lambda r: str(r["stages"][0]["at"]))
        return SourceResult(
            f"{self.name}:changes", link, records[: p.limit], partial=len(records) > p.limit
        )

    # -- get_alert: monitors ------------------------------------------------------------------
    def _alerts(self, p: Any, environment: str) -> SourceResult:
        """Triggered monitors (Alert or Warn) tagged with the service. Datadog's monitor list
        holds the state and when each group last triggered, not the value that triggered it."""
        found, link = self._call(
            "/api/v1/monitor",
            {
                "monitor_tags": substitute(
                    self.monitor_tags, quote_tag, service=p.service, environment=environment
                ),
                "group_states": "alert,warn",
                "page": "0",
                "page_size": "100",
            },
        )
        if not isinstance(found, list):
            raise SourceUnavailable(f"{self.name}: not a list of monitors")
        records: list[Record] = []
        for m in found:
            tags = _tags(m.get("tags"))
            if tags.get("service") != p.service or not self._scoped(tags, p.service, environment):
                continue  # a monitor not tagged with the service is not its alert (FR-44)
            if m.get("overall_state") not in _MONITOR_STATE:
                continue
            if p.alert_id is not None and p.alert_id not in (str(m.get("id")), m.get("name")):
                continue
            groups = ((m.get("state") or {}).get("groups") or {}).values()
            triggered = [
                t
                for g in groups
                if g.get("status") in _MONITOR_STATE
                and (t := _epoch(g.get("last_triggered_ts"))) is not None
            ]
            fired = max(triggered) if triggered else None
            if fired is None and m.get("overall_state_modified"):
                fired = utc_text(datetime.fromisoformat(m["overall_state_modified"]))
            records.append(
                {
                    "alert_name": m.get("name"),
                    "alert_id": str(m.get("id")),
                    "state": _MONITOR_STATE[m["overall_state"]],
                    "service": p.service,
                    "environment": tags.get("env", environment),
                    "rule": m.get("query"),
                    "value": None,
                    "threshold": ((m.get("options") or {}).get("thresholds") or {}).get("critical"),
                    "breach_started_at": None,
                    "fired_at": fired,
                }
            )
        records.sort(key=lambda r: str(r["fired_at"] or ""), reverse=True)
        return SourceResult(
            f"{self.name}:alert", link, records[:MAX_ALERTS], partial=len(records) > MAX_ALERTS
        )

    # -- get_traces: APM spans search ---------------------------------------------------------
    def _traces(self, p: Any, environment: str) -> SourceResult:
        """The service's error spans in the window, grouped into traces: each trace holds the
        failing spans of this service (what the fact rules read), not the whole trace."""
        q = substitute(self.spans_query, quote_tag, service=p.service, environment=environment)
        found, link = self._call(
            "/api/v2/spans/events/search",
            {},
            {
                "data": {
                    "type": "search_request",
                    "attributes": {
                        "filter": {
                            "query": f"{q} status:error",  # ours: only failing spans
                            "from": utc_text(p.start),
                            "to": utc_text(p.end),
                        },
                        "options": {"timezone": "UTC"},
                        "page": {"limit": p.limit},
                        "sort": "timestamp",
                    },
                }
            },
        )
        by_trace: dict[str, list[Record]] = {}
        for item in found.get("data") or []:
            a = item.get("attributes") or {}
            tags = _tags(a.get("tags"))
            if a.get("service") != p.service or (a.get("env") or tags.get("env", environment)) != (
                environment
            ):
                continue  # FR-44
            start, end = a.get("start_timestamp"), a.get("end_timestamp")
            if not start or not p.start <= datetime.fromisoformat(start) <= p.end:
                continue
            attrs = {**_flat(a.get("custom")), **_flat(a.get("attributes"))}
            exc_type, exc_msg = attrs.get("error.type"), attrs.get("error.message")
            by_trace.setdefault(str(a.get("trace_id")), []).append(
                {
                    "name": a.get("resource_name"),
                    "status": "ERROR",
                    "start": iso_ms(datetime.fromisoformat(start)),
                    "end": iso_ms(datetime.fromisoformat(end)) if end else None,
                    "attributes": attrs,
                    "exceptions": (
                        [{"type": exc_type, "message": exc_msg}] if exc_type or exc_msg else []
                    ),
                }
            )
        if p.path:
            by_trace = {
                t: spans
                for t, spans in by_trace.items()
                if any(p.path in str(s["name"]) for s in spans)
            }
        records: list[Record] = [
            {"trace_id": t, "spans": sorted(spans, key=lambda s: str(s["start"]))}
            for t, spans in by_trace.items()
        ]
        meta = found.get("meta") or {}
        more = bool((meta.get("page") or {}).get("after")) or meta.get("status") == "timeout"
        return SourceResult(
            f"{self.name}:traces",
            link,
            records[: self.max_traces],
            partial=more or bool(meta.get("warnings")) or len(records) > self.max_traces,
        )

    # -- get_error_groups: Error Tracking issues ----------------------------------------------
    def _error_groups(self, p: Any, environment: str) -> SourceResult:
        """Error Tracking's issues with errors in the window, native: Datadog groups them.
        An issue names its file but not a line, and its first and last versions only."""
        q = substitute(
            self.error_tracking_query, quote_tag, service=p.service, environment=environment
        )
        found, link = self._call(
            "/api/v2/error-tracking/issues/search",
            {"include": "issue"},
            {
                "data": {
                    "type": "search_request",
                    "attributes": {
                        "query": q,
                        "from": int(p.start.timestamp() * 1000),
                        "to": int(p.end.timestamp() * 1000),
                        "track": self.error_tracking_track,
                    },
                }
            },
        )
        issues = {
            i.get("id"): i.get("attributes") or {}
            for i in found.get("included") or []
            if i.get("type") == "issue"
        }
        records: list[Record] = []
        for result in found.get("data") or []:
            ref = ((result.get("relationships") or {}).get("issue") or {}).get("data") or {}
            issue = issues.get(ref.get("id"))
            if issue is None or issue.get("service") != p.service:
                continue  # FR-44: only this service's issues
            versions = [
                v for v in (issue.get("first_seen_version"), issue.get("last_seen_version")) if v
            ]
            if p.release and p.release not in versions:
                continue
            records.append(
                {
                    "type": issue.get("error_type"),
                    "message": issue.get("error_message"),
                    "count": (result.get("attributes") or {}).get("total_count"),
                    "first_seen": _epoch(issue.get("first_seen")),
                    "last_seen": _epoch(issue.get("last_seen")),
                    "versions": sorted(set(versions)),
                    "frames": [[issue["file_path"], ""]] if issue.get("file_path") else [],
                }
            )
        return SourceResult(
            f"{self.name}:error_groups",
            link,
            records[: p.limit],
            partial=len(records) > p.limit,
        )

    # -- get_dependency_health: configured metrics per dependency ------------------------------
    def _dependencies(self, p: Any, environment: str) -> SourceResult:
        assert self.dependencies is not None
        start = p.at - timedelta(seconds=self.dependency_lookback_s)
        records, link = [], ""
        for dep, spec in self.dependencies.items():
            measured: dict[str, float | None] = {}
            for measure, query in spec["queries"].items():
                q = substitute(
                    query, quote_tag, service=p.service, environment=environment, dependency=dep
                )
                found, link = self._call(
                    "/api/v1/query",
                    {
                        "from": str(int(start.timestamp())),
                        "to": str(int(p.at.timestamp())),
                        "query": q,
                    },
                )
                series = [
                    s
                    for s in found.get("series") or []
                    if self._scoped(_tags(s.get("tag_set")), p.service, environment)
                ]
                if len(series) > 1:
                    raise SourceUnavailable(
                        f"{self.name}: {dep} gave {len(series)} series; aggregate it"
                    )
                points = [v for _, v in (series[0].get("pointlist") or [])] if series else []
                last = next((v for v in reversed(points) if v is not None), None)
                measured[measure] = None if last is None else float(last)
            how = (
                f"last point in the {self.dependency_lookback_s} s to {utc_text(p.at)}, {self.name}"
            )
            records.append(dependency_record(dep, spec, measured, p.at, how))
        unknown = any(r["status"] == "unknown" for r in records)
        return SourceResult(f"{self.name}:dependencies", link, records, partial=unknown)


_MONITOR_STATE = {"Alert": "firing", "Warn": "warning"}


def _epoch(stamp: Any) -> str | None:
    """A Datadog epoch time, seconds or milliseconds, as UTC text."""
    if stamp in (None, ""):
        return None
    t = float(stamp)
    return utc_text(datetime.fromtimestamp(t / 1000 if t > 1e11 else t, UTC))


def _flat(value: Any, prefix: str = "") -> dict[str, Any]:
    """Nested span attributes as dotted keys (`error.type`), as OpenTelemetry names them."""
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, Any] = {}
    for k, v in value.items():
        key = f"{prefix}{k}"
        if isinstance(v, Mapping):
            out.update(_flat(v, key + "."))
        else:
            out[key] = v
    return out
