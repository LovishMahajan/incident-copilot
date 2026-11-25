"""Artifact 9.4, 13.5 (M5, ADR-75): the Prometheus stack, read-only. Prometheus for metrics and
dependency health, Loki for logs and error groups, Alertmanager for alerts, Tempo for traces.

- Prometheus answers `get_metric` through the HTTP API v1 range query, `GET /api/v1/query_range`
  (https://prometheus.io/docs/prometheus/latest/querying/api/#range-queries, API v1, as served
  by Prometheus 2.x and 3.x), and `get_dependency_health` through the instant query, `GET
  /api/v1/query` at the call's `at` (same page, #instant-queries): each configured dependency's
  `error_rate` and `latency_p95` query, against its limits (sources.dependency_record). Derived:
  Prometheus has no notion of a dependency, only the series its owners name.
- Loki answers `get_logs` through `GET /loki/api/v1/query_range`
  (https://grafana.com/docs/loki/latest/reference/loki-http-api/#query-logs-within-a-range-of-time,
  Loki 2.x and 3.x), and `get_error_groups`, derived, through the same endpoint with a LogQL
  metric query (https://grafana.com/docs/loki/latest/query/metric_queries/):
  `sum by (<type>, <message>, <version>) (count_over_time(<selector> [<step>]))`. Counts hold no
  stack, so its groups have no frames; first and last sighting are to the step.
- Alertmanager answers `get_alert` through API v2, `GET /api/v2/alerts` with `filter` label
  matchers (https://github.com/prometheus/alertmanager/blob/main/api/v2/openapi.yaml, API v2,
  Alertmanager 0.16 and later): active, not silenced or inhibited, labelled with the service and
  environment. The value and threshold come from annotations if the rule sets them; Alertmanager
  doesn't hold when the condition began, so `breach_started_at` is empty.
- Tempo answers `get_traces` through TraceQL search, `GET /api/search`, then each trace by `GET
  /api/traces/<traceID>` in OpenTelemetry JSON (https://grafana.com/docs/tempo/latest/api_docs/,
  Tempo 2.x), up to `max_traces` (5) traces a call.

None holds changes; a call for a tool an adapter isn't configured for raises SourceUnavailable,
which is a gap (ADR-54).

The queries are configuration, written by the service's owners, with `$service`, `$environment`
and, for logs, `$levels` (a regular expression of the levels asked for), for dependencies
`$dependency`, substituted as quoted string contents: the template supplies the quotes, as in
`service="$service"`. Each metric has a `value` query and, for `http_5xx_rate` and
`error_rate_by_version`, an optional `requests` query; `error_rate_by_version` groups by the
label `version_label` names. A get_logs `contains` is added as a line filter, `|= "..."`.

Bounds (Artifact 9.4): the window is the call's (the ToolGate already capped it), one point a
`step_s`, at most `limit` rows (MAX_ALERTS alerts, `max_traces` traces), at most
MAX_RESPONSE_BYTES read, `timeout_s` per request; retries and the circuit breaker are the
collector's, as for every source. A series, stream, alert or span labelled with another service
or environment is dropped (FR-44). Credentials, a bearer token or a basic-auth password, are by
reference (SecretRef). Tested against recorded responses only (tests/unit/test_vendor_sources.py
and test_vendor_gaps.py).
"""

from __future__ import annotations

import base64
import json
import math
import re
import urllib.request
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

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
    error_group_records,
    http_json,
    iso_ms,
    log_record,
    metric_queries,
    metric_records,
    optional_ref,
    parse_time,
    substitute,
    utc_text,
    wanted_levels,
)


def quote_string(value: str) -> str:
    """The contents of a PromQL or LogQL double-quoted string (Go escapes, which JSON's are)."""
    return json.dumps(value, ensure_ascii=False)[1:-1]


