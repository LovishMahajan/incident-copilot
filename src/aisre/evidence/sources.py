"""Artifact 9.4, 13.5 T1.10: the read-only source adapters behind the READ tools.

An adapter holds the source's address (and, for a real source, its narrow credential, never
shown to the model: ST-17) and answers one tool call with records in the platform's own shape,
below, so the collector and the fact rules (aisre.evidence.facts) never see a vendor's format.
Which adapter answers which tool, for which service and environment, is configuration
(`SourceSet.from_config`, ADR-75): a real source is one more class in ADAPTERS and one more
entry in the configuration, nothing else.

Every adapter is read-only, stays inside the call's service, environment and window (it asks
the source for that scope and drops anything else it is sent: FR-44, ST-22), and raises
SourceUnavailable when the source fails, times out, or doesn't hold that kind of data. The
collector turns that into a gap record. An empty answer is an answer; a failure never is (ADR-54).

Records, one list per call:

    alert         {alert_name, alert_id, state, service, environment, rule, value, threshold,
                   breach_started_at, fired_at}
    metric        {at, value, requests} or, for error_rate_by_version, {at, versions: {version:
                   {rate, requests}}}
    log           {ts, level, instance, version, message, error_type, error_message, stack,
                   trace_id, path, status}
    trace         {trace_id, spans: [{name, status, start, end, attributes, exceptions}]}
    change        {change_id, version, previous_version, code_changed, files, config_changes:
                   [{key, old, new}], config_revision, stages: [{step, at}], rollout:
                   [{traffic_pct, at}], diff}
    error_group   {type, message, count, first_seen, last_seen, versions, frames: [[file, line]]}
    dependency    {name, kind, status, observed_at, detail}
"""

from __future__ import annotations

import importlib
import json
import os.path
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlencode

from pydantic import BaseModel

from aisre.config import referenced_value

Record = dict[str, Any]
DEFAULT_TIMEOUT_S = 20.0  # per source call; the collector's 180 s covers a slow source (FR-09)


class SourceUnavailable(Exception):
    """The source failed, timed out, or doesn't hold this kind of data. Becomes a gap."""


@dataclass(frozen=True)
class SourceResult:
    source_system: str  # which system was read
    source_link: str  # where the original is
    records: list[Record]
    source_as_of: datetime | None = None  # the source's own version time, if it gives one
    partial: bool = False  # the source said its answer is incomplete (NFR-29)


class Source(Protocol):
    """One configured source. `params` is the ToolGate's validated parameter model. A real
    adapter also has `serves() -> frozenset[str]`, the tools its configuration answers, so the
    go-live check can tell a `"*"` that covers a tool from one that would only ever be a gap."""

    def fetch(self, tool: str, params: BaseModel, environment: str) -> SourceResult: ...


def served(**tools: bool) -> frozenset[str]:
    """`serves()`'s answer from `tool=answered` flags."""
    return frozenset(t for t, on in tools.items() if on)


def answers(source: Source, tool: str) -> bool:
    """Whether `source` answers `tool`: an adapter without `serves()` (the fake) is taken at its
    configuration."""
    serves = getattr(source, "serves", None)
    return serves is None or tool in serves()


# ---------------------------------------------------------------------------
# What every real adapter shares: credentials by reference, and bounded HTTP
# ---------------------------------------------------------------------------
MAX_RESPONSE_BYTES = 4_000_000  # per source answer, before the collector caps what is kept


class SecretRef:
    """A credential by reference, never by value: `{"env": NAME}` or `{"file": PATH}`. It is read
    when a call needs it, never when the adapter is built (so `aisre pilot check` can build every
    adapter without reaching a secret or the network), and never logged, shown or stored."""

    def __init__(self, ref: Any, what: str) -> None:
        if not (
            isinstance(ref, Mapping)
            and len(ref) == 1
            and next(iter(ref)) in ("env", "file")
            and isinstance(next(iter(ref.values())), str)
            and next(iter(ref.values()))
        ):
            raise ValueError(f'{what}: by reference only, {{"env": NAME}} or {{"file": PATH}}')
        (self.kind, self.where), self.what = next(iter(ref.items())), what

    def describe(self) -> str:
        return (
            f"{self.what} ({'environment variable' if self.kind == 'env' else 'file'} {self.where})"
        )

    def present(self, environ: Mapping[str, str] | None = None) -> bool:
        if self.kind == "env":
            return bool(referenced_value(self.where, environ))
        return os.path.isfile(self.where)

    def value(self) -> str:
        try:
            found = (
                referenced_value(self.where)
                if self.kind == "env"
                else open(self.where, encoding="utf-8").read().strip()
            )
        except OSError:
            found = ""
        if not found:
            raise SourceUnavailable(f"{self.describe()} is not set")
        return found


