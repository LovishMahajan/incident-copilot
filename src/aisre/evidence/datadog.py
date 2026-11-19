"""Artifact 9.4, 13.5 (M5, ADR-75): Datadog for metrics, logs and change events, read-only.

Three documented Datadog APIs, on the configured site (`datadoghq.com`, `datadoghq.eu`,
`us3.datadoghq.com`, `us5.datadoghq.com`, `ap1.datadoghq.com`, `ddog-gov.com`):

- `get_metric`: Query timeseries points, `GET https://api.{site}/api/v1/query` (API v1,
  https://docs.datadoghq.com/api/latest/metrics/#query-timeseries-points);
- `get_logs`: Search logs, `POST https://api.{site}/api/v2/logs/events/search` (API v2,
  https://docs.datadoghq.com/api/latest/logs/#search-logs);
- `get_changes`: Get a list of events, `GET https://api.{site}/api/v1/events` (API v1,
  https://docs.datadoghq.com/api/latest/events/#get-a-list-of-events), the deployment events
  the service's pipeline sends, tagged `service:`, `env:` and `version:` (and, if it sends one,
  `previous_version:`). An event is a change with its version and time; Datadog doesn't hold the
  files or configuration keys, so those stay empty and the fact rules have less to go on.

Alerts, traces, error groups and dependency health raise SourceUnavailable: a gap (ADR-54).

The API key and the application key are by reference (SecretRef), sent as `DD-API-KEY` and
`DD-APPLICATION-KEY`; the application key should be scoped read-only (`timeseries_query`,
`logs_read_data`, `events_read`). Queries are configuration with `$service`, `$environment` and,
for logs, `$levels` (`error OR warn`, or the one level asked for); a value is put in only if it is
a plain tag value (letters, digits, `_ - . : /`), else the call is a gap, so a value can't change
a query. Bounds as every source's (Artifact 9.4): the call's window, `limit` rows, at most
MAX_RESPONSE_BYTES read, `timeout_s` per call; the collector retries and holds the breaker. A
series or log tagged with another service or environment is dropped (FR-44). Tested against
recorded responses only (tests/unit/test_vendor_sources.py).
"""

from __future__ import annotations

import json
import re
import urllib.request
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from pydantic import BaseModel

from aisre.evidence.sources import (
    DEFAULT_TIMEOUT_S,
    MAX_RESPONSE_BYTES,
    Record,
    SecretRef,
    SourceResult,
    SourceUnavailable,
    http_json,
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