class _Http:
    def __init__(
        self,
        url: str,
        name: str,
        timeout_s: float,
        max_bytes: int,
        token: Any,
        username: str | None,
        tenant: str | None,
    ) -> None:
        if not url.startswith(("https://", "http://")):
            raise ValueError(f"{name}: url must be http(s)")
        self.url, self.name = url.rstrip("/"), name
        self.timeout_s, self.max_bytes = timeout_s, max_bytes
        self.token: SecretRef | None = optional_ref(token, f"{name} token")
        self.username, self.tenant = username, tenant
        self.credentials = tuple(r for r in (self.token,) if r is not None)

    def get(self, path: str, query: Mapping[str, str]) -> tuple[Any, str]:
        """A Prometheus or Loki API answer: `{"status": "success", "data": ...}`."""
        body, link = self.get_json(path, query)
        if not isinstance(body, dict) or body.get("status") != "success":
            raise SourceUnavailable(f"{self.name}: the query failed")
        return body.get("data") or {}, link

    def get_json(
        self, path: str, query: Mapping[str, str] | list[tuple[str, str]]
    ) -> tuple[Any, str]:
        link = f"{self.url}{path}" + (f"?{urlencode(query)}" if query else "")
        headers = {"Accept": "application/json"}
        if self.token is not None:
            secret = self.token.value()
            headers["Authorization"] = (
                "Basic " + base64.b64encode(f"{self.username}:{secret}".encode()).decode()
                if self.username
                else f"Bearer {secret}"
            )
        if self.tenant:
            headers["X-Scope-OrgID"] = self.tenant
        body = http_json(
            urllib.request.Request(link, headers=headers), self.name, self.timeout_s, self.max_bytes
        )
        return body, link


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


class PrometheusSource:
    """`get_metric` from Prometheus's range query API, and `get_dependency_health` from instant
    queries of each configured dependency's error rate and latency."""

    def __init__(
        self,
        url: str,
        metrics: Mapping[str, Any] | None = None,
        token: Any = None,
        username: str | None = None,
        tenant: str | None = None,
        version_label: str = "version",
        service_label: str = "service",
        environment_label: str = "environment",
        step_s: int = 60,
        dependencies: Mapping[str, Any] | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_bytes: int = MAX_RESPONSE_BYTES,
        name: str = "prometheus",
    ) -> None:
        self.name = name
        self.http = _Http(url, name, timeout_s, max_bytes, token, username, tenant)
        self.credentials = self.http.credentials
        self.metrics = metric_queries(metrics or {}, name)
        self.dependencies = (
            dependency_specs(dependencies, name) if dependencies is not None else None
        )
        self.version_label, self.step_s = version_label, step_s
        self.service_label, self.environment_label = service_label, environment_label

    def _series(
        self, query: str, p: Any, environment: str, by_version: bool
    ) -> tuple[dict[str, dict[str, float | None]], str]:
        q = substitute(query, quote_string, service=p.service, environment=environment)
        data, link = self.http.get(
            "/api/v1/query_range",
            {
                "query": q,
                "start": utc_text(p.start),
                "end": utc_text(p.end),
                "step": f"{self.step_s}s",
            },
        )
        if data.get("resultType") != "matrix":
            raise SourceUnavailable(f"{self.name}: not a range result")
        out: dict[str, dict[str, float | None]] = {}
        for series in data.get("result") or []:
            labels = series.get("metric") or {}
            if (
                labels.get(self.service_label, p.service) != p.service
                or labels.get(self.environment_label, environment) != environment
            ):
                continue  # another service's series: dropped, whatever was asked (FR-44)
            key = str(labels.get(self.version_label, "")) if by_version else ""
            if by_version and not key:
                continue
            points = out.setdefault(key, {})
            for stamp, value in series.get("values") or []:
                points[utc_text(datetime.fromtimestamp(float(stamp), UTC))] = _num(value)
        return out, link

    def fetch(self, tool: str, params: BaseModel, environment: str) -> SourceResult:
        p: Any = params
        if tool == "get_dependency_health" and self.dependencies is not None:
            return self._dependencies(p, environment)
        if tool != "get_metric":
            raise SourceUnavailable(f"{self.name} has no {tool}")
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
        system = f"{self.name}:metric"
        return SourceResult(system, link, records[: p.limit], partial=len(records) > p.limit)

    def _instant(
        self, query: str, p: Any, environment: str, dependency: str
    ) -> tuple[float | None, str]:
        """One instant query at the call's `at`: one number, or None for no data. More than one
        series for this service is a configuration error, and a gap."""
        q = substitute(
            query, quote_string, service=p.service, environment=environment, dependency=dependency
        )
        data, link = self.http.get("/api/v1/query", {"query": q, "time": utc_text(p.at)})
        if data.get("resultType") not in ("vector", "scalar"):
            raise SourceUnavailable(f"{self.name}: not an instant result")
        if data.get("resultType") == "scalar":
            return _num((data.get("result") or [None, None])[1]), link
        values = [
            _num((series.get("value") or [None, None])[1])
            for series in data.get("result") or []
            if (series.get("metric") or {}).get(self.service_label, p.service) == p.service
            and (series.get("metric") or {}).get(self.environment_label, environment) == environment
        ]
        if len(values) > 1:
            raise SourceUnavailable(
                f"{self.name}: {dependency} gave {len(values)} series; aggregate it"
            )
        return (values[0] if values else None), link

    def _dependencies(self, p: Any, environment: str) -> SourceResult:
        assert self.dependencies is not None
        records, link = [], ""
        for dep, spec in self.dependencies.items():
            measured: dict[str, float | None] = {}
            for measure, query in spec["queries"].items():
                measured[measure], link = self._instant(query, p, environment, dep)
            records.append(
                dependency_record(
                    dep, spec, measured, p.at, f"from {self.name} at {utc_text(p.at)}"
                )
            )
        unknown = any(r["status"] == "unknown" for r in records)
        return SourceResult(f"{self.name}:dependencies", link, records, partial=unknown)