def optional_ref(ref: Any, what: str) -> SecretRef | None:
    return None if ref is None else SecretRef(ref, what)


def http_json(
    req: urllib.request.Request, name: str, timeout_s: float, max_bytes: int = MAX_RESPONSE_BYTES
) -> Any:
    """One HTTP call to a source, bounded in time and size. Every failure is SourceUnavailable,
    whose text names the source and the status, never the URL (it may carry a query) or a
    header (it carries the credential)."""
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read(max_bytes + 1)
    except urllib.error.HTTPError as e:
        raise SourceUnavailable(f"{name}: HTTP {e.code}") from None
    except TimeoutError:
        raise SourceUnavailable(f"{name}: no answer in {timeout_s:g} s") from None
    except (urllib.error.URLError, OSError) as e:
        raise SourceUnavailable(f"{name}: {type(e).__name__}") from None
    if len(raw) > max_bytes:
        raise SourceUnavailable(f"{name}: answer over {max_bytes} bytes")
    try:
        return json.loads(raw)
    except ValueError:
        raise SourceUnavailable(f"{name}: unreadable answer") from None


def substitute(template: str, quote: Callable[[str], str], **values: str) -> str:
    """`$service`, `$environment` (and any other `$name` given) in a configured query, each
    value through the query language's own quoting, so a value can't change the query."""
    out = template
    for key in sorted(values, key=len, reverse=True):
        out = out.replace(f"${key}", quote(values[key]))
    return out


METRICS = ("http_5xx_rate", "http_latency_p95", "http_request_rate", "error_rate_by_version")


def metric_queries(metrics: Mapping[str, Any], what: str) -> dict[str, dict[str, str]]:
    """`{metric: {"value": query, "requests": query?}}`, the metrics the registry names only."""
    out: dict[str, dict[str, str]] = {}
    for metric, spec in metrics.items():
        if metric not in METRICS:
            raise ValueError(f"{what}: no metric {metric!r} (one of {', '.join(METRICS)})")
        if (
            not isinstance(spec, Mapping)
            or not isinstance(spec.get("value"), str)
            or set(spec) - {"value", "requests"}
            or not all(isinstance(v, str) for v in spec.values())
        ):
            raise ValueError(f'{what}: {metric} is {{"value": query, "requests": query}}')
        out[metric] = dict(spec)
    return out


def metric_records(
    metric: str,
    value: Mapping[str, Mapping[str, float | None]],
    requests: Mapping[str, Mapping[str, float | None]],
) -> list[Record]:
    """The platform's metric records from series already read: `value` and `requests` map a
    series key (a version, or "" for the one series) to {time: number}."""
    if metric == "error_rate_by_version":
        times = sorted({t for s in value.values() for t in s})
        return [
            {
                "at": t,
                "versions": {
                    v: {"rate": s.get(t), "requests": requests.get(v, {}).get(t)}
                    for v, s in sorted(value.items())
                    if t in s
                },
            }
            for t in times
        ]
    if len(value) > 1:
        raise SourceUnavailable(f"{metric}: the query gave {len(value)} series; aggregate it")
    series = next(iter(value.values()), {})
    reqs = next(iter(requests.values()), {})
    out: list[Record] = []
    for t in sorted(series):
        rec: Record = {"at": t, "value": series[t]}
        if metric == "http_5xx_rate":
            rec["requests"] = reqs.get(t)
        out.append(rec)
    return out


