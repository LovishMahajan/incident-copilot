"""Artifact 9.4, 13.5 (M5, ADR-75): Prometheus for metrics and Loki for logs, read-only.

Prometheus answers `get_metric` through the HTTP API v1 range query, `GET /api/v1/query_range`
(https://prometheus.io/docs/prometheus/latest/querying/api/#range-queries, API v1, as served by
Prometheus 2.x and 3.x). Loki answers `get_logs` through `GET /loki/api/v1/query_range`
(https://grafana.com/docs/loki/latest/reference/loki-http-api/#query-logs-within-a-range-of-time,
Loki 2.x and 3.x). Neither holds alerts, changes, traces, error groups or dependency health; a
call for one raises SourceUnavailable, which is a gap (ADR-54).

The queries are configuration, written by the service's owners, with `$service`, `$environment`
and, for logs, `$levels` (a regular expression of the levels asked for) substituted as quoted
string contents: the template supplies the quotes, as in `service="$service"`. Each metric has a
`value` query and, for `http_5xx_rate` and `error_rate_by_version`, an optional `requests` query;
`error_rate_by_version` groups by the label `version_label` names. A get_logs `contains` is
added as a line filter, `|= "..."`.

Bounds (Artifact 9.4): the window is the call's (the ToolGate already capped it), one point a
`step_s`, at most `limit` rows, at most MAX_RESPONSE_BYTES read, `timeout_s` per call; retries and
the circuit breaker are the collector's, as for every source. A series labelled with another
service or environment is dropped (FR-44). Credentials, a bearer token or a basic-auth password,
are by reference (SecretRef). Tested against recorded responses only (tests/unit/test_vendor_sources.py).
"""

from __future__ import annotations

import base64
import json
import math
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
    optional_ref,
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
        link = f"{self.url}{path}?{urlencode(query)}"
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
        if not isinstance(body, dict) or body.get("status") != "success":
            raise SourceUnavailable(f"{self.name}: the query failed")
        return body.get("data") or {}, link


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


class PrometheusSource:
    """`get_metric` from Prometheus's range query API."""

    def __init__(
        self,
        url: str,
        metrics: Mapping[str, Any],
        token: Any = None,
        username: str | None = None,
        tenant: str | None = None,
        version_label: str = "version",
        service_label: str = "service",
        environment_label: str = "environment",
        step_s: int = 60,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_bytes: int = MAX_RESPONSE_BYTES,
        name: str = "prometheus",
    ) -> None:
        self.name = name
        self.http = _Http(url, name, timeout_s, max_bytes, token, username, tenant)
        self.credentials = self.http.credentials
        self.metrics = metric_queries(metrics, name)
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


class LokiSource:
    """`get_logs` from Loki's range query API."""

    def __init__(
        self,
        url: str,
        query: str,
        token: Any = None,
        username: str | None = None,
        tenant: str | None = None,
        service_label: str = "service",
        environment_label: str = "environment",
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_bytes: int = MAX_RESPONSE_BYTES,
        name: str = "loki",
    ) -> None:
        if not isinstance(query, str) or "$service" not in query:
            raise ValueError(f"{name}: query must select the service with $service")
        self.name, self.query = name, query
        self.http = _Http(url, name, timeout_s, max_bytes, token, username, tenant)
        self.credentials = self.http.credentials
        self.service_label, self.environment_label = service_label, environment_label

    def fetch(self, tool: str, params: BaseModel, environment: str) -> SourceResult:
        p: Any = params
        if tool != "get_logs":
            raise SourceUnavailable(f"{self.name} has no {tool}")
        levels = wanted_levels(p.level)
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