_LABEL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,99}$")
_GROUP_KEYS = {"selector", "type_label", "message_label", "version_label", "step_s"}


class LokiSource:
    """`get_logs` from Loki's range query API, and `get_error_groups` derived from it: a
    LogQL metric query counting the service's error lines by type, message and version."""

    def __init__(
        self,
        url: str,
        query: str | None = None,
        token: Any = None,
        username: str | None = None,
        tenant: str | None = None,
        service_label: str = "service",
        environment_label: str = "environment",
        error_groups: Mapping[str, Any] | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_bytes: int = MAX_RESPONSE_BYTES,
        name: str = "loki",
    ) -> None:
        if query is None and error_groups is None:
            raise ValueError(f"{name}: configure a query (get_logs) or error_groups, or both")
        if query is not None and (not isinstance(query, str) or "$service" not in query):
            raise ValueError(f"{name}: query must select the service with $service")
        self.name, self.query = name, query
        self.http = _Http(url, name, timeout_s, max_bytes, token, username, tenant)
        self.credentials = self.http.credentials
        self.service_label, self.environment_label = service_label, environment_label
        self.error_groups = None if error_groups is None else _group_spec(error_groups, name)

    def fetch(self, tool: str, params: BaseModel, environment: str) -> SourceResult:
        p: Any = params
        if tool == "get_error_groups" and self.error_groups is not None:
            return self._error_groups(p, environment)
        if tool != "get_logs" or self.query is None:
            raise SourceUnavailable(f"{self.name} has no {tool}")
        levels = wanted_levels(p.level)
        assert self.query is not None
        q = substitute(
            self.query,
            quote_string,
            service=p.service,
            environment=environment,
            levels="(?i)" + "|".join(sorted(levels)),
        )
        if p.contains:
            q += f' |= "{quote_string(p.contains)}"'
        data, link = self.http.get(
            "/loki/api/v1/query_range",
            {
                "query": q,
                "start": str(int(p.start.timestamp() * 1e9)),
                "end": str(int(p.end.timestamp() * 1e9)),
                "limit": str(p.limit),
                "direction": "forward",
            },
        )
        if data.get("resultType") != "streams":
            raise SourceUnavailable(f"{self.name}: not a log query result")
        records: list[Record] = []
        for stream in data.get("result") or []:
            labels = stream.get("stream") or {}
            if (
                labels.get(self.service_label, p.service) != p.service
                or labels.get(self.environment_label, environment) != environment
            ):
                continue
            for stamp, line in stream.get("values") or []:
                try:
                    parsed = json.loads(line)
                except ValueError:
                    parsed = None
                fields = {**labels, **(parsed if isinstance(parsed, dict) else {})}
                ts = datetime.fromtimestamp(int(stamp) / 1e9, UTC)
                if not p.start <= ts <= p.end:
                    continue
                rec = log_record(fields, utc_text(ts), str(line))
                if rec["level"] in levels:
                    records.append(rec)
        records.sort(key=lambda r: str(r["ts"]))
        # Loki stops at `limit` lines: a full page may have more behind it.
        full = sum(len(s.get("values") or []) for s in data.get("result") or []) >= p.limit
        return SourceResult(
            f"{self.name}:logs", link, records[: p.limit], partial=full or len(records) > p.limit
        )

    def _error_groups(self, p: Any, environment: str) -> SourceResult:
        """`sum by (type, message, version) (count_over_time(<selector> [step]))` over the
        window, one point a step: a group's count is the sum of its points, its first and last
        sighting the first and last step with lines (to the step, not the line). Loki's counts
        hold no stack, so a group has no frames and tier 4 can't come from here."""
        spec = self.error_groups
        assert spec is not None
        labels = [spec["type_label"], spec["message_label"], spec["version_label"]]
        selector = substitute(
            spec["selector"], quote_string, service=p.service, environment=environment
        )
        step = int(spec["step_s"])
        q = f"sum by ({', '.join(labels)}) (count_over_time({selector} [{step}s]))"
        data, link = self.http.get(
            "/loki/api/v1/query_range",
            {
                "query": q,
                "start": str(int(p.start.timestamp() * 1e9)),
                "end": str(int(p.end.timestamp() * 1e9)),
                "step": f"{step}s",
            },
        )
        if data.get("resultType") != "matrix":
            raise SourceUnavailable(f"{self.name}: not a metric query result")
        rows: list[Record] = []
        for series in data.get("result") or []:
            m = series.get("metric") or {}
            seen = [
                (datetime.fromtimestamp(float(t), UTC), _num(v))
                for t, v in series.get("values") or []
            ]
            seen = [
                (t, n) for t, n in seen if n and p.start <= t <= p.end + timedelta(seconds=step)
            ]
            if not seen:
                continue
            rows.append(
                {
                    "type": m.get(spec["type_label"]),
                    "message": m.get(spec["message_label"]),
                    "version": m.get(spec["version_label"]),
                    "count": round(sum(n for _, n in seen if n)),
                    # count_over_time at t counts (t - step, t]: the step's start, never later
                    "first_seen": utc_text(max(seen[0][0] - timedelta(seconds=step), p.start)),
                    "last_seen": utc_text(min(seen[-1][0], p.end)),
                    "frames": [],
                }
            )
        groups = error_group_records(rows)
        if p.release:
            groups = [g for g in groups if p.release in g["versions"]]
        return SourceResult(
            f"{self.name}:error_groups",
            link,
            groups[: p.limit],
            partial=len(groups) > p.limit,
        )