def log_record(fields: Mapping[str, Any], ts: str, line: str) -> Record:
    """A log line as the platform's record, from its parsed fields (JSON, labels or attributes),
    flat or nested the way the fake's are; the line itself is the message when nothing names one."""
    nested_err, nested_http = fields.get("error"), fields.get("http")
    err: Mapping[str, Any] = nested_err if isinstance(nested_err, Mapping) else {}
    http: Mapping[str, Any] = nested_http if isinstance(nested_http, Mapping) else {}
    method, path = (
        http.get("method") or fields.get("method"),
        http.get("path") or fields.get("path"),
    )
    level = fields.get("level") or fields.get("status") or fields.get("severity")
    return {
        "ts": ts,
        "level": {"WARN": "WARNING"}.get(str(level).upper(), str(level).upper()) if level else None,
        "instance": fields.get("instance") or fields.get("host"),
        "version": fields.get("version"),
        "message": fields.get("message") or fields.get("msg") or line,
        "error_type": err.get("type") or fields.get("error_type"),
        "error_message": err.get("message") or fields.get("error_message"),
        "stack": err.get("stack") or fields.get("stack"),
        "trace_id": fields.get("trace_id"),
        "path": f"{method or ''} {path or ''}".strip() or None,
        "status": http.get("status") or fields.get("status_code"),
    }


# ---------------------------------------------------------------------------
# Shared by the adapters for alerts, traces, error groups and dependency health (M5, ADR-75)
# ---------------------------------------------------------------------------
MAX_ALERTS = 20  # alerts kept per get_alert call: the tool's 16 KB holds about that many
MAX_TRACES = 5  # traces read in full per get_traces call (each is a further request)
_FRAME = re.compile(r'File "([^"]+)", line (\d+)')
_DEP_NAME = re.compile(r"^[A-Za-z0-9_.:/-]{1,100}$")
_DEP_KEYS = {"kind", "error_rate", "latency_p95", "max_error_rate", "max_latency_s"}
DEPENDENCY_MEASURES = ("error_rate", "latency_p95")


def frames_from(stack: str | None) -> list[list[str]]:
    """`[file, line]` for each frame of a Python traceback, in order, without repeats (the
    fake error tracker's rule, ops/fakes/monitoring.py)."""
    out: list[list[str]] = []
    for frame in _FRAME.findall(stack or ""):
        if list(frame) not in out:
            out.append(list(frame))
    return out


def error_group_records(rows: Iterable[Mapping[str, Any]]) -> list[Record]:
    """Error groups from rows already counted by a source, one row per (type, message, version):
    `{type, message, version?, count, first_seen, last_seen, frames?}`. Rows of one type and
    message merge: counts add, first and last sighting widen, versions and frames gather. In
    order of first sighting, as the fake error tracker gives them."""
    groups: dict[tuple[str, str], Record] = {}
    for r in rows:
        if not r.get("count"):
            continue
        key = (str(r.get("type") or ""), str(r.get("message") or ""))
        g = groups.setdefault(
            key,
            {
                "type": key[0] or None,
                "message": key[1] or None,
                "count": 0,
                "first_seen": r["first_seen"],
                "last_seen": r["last_seen"],
                "versions": [],
                "frames": [],
            },
        )
        g["count"] += int(r["count"])
        g["first_seen"] = min(g["first_seen"], r["first_seen"], key=parse_time)
        g["last_seen"] = max(g["last_seen"], r["last_seen"], key=parse_time)
        if r.get("version") and r["version"] not in g["versions"]:
            g["versions"].append(r["version"])
        for frame in r.get("frames") or []:
            if list(frame) not in g["frames"]:
                g["frames"].append(list(frame))
    for g in groups.values():
        g["versions"].sort()
    return sorted(groups.values(), key=lambda g: parse_time(g["first_seen"]))


def dependency_specs(deps: Any, what: str) -> dict[str, dict[str, Any]]:
    """`{name: {"kind": ..., "error_rate": query?, "latency_p95": query?, "max_error_rate":
    0.05, "max_latency_s": seconds}}`: the service's dependencies and the queries that measure
    each, written by its owners. A name is a plain value (it is put in queries as
    `$dependency`); at least one query each; a latency query needs its limit."""
    if not isinstance(deps, Mapping) or not deps:
        raise ValueError(f"{what}: dependencies is {{name: {{kind, error_rate, latency_p95}}}}")
    out: dict[str, dict[str, Any]] = {}
    for name, spec in deps.items():
        if not isinstance(name, str) or not _DEP_NAME.match(name):
            raise ValueError(f"{what}: dependency name {name!r} is not a plain value")
        if not isinstance(spec, Mapping) or set(spec) - _DEP_KEYS:
            raise ValueError(f"{what}: dependency {name} takes {', '.join(sorted(_DEP_KEYS))}")
        if not isinstance(spec.get("kind"), str):
            raise ValueError(f"{what}: dependency {name} needs a kind")
        queries = {m: spec[m] for m in DEPENDENCY_MEASURES if m in spec}
        if not queries or not all(isinstance(q, str) and q for q in queries.values()):
            raise ValueError(f"{what}: dependency {name} needs an error_rate or latency_p95 query")
        limits = {"max_error_rate": spec.get("max_error_rate", 0.05)}
        if "latency_p95" in queries:
            limits["max_latency_s"] = spec.get("max_latency_s")
        if not all(isinstance(v, int | float) and v > 0 for v in limits.values()):
            raise ValueError(f"{what}: dependency {name}: limits are positive numbers")
        out[name] = {"kind": spec["kind"], "queries": queries, **limits}
    return out


