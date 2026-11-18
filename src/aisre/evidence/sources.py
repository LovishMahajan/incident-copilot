"""Artifact 9.4, 13.5 T1.10: the read-only source adapters behind the READ tools.

An adapter holds the source's address (and, for a real source, its narrow credential, never
shown to the model: ST-17) and answers one tool call with records in the platform's own shape,
below, so the collector and the fact rules (aisre.evidence.facts) never see a vendor's format.
Which adapter answers which tool is configuration (`SourceSet.from_config`): a real source is
one more class in ADAPTERS and one more entry in the configuration, nothing else.

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

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlencode

from pydantic import BaseModel

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
    """One configured source. `params` is the ToolGate's validated parameter model."""

    def fetch(self, tool: str, params: BaseModel, environment: str) -> SourceResult: ...


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
# Configuration: which adapter answers which tool
# ---------------------------------------------------------------------------
ADAPTERS: dict[str, Callable[..., Source]] = {"fake_monitoring": FakeMonitoringSource}
LOCAL_ONLY = {"fake_monitoring"}


@dataclass(frozen=True)
class SourceSet:
    """Tool name -> source; `"*"` answers any tool not named. `replay_as_of` is for replaying a
    recorded scenario only: the collector treats it as "now", so data after the scenario's
    evidence cut-off stays hidden (tests/fixtures/reference-incident/README.md)."""

    sources: Mapping[str, Source]
    replay_as_of: datetime | None = None

    def for_tool(self, tool: str) -> Source | None:
        return self.sources.get(tool) or self.sources.get("*")

    @classmethod
    def from_config(cls, config: Mapping[str, Any], *, production: bool = False) -> SourceSet:
        """`{"sources": {"*" | tool: {"adapter": name, ...its arguments}}, "replay_as_of": ...}`.
        In production, local-only adapters and replay are refused."""
        sources: dict[str, Source] = {}
        for tool, spec in config.get("sources", {}).items():
            spec = dict(spec)
            name = spec.pop("adapter")
            if name not in ADAPTERS:
                raise ValueError(f"unknown evidence adapter {name!r}")
            if production and name in LOCAL_ONLY:
                raise ValueError(f"evidence adapter {name!r} is for local use only")
            sources[tool] = ADAPTERS[name](**spec)
        replay = config.get("replay_as_of")
        if production and replay:
            raise ValueError("replay_as_of is for replaying scenarios, never production")
        return cls(sources, parse_time(replay) if replay else None)