def _group_spec(spec: Mapping[str, Any], name: str) -> dict[str, Any]:
    if not isinstance(spec, Mapping) or set(spec) - _GROUP_KEYS:
        raise ValueError(f"{name}: error_groups takes {', '.join(sorted(_GROUP_KEYS))}")
    out = {
        "type_label": "error_type",
        "message_label": "error_message",
        "version_label": "version",
        "step_s": 60,
        **spec,
    }
    if not isinstance(out.get("selector"), str) or "$service" not in out["selector"]:
        raise ValueError(f"{name}: error_groups.selector must select the service with $service")
    for k in ("type_label", "message_label", "version_label"):
        if not isinstance(out[k], str) or not _LABEL.match(out[k]):
            raise ValueError(f"{name}: error_groups.{k} must be a label name")
    if not isinstance(out["step_s"], int) or not 10 <= out["step_s"] <= 3600:
        raise ValueError(f"{name}: error_groups.step_s is 10 to 3600")
    return out


class AlertmanagerSource:
    """`get_alert` from Alertmanager's API v2, `GET /api/v2/alerts`: the active alerts whose
    labels name the service and environment (silenced and inhibited ones left out)."""

    def __init__(
        self,
        url: str,
        token: Any = None,
        username: str | None = None,
        tenant: str | None = None,
        service_label: str = "service",
        environment_label: str = "environment",
        value_annotation: str | None = "value",
        threshold_annotation: str | None = "threshold",
        rule_annotation: str = "summary",
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_bytes: int = MAX_RESPONSE_BYTES,
        name: str = "alertmanager",
    ) -> None:
        for label in (service_label, environment_label):
            if not _LABEL.match(label):
                raise ValueError(f"{name}: {label!r} is not a label name")
        self.name = name
        self.http = _Http(url, name, timeout_s, max_bytes, token, username, tenant)
        self.credentials = self.http.credentials
        self.service_label, self.environment_label = service_label, environment_label
        self.value_annotation, self.threshold_annotation = value_annotation, threshold_annotation
        self.rule_annotation = rule_annotation

    def fetch(self, tool: str, params: BaseModel, environment: str) -> SourceResult:
        p: Any = params
        if tool != "get_alert":
            raise SourceUnavailable(f"{self.name} has no {tool}")
        query = [
            ("active", "true"),
            ("silenced", "false"),
            ("inhibited", "false"),
            ("filter", f'{self.service_label}="{quote_string(p.service)}"'),
            ("filter", f'{self.environment_label}="{quote_string(environment)}"'),
        ]
        found, link = self.http.get_json("/api/v2/alerts", query)
        if not isinstance(found, list):
            raise SourceUnavailable(f"{self.name}: not a list of alerts")
        records: list[Record] = []
        for a in found:
            labels, notes = a.get("labels") or {}, a.get("annotations") or {}
            if (
                labels.get(self.service_label) != p.service
                or labels.get(self.environment_label, environment) != environment
            ):
                continue  # FR-44: only alerts labelled with this service
            if p.alert_id is not None and p.alert_id not in (
                a.get("fingerprint"),
                labels.get("alertname"),
            ):
                continue
            state = (a.get("status") or {}).get("state")
            records.append(
                {
                    "alert_name": labels.get("alertname"),
                    "alert_id": a.get("fingerprint"),
                    "state": "firing" if state == "active" else state,
                    "service": p.service,
                    "environment": labels.get(self.environment_label, environment),
                    "rule": _rule(a.get("generatorURL")) or notes.get(self.rule_annotation),
                    "value": _num(notes.get(self.value_annotation))
                    if self.value_annotation
                    else None,
                    "threshold": (
                        _num(notes.get(self.threshold_annotation))
                        if self.threshold_annotation
                        else None
                    ),
                    # Alertmanager holds when the alert fired, not when its condition began
                    "breach_started_at": None,
                    "fired_at": utc_text(parse_time(a["startsAt"])) if a.get("startsAt") else None,
                }
            )
        records.sort(key=lambda r: str(r["fired_at"] or ""), reverse=True)
        return SourceResult(
            f"{self.name}:alert", link, records[:MAX_ALERTS], partial=len(records) > MAX_ALERTS
        )