def dependency_record(
    name: str, spec: Mapping[str, Any], measured: Mapping[str, float | None], at: datetime, how: str
) -> Record:
    """One dependency's health from what its queries measured at `at`. Any measure without
    data is `unknown`, never healthy; over a limit is `degraded`. A failed query never gets
    here: it is a gap for the whole call (ADR-54)."""
    parts, missing, over = [], [], []
    for m in spec["queries"]:
        v = measured.get(m)
        limit = spec["max_error_rate"] if m == "error_rate" else spec["max_latency_s"]
        unit = "" if m == "error_rate" else " s"
        if v is None:
            missing.append(m)
            parts.append(f"{m}: no data")
            continue
        if v >= limit:
            over.append(m)
        parts.append(f"{m} {v:g}{unit} (limit {limit:g}{unit})")
    status = "unknown" if missing else "degraded" if over else "healthy"
    return {
        "name": name,
        "kind": spec["kind"],
        "status": status,
        "observed_at": utc_text(at),
        "detail": "; ".join(parts) + f", {how}",
    }


def iso_ms(t: datetime) -> str:
    """A span's time: UTC to the millisecond, `Z`, as the fake's traces give it."""
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def wanted_levels(level: str | None) -> set[str]:
    """The log levels a get_logs call asks for: the one named, or errors and warnings."""
    return {level.upper()} if level else {"ERROR", "WARNING", "WARN"}


def parse_time(value: str) -> datetime:
    t = datetime.fromisoformat(value)
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def utc_text(t: datetime) -> str:
    """The form every source here is sent: UTC, whole seconds, `Z`."""
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _in(stamp: str | None, start: datetime | None, end: datetime | None) -> bool:
    if stamp is None:
        return False
    t = parse_time(stamp)
    return (start is None or t >= start) and (end is None or t <= end)


def _scoped(item: Mapping[str, Any], service: str, environment: str) -> bool:
    """Anything labelled with another service or environment is dropped, whatever was asked."""
    return item.get("service", service) == service and item.get("environment", environment) in (
        environment,
        None,
    )


# ---------------------------------------------------------------------------
# The fake monitoring source (ops/fakes/monitoring.py): local and test use only
# ---------------------------------------------------------------------------
_VERSION_COLUMN = re.compile(r"^rate_500_v(\d+(?:_\d+)*)$")


class FakeMonitoringSource:
    """LOCAL USE ONLY: one scenario of the fake monitoring source, at `url`
    (…/v1/scenarios/<name>). Each tool maps to one of its endpoints."""

    def __init__(self, url: str, timeout_s: float = DEFAULT_TIMEOUT_S, name: str = "") -> None:
        self.url = url.rstrip("/")
        self.timeout_s = timeout_s
        self.name = name or "fake-monitoring"

    def _get(self, kind: str, query: dict[str, str]) -> tuple[Any, str]:
        link = f"{self.url}/{kind}" + (f"?{urlencode(query)}" if query else "")
        try:
            with urllib.request.urlopen(link, timeout=self.timeout_s) as resp:
                return json.loads(resp.read()), link
        except urllib.error.HTTPError as e:
            raise SourceUnavailable(f"{self.name} {kind}: HTTP {e.code}") from None
        except TimeoutError:
            raise SourceUnavailable(
                f"{self.name} {kind}: no answer in {self.timeout_s:g} s"
            ) from None
        except (urllib.error.URLError, OSError) as e:
            raise SourceUnavailable(f"{self.name} {kind}: {type(e).__name__}") from None
        except ValueError:
            raise SourceUnavailable(f"{self.name} {kind}: unreadable answer") from None

    def fetch(self, tool: str, params: BaseModel, environment: str) -> SourceResult:
        p: Any = params
        service: str = p.service
        start: datetime | None = getattr(p, "start", None)
        end: datetime | None = getattr(p, "end", None)
        window = {"start": utc_text(start), "end": utc_text(end)} if start and end else {}
        limit: int = getattr(p, "limit", 500)
        system = f"{self.name}:{tool.removeprefix('get_')}"

        if tool == "get_alert":
            body, link = self._get("alert", {})
            raw = body.get("monitoring_alert", {})
            alert = {
                "alert_name": raw.get("alert_name"),
                "alert_id": raw.get("alert_id"),
                "state": raw.get("state"),
                "service": raw.get("service"),
                "environment": raw.get("environment"),
                "rule": raw.get("rule"),
                "value": raw.get("value_at_firing"),
                "threshold": raw.get("threshold"),
                "breach_started_at": raw.get("breach_started_at"),
                "fired_at": raw.get("fired_at"),
            }
            wanted = p.alert_id is None or alert["alert_id"] == p.alert_id
            records = [alert] if wanted and _scoped(alert, service, environment) else []
            return SourceResult(system, link, records)

        if tool == "get_metric":
            rows, link = self._get("metrics", window)
            rows = [r for r in rows if _in(r.get("minute_utc"), start, end)]
            records = _metric(p.metric, rows)
            return SourceResult(system, link, records[:limit], partial=len(records) > limit)

        if tool == "get_logs":
            rows, link = self._get("logs", window)
            levels = {p.level.upper()} if p.level else {"ERROR", "WARNING"}
            records = [
                _log(r)
                for r in rows
                if _scoped(r, service, environment)
                and _in(r.get("ts"), start, end)
                and r.get("level") in levels
                and (not p.contains or p.contains.lower() in json.dumps(r).lower())
            ]
            return SourceResult(system, link, records[:limit], partial=len(records) > limit)

        if tool == "get_traces":
            trace, link = self._get("traces", {})
            spans = [_span(s) for s in trace.get("spans", []) if s.get("service") == service]
            failing = any(s["status"] == "ERROR" for s in spans)
            on_path = not p.path or any(p.path in s["name"] for s in spans)
            in_window = spans and _in(min(s["start"] for s in spans), start, end)
            records = (
                [{"trace_id": trace.get("trace_id"), "spans": spans}]
                if failing and on_path and in_window
                else []
            )
            return SourceResult(system, link, records)

        if tool == "get_changes":
            body, link = self._get("changes", {})
            change = _change(body)
            times = [s["at"] for s in change["stages"]] or [body.get("recorded_at")]
            in_window = any(_in(t, start, end) for t in times if t)
            ok = in_window and _scoped(body, service, environment)
            as_of = parse_time(body["recorded_at"]) if body.get("recorded_at") else None
            return SourceResult(system, link, [change] if ok else [], source_as_of=as_of)

        if tool == "get_error_groups":
            groups, link = self._get("errors", window)
            records = [
                {k: g.get(k) for k in _ERROR_GROUP_FIELDS}
                for g in groups
                if _scoped(g, service, environment)
                and (not p.release or p.release in g.get("versions", []))
            ]
            return SourceResult(system, link, records[:limit], partial=len(records) > limit)

        if tool == "get_dependency_health":
            body, link = self._get("dependencies", {"at": utc_text(p.at)})
            records = (
                [
                    {k: d.get(k) for k in ("name", "kind", "status", "observed_at", "detail")}
                    for d in body.get("dependencies", [])
                ]
                if _scoped(body, service, environment)
                else []
            )
            return SourceResult(system, link, records)

        raise SourceUnavailable(f"{self.name} has no {tool}")


_ERROR_GROUP_FIELDS = (
    "type",
    "message",
    "count",
    "first_seen",
    "last_seen",
    "versions",
    "frames",
)


def _metric(metric: str, rows: list[dict[str, str]]) -> list[Record]:
    def num(v: str | None) -> float | None:
        return float(v) if v not in (None, "") else None

    if metric == "http_5xx_rate":
        return [
            {
                "at": r["minute_utc"],
                "value": num(r.get("rate_500_total")),
                "requests": num(r.get("requests_total")),
            }
            for r in rows
        ]
    if metric == "http_request_rate":
        return [{"at": r["minute_utc"], "value": num(r.get("requests_total"))} for r in rows]
    if metric == "error_rate_by_version":
        out = []
        for r in rows:
            versions = {}
            for col in r:
                m = _VERSION_COLUMN.match(col)
                if m:
                    tag = m.group(1)
                    versions[tag.replace("_", ".")] = {
                        "rate": num(r[col]),
                        "requests": num(r.get(f"requests_v{tag}")),
                    }
            out.append({"at": r["minute_utc"], "versions": versions})
        return out
    raise SourceUnavailable(f"metric {metric} is not reported by this source")