def _rule(generator_url: Any) -> str | None:
    """The alerting rule's expression, from the Prometheus graph link Alertmanager keeps
    (`g0.expr`); never the link itself."""
    if not isinstance(generator_url, str):
        return None
    found = parse_qs(urlsplit(generator_url).query).get("g0.expr")
    return found[0] if found else None


_TRACE_ID = re.compile(r"^[0-9a-fA-F]{1,32}$")
_OTLP_STATUS = {"STATUS_CODE_ERROR": "ERROR", "STATUS_CODE_OK": "OK", 2: "ERROR", 1: "OK"}


class TempoSource:
    """`get_traces` from Grafana Tempo: TraceQL search, `GET /api/search`, for the service's
    failing traces in the window, then each one (up to `max_traces`) by `GET
    /api/traces/<traceID>` in OpenTelemetry JSON."""

    def __init__(
        self,
        url: str,
        query: str = '{ resource.service.name = "$service" && status = error }',
        token: Any = None,
        username: str | None = None,
        tenant: str | None = None,
        max_traces: int = MAX_TRACES,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_bytes: int = MAX_RESPONSE_BYTES,
        name: str = "tempo",
    ) -> None:
        if not isinstance(query, str) or "$service" not in query:
            raise ValueError(f"{name}: query must select the service with $service")
        if not 1 <= max_traces <= 20:
            raise ValueError(f"{name}: max_traces is 1 to 20")
        self.name, self.query, self.max_traces = name, query, max_traces
        self.http = _Http(url, name, timeout_s, max_bytes, token, username, tenant)
        self.credentials = self.http.credentials

    def fetch(self, tool: str, params: BaseModel, environment: str) -> SourceResult:
        p: Any = params
        if tool != "get_traces":
            raise SourceUnavailable(f"{self.name} has no {tool}")
        window = {"start": str(int(p.start.timestamp())), "end": str(int(p.end.timestamp()))}
        q = substitute(self.query, quote_string, service=p.service, environment=environment)
        found, link = self.http.get_json(
            "/api/search", {"q": q, **window, "limit": str(self.max_traces)}
        )
        if not isinstance(found, dict) or not isinstance(found.get("traces", []), list):
            raise SourceUnavailable(f"{self.name}: not a search result")
        ids = [
            str(t.get("traceID"))
            for t in found.get("traces") or []
            if _TRACE_ID.match(str(t.get("traceID")))
        ]
        records: list[Record] = []
        for trace_id in ids[: self.max_traces]:
            body, _ = self.http.get_json(f"/api/traces/{trace_id}", window)
            spans = _otlp_spans(body, p.service, environment)
            failing = any(s["status"] == "ERROR" for s in spans)
            on_path = not p.path or any(
                p.path in str(s["name"]) or p.path in str(s["attributes"].get("http.route", ""))
                for s in spans
            )
            starts = [parse_time(s["start"]) for s in spans if s["start"]]
            in_window = starts and p.start <= min(starts) <= p.end
            if failing and on_path and in_window:
                records.append({"trace_id": trace_id.lower().zfill(32), "spans": spans})
        # Tempo stops at `limit` traces: a full page may have more behind it.
        return SourceResult(
            f"{self.name}:traces", link, records, partial=len(ids) >= self.max_traces
        )