def _log(r: dict[str, Any]) -> Record:
    err, http = r.get("error") or {}, r.get("http") or {}
    return {
        "ts": r.get("ts"),
        "level": r.get("level"),
        "instance": r.get("instance"),
        "version": r.get("version"),
        "message": r.get("message"),
        "error_type": err.get("type"),
        "error_message": err.get("message"),
        "stack": err.get("stack"),
        "trace_id": r.get("trace_id"),
        "path": f"{http.get('method', '')} {http.get('path', '')}".strip() or None,
        "status": http.get("status"),
    }


def _span(s: dict[str, Any]) -> Record:
    return {
        "name": s.get("name"),
        "status": s.get("status"),
        "start": s.get("start"),
        "end": s.get("end"),
        "attributes": s.get("attributes", {}),
        "exceptions": [
            {
                "type": e.get("attributes", {}).get("exception.type"),
                "message": e.get("attributes", {}).get("exception.message"),
            }
            for e in s.get("events", [])
            if e.get("name") == "exception"
        ],
    }


def _change(body: dict[str, Any]) -> Record:
    revision = body.get("config_revision") or {}
    return {
        "change_id": f"release {body.get('version')}",
        "version": body.get("version"),
        "previous_version": body.get("previous_version"),
        "code_changed": body.get("code_changed"),
        "files": sorted({f for c in body.get("commits", []) for f in c.get("files", [])}),
        "commits": [
            {"sha": c.get("sha"), "message": c.get("message")} for c in body.get("commits", [])
        ],
        "config_changes": body.get("config_changes", []),
        "config_revision": revision.get("new"),
        "stages": body.get("stages", []),
        "rollout": [
            {"traffic_pct": s.get("traffic_pct"), "at": s.get("at")}
            for s in (body.get("rollout") or {}).get("steps", [])
        ],
        "diff": body.get("config_diff"),
    }


# ---------------------------------------------------------------------------
# Configuration: which adapter answers which tool, for which service
# ---------------------------------------------------------------------------
def _lazy(module: str, cls: str) -> Callable[..., Source]:
    """An adapter class imported when it is first configured, so one vendor's module (and its
    optional dependency) loads only where it is used."""

    def build(**spec: Any) -> Source:
        built: Source = getattr(importlib.import_module(module), cls)(**spec)
        return built

    return build


ADAPTERS: dict[str, Callable[..., Source]] = {
    "fake_monitoring": FakeMonitoringSource,
    # Real sources (M5, ADR-75): read-only, credentials by reference (SecretRef).
    "prometheus": _lazy("aisre.evidence.prometheus", "PrometheusSource"),
    "loki": _lazy("aisre.evidence.prometheus", "LokiSource"),
    "alertmanager": _lazy("aisre.evidence.prometheus", "AlertmanagerSource"),
    "tempo": _lazy("aisre.evidence.prometheus", "TempoSource"),
    "datadog": _lazy("aisre.evidence.datadog", "DatadogSource"),
    "cloudwatch": _lazy("aisre.evidence.cloudwatch", "CloudWatchSource"),
}
LOCAL_ONLY = {"fake_monitoring"}
# The READ tools a source answers: every READ tool in aisre.agent.tool_registry but the two
# the corpus answers (search_runbooks, search_past_incidents). A unit test keeps them equal.
SOURCE_TOOLS = (
    "get_alert",
    "get_metric",
    "get_logs",
    "get_traces",
    "get_changes",
    "get_error_groups",
    "get_dependency_health",
)
ToolSources = Mapping[str, Source]