def _otlp_value(v: Any) -> Any:
    if not isinstance(v, dict):
        return v
    for k in ("stringValue", "boolValue", "doubleValue"):
        if k in v:
            return v[k]
    if "intValue" in v:
        return int(v["intValue"])  # a string in OTLP JSON
    if "arrayValue" in v:
        return [_otlp_value(x) for x in (v["arrayValue"] or {}).get("values") or []]
    return None


def _otlp_attrs(items: Any) -> dict[str, Any]:
    return {a.get("key"): _otlp_value(a.get("value")) for a in items or [] if a.get("key")}


def _otlp_spans(body: Any, service: str, environment: str) -> list[Record]:
    """The service's spans of one OpenTelemetry JSON trace (Tempo's `batches`, or the
    `trace.resourceSpans` of `/api/v2/traces`), in the platform's span shape. A resource
    naming another service or environment is dropped (FR-44)."""
    if not isinstance(body, dict):
        raise SourceUnavailable("tempo: not a trace")
    batches = body.get("batches") or (body.get("trace") or {}).get("resourceSpans") or []
    spans: list[Record] = []
    for batch in batches:
        res = _otlp_attrs((batch.get("resource") or {}).get("attributes"))
        env = res.get("deployment.environment.name") or res.get("deployment.environment")
        if res.get("service.name") != service or env not in (None, environment):
            continue
        for scope in batch.get("scopeSpans") or batch.get("instrumentationLibrarySpans") or []:
            for s in scope.get("spans") or []:
                start, end = s.get("startTimeUnixNano"), s.get("endTimeUnixNano")
                spans.append(
                    {
                        "name": s.get("name"),
                        "status": _OTLP_STATUS.get((s.get("status") or {}).get("code"), "UNSET"),
                        "start": iso_ms(datetime.fromtimestamp(int(start) / 1e9, UTC))
                        if start
                        else None,
                        "end": iso_ms(datetime.fromtimestamp(int(end) / 1e9, UTC)) if end else None,
                        "attributes": _otlp_attrs(s.get("attributes")),
                        "exceptions": [
                            {
                                "type": ev.get("exception.type"),
                                "message": ev.get("exception.message"),
                            }
                            for ev in (
                                _otlp_attrs(e.get("attributes"))
                                for e in s.get("events") or []
                                if e.get("name") == "exception"
                            )
                        ],
                    }
                )
    spans.sort(key=lambda s: str(s["start"] or ""))
    return spans