@dataclass(frozen=True)
class SourceSet:
    """Tool name -> source; `"*"` answers any tool not named. `services` holds the same map per
    service (key `service`) or per service and environment (key `service/environment`). The
    most specific scope that names the tool, or failing that has a `"*"`, answers: the service
    and environment, then the service, then the platform-wide `sources`. A service no scope
    covers has no source, and the collector records a gap: never another service's source.

    `replay_as_of` is for replaying a recorded scenario only: the collector treats it as "now",
    so data after the scenario's evidence cut-off stays hidden
    (tests/fixtures/reference-incident/README.md)."""

    sources: ToolSources
    replay_as_of: datetime | None = None
    services: Mapping[str, ToolSources] = field(default_factory=dict)
    # scope -> {tool: reason}: gaps the service's owners accept, each with its reason (ADR-75).
    # Only `aisre pilot check` reads it, to report WARN instead of FAIL; every call is still a gap.
    allowed_gaps: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    def allowed_gap(self, tool: str, service: str, environment: str) -> str | None:
        """The stated reason `tool` may be a gap for this service, most specific scope first."""
        for scope in (f"{service}/{environment}", service):
            reason = self.allowed_gaps.get(scope, {}).get(tool)
            if reason is not None:
                return reason
        return None

    def for_tool(
        self, tool: str, service: str | None = None, environment: str | None = None
    ) -> Source | None:
        scopes: list[ToolSources] = []
        if service is not None:
            if environment is not None and f"{service}/{environment}" in self.services:
                scopes.append(self.services[f"{service}/{environment}"])
            if service in self.services:
                scopes.append(self.services[service])
        scopes.append(self.sources)
        for scope in scopes:
            if found := scope.get(tool) or scope.get("*"):
                return found
        return None

    @classmethod
    def from_config(cls, config: Mapping[str, Any], *, production: bool = False) -> SourceSet:
        """`{"sources": {"*" | tool: {"adapter": name, ...its arguments}}, "services":
        {service | "service/environment": {"*" | tool: {...}}}, "replay_as_of": ...}`. Either
        map may be absent. In production, local-only adapters and replay are refused."""
        unknown = set(config) - {"sources", "services", "replay_as_of"}
        if unknown:
            raise ValueError(f"unknown evidence configuration keys: {sorted(unknown)}")
        services: dict[str, ToolSources] = {}
        allowed: dict[str, dict[str, str]] = {}
        for scope, tools in config.get("services", {}).items():
            service, _, environment = scope.partition("/")
            if not service or "/" in environment or scope.endswith("/"):
                raise ValueError(f"evidence scope {scope!r}: `service` or `service/environment`")
            tools = dict(tools)
            if "allowed_gaps" in tools:
                allowed[scope] = _allowed_gaps(scope, tools.pop("allowed_gaps"), tools)
            services[scope] = _tool_sources(tools, production)
        sources = _tool_sources(config.get("sources", {}), production)
        replay = config.get("replay_as_of")
        if production and replay:
            raise ValueError("replay_as_of is for replaying scenarios, never production")
        return cls(sources, parse_time(replay) if replay else None, services, allowed)


MIN_REASON = 10  # characters: a stated reason, not a placeholder


def _allowed_gaps(scope: str, gaps: Any, tools: Mapping[str, Any]) -> dict[str, str]:
    """`[{"tool": name, "reason": why}]`: source tools this service may go live without. A tool
    the same scope names an adapter for can't also be an allowed gap."""
    what = f"evidence scope {scope!r}: allowed_gaps"
    if not isinstance(gaps, list) or not gaps:
        raise ValueError(f'{what} is [{{"tool": name, "reason": why}}]')
    out: dict[str, str] = {}
    for gap in gaps:
        if not isinstance(gap, Mapping) or set(gap) != {"tool", "reason"}:
            raise ValueError(f'{what} is [{{"tool": name, "reason": why}}]')
        tool, reason = gap["tool"], gap["reason"]
        if tool not in SOURCE_TOOLS:
            raise ValueError(f"{what}: no evidence source answers {tool!r}")
        if not isinstance(reason, str) or len(reason.strip()) < MIN_REASON:
            raise ValueError(f"{what}: {tool} needs a stated reason")
        if tool in tools or tool in out:
            raise ValueError(f"{what}: {tool} is configured or listed twice")
        out[tool] = reason.strip()
    return out


def _tool_sources(tools: Mapping[str, Any], production: bool) -> dict[str, Source]:
    out: dict[str, Source] = {}
    for tool, spec in tools.items():
        if tool != "*" and tool not in SOURCE_TOOLS:
            raise ValueError(f"no evidence source answers {tool!r}")
        spec = dict(spec)
        name = spec.pop("adapter")
        if name not in ADAPTERS:
            raise ValueError(f"unknown evidence adapter {name!r}")
        if production and name in LOCAL_ONLY:
            raise ValueError(f"evidence adapter {name!r} is for local use only")
        out[tool] = ADAPTERS[name](**spec)
    return out
