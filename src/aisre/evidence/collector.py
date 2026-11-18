"""Artifact 9.2, 9.4, 9.5, 13.5 T1.10: the Evidence Collector. No model call.

`collect` runs a plan of tool calls: the fixed baseline (9.2), or the requests a targeted round
carries, which their producing skill has already pre-checked. Each call goes through the
ToolGate under `collect_evidence`, then, in parallel, to the source configured for its tool
(aisre.evidence.sources). A repeat of an earlier call is answered from the evidence it stored,
not run again (9.4). Each result is:

1. scoped: the adapter keeps only the incident's service, environment and window (FR-44);
2. redacted: secrets and personal data out of every string, before anything is stored or shown
   (NFR-18, telemetry.redact_text);
3. capped: whole records dropped from the end until it fits the tool's output limit, and marked
   partial, as is a source that says its answer is incomplete (NFR-29);
4. fingerprinted: SHA-256 of the snapshot bytes (Q9.22);
5. summarised: a short extract, the only part the model sees (9.4);
6. given its strength tier, from facts computed against the change history (ADR-53,
   aisre.evidence.facts), with those facts kept as its reasons;
7. flagged when any of its text reads as instructions to the reader (validation.flag_injection,
   ST-33): the record stays readable, is shown flagged, and supports no claim (T1.12).

A source that fails, times out, has no adapter, or doesn't hold the data becomes a gap record
with its reason, never an empty or healthy one (FR-08, ADR-54). Nothing here touches the
database: the caller stores the outcome (aisre.record.evidence), so the counting and inserting
of tool calls happens in one transaction (ADR-69).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from aisre.agent.agent_contracts import Evidence, EvidenceKind
from aisre.agent.tool_registry import REGISTRY, ToolGate, ToolRefused
from aisre.agent.validation import flag_injection, flags_injection
from aisre.evidence.facts import ChangeInfo, Observed, change_info, tier_and_reasons
from aisre.evidence.sources import DEFAULT_TIMEOUT_S, Record, SourceSet, SourceUnavailable
from aisre.record.incidents import uuid7
from aisre.reliability.telemetry import redact_text

SKILL = "collect_evidence"
COLLECTOR = "evidence-collector"
SUMMARY_MAX = 4000
TOOL_KIND = {
    "get_alert": EvidenceKind.alert,
    "get_logs": EvidenceKind.log,
    "get_metric": EvidenceKind.metric,
    "get_changes": EvidenceKind.change,
    "get_traces": EvidenceKind.trace,
    "get_error_groups": EvidenceKind.error_group,
    "get_dependency_health": EvidenceKind.dependency,
    "search_runbooks": EvidenceKind.runbook,
    "search_past_incidents": EvidenceKind.past_incident,
}


@dataclass(frozen=True)
class Scope:
    """What the collector needs from the record: the incident, its service and environment, when
    it started, and the alert that opened it."""

    incident_id: str
    service: str
    environment: str
    started_at: datetime
    alert_id: str | None = None


@dataclass(frozen=True)
class Call:
    tool: str
    params: dict[str, Any]


def baseline_plan(scope: Scope) -> list[Call]:
    """9.2's fixed plan: alert, changes, 5xx rate, error rate by version, error logs, error
    groups, failing traces, dependency health. Windows are the gate's default: 60 minutes before
    the start up to now (FR-44)."""
    s = {"service": scope.service}
    return [
        Call("get_alert", s | ({"alert_id": scope.alert_id} if scope.alert_id else {})),
        Call("get_changes", s),
        Call("get_metric", s | {"metric": "http_5xx_rate"}),
        Call("get_metric", s | {"metric": "error_rate_by_version"}),
        Call("get_logs", s),
        Call("get_error_groups", s),
        Call("get_traces", s),
        Call("get_dependency_health", s | {"at": scope.started_at.isoformat()}),
    ]


@dataclass
class Collected:
    """One new evidence record, as it will be stored. `records` are the redacted, capped records
    the snapshot holds, kept here for the fact rules; they are never given to the model."""

    evidence: Evidence
    attributes: dict[str, Any]
    snapshot: bytes | None  # None for a gap
    records: list[Record] = field(default_factory=list)


@dataclass(frozen=True)
class CallRow:
    """One `tool_calls` row: a call the gate let through, and the evidence it produced or, for a
    repeat, the evidence an earlier identical call stored."""

    tool: str
    call_hash: str
    params: dict[str, Any]
    evidence_ids: tuple[str, ...]
    new_evidence: int  # gaps and repeats are not new evidence
    skill: str = SKILL


@dataclass
class Outcome:
    collected: list[Collected] = field(default_factory=list)
    calls: list[CallRow] = field(default_factory=list)
    refused: list[dict[str, Any]] = field(default_factory=list)

    @property
    def evidence_ids(self) -> list[str]:
        """Every ID the round answers with, new and repeated, in plan order, once each."""
        seen: dict[str, None] = {}
        for row in self.calls:
            seen.update(dict.fromkeys(row.evidence_ids))
        return list(seen)

    @property
    def gap_count(self) -> int:
        return sum(c.evidence.kind == EvidenceKind.gap for c in self.collected)


def redact(value: Any) -> Any:
    """Every string, at any depth, through the platform's redaction patterns."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    return value


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def collect(
    scope: Scope,
    calls: list[Call],
    gate: ToolGate,
    sources: SourceSet,
    *,
    collection_key: str,
    now: datetime,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    on_progress: Callable[[], None] | None = None,
    known_changes: Sequence[ChangeInfo] = (),
) -> Outcome:
    """Runs `calls` and returns what to store. Never raises for a source: a failure is a gap.
    `known_changes` is the change history earlier rounds stored, for the tier rules."""
    out = Outcome()
    pending: list[tuple[str, BaseModel, str, dict[str, Any]]] = []
    for call in calls:
        try:
            tool, params, call_hash = gate.check(call.tool, call.params, SKILL)
        except ToolRefused as e:
            out.refused.append({"tool": call.tool, "params": call.params, "refused": e.code})
            continue
        dumped = params.model_dump(mode="json")
        if any(h == call_hash for _, _, h, _ in pending):
            continue  # the same call twice in one plan runs once
        stored = gate.stored_result(call_hash) if gate.is_repeat(call_hash) else None
        if stored is not None:
            out.calls.append(CallRow(tool.name, call_hash, dumped, tuple(stored), 0))
            continue
        pending.append((tool.name, params, call_hash, dumped))

    async def run(tool: str, params: BaseModel, dumped: dict[str, Any]) -> Collected:
        source = sources.for_tool(tool)
        try:
            if source is None:
                raise SourceUnavailable(f"no source is configured for {tool}")
            result = await asyncio.wait_for(
                asyncio.to_thread(source.fetch, tool, params, scope.environment), timeout_s
            )
        except SourceUnavailable as e:
            return _gap(scope, tool, dumped, str(e), collection_key, now)
        except TimeoutError:
            reason = f"{tool}: no answer in {timeout_s:g} s"
            return _gap(scope, tool, dumped, reason, collection_key, now)
        finally:
            if on_progress is not None:
                on_progress()
        return _normalise(
            scope,
            tool,
            dumped,
            gate,
            collection_key,
            now,
            result.source_system,
            result.source_link,
            result.records,
            result.source_as_of,
            result.partial,
        )

    results = await asyncio.gather(*(run(t, p, d) for t, p, _, d in pending))
    assign_tiers(results, known_changes)
    for (name, _, call_hash, dumped), item in zip(pending, results, strict=True):
        out.collected.append(item)
        new = 0 if item.evidence.kind == EvidenceKind.gap else 1
        out.calls.append(CallRow(name, call_hash, dumped, (str(item.evidence.evidence_id),), new))
    return out


def assign_tiers(items: Sequence[Collected], known_changes: Sequence[ChangeInfo] = ()) -> None:
    """Sets each record's strength tier and `attributes.facts` (T1.11), and keeps each change
    record's ChangeInfo as `attributes.change` for later rounds."""
    observed = [
        Observed(c.evidence.kind, c.attributes.get("params", {}), c.records)
        for c in items
        if c.evidence.kind != EvidenceKind.gap
    ]
    changes = list(known_changes)
    for c in items:
        if c.evidence.kind == EvidenceKind.change:
            infos = [change_info(r) for r in c.records]
            c.attributes["change"] = [i.to_json() for i in infos]
            changes += infos
    for c, o in zip(
        [c for c in items if c.evidence.kind != EvidenceKind.gap], observed, strict=True
    ):
        tier, reasons = tier_and_reasons(o, changes, observed)
        c.evidence = c.evidence.model_copy(update={"strength_tier": tier})
        c.attributes["facts"] = reasons


def _window(params: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
    if "at" in params:
        at = datetime.fromisoformat(params["at"])
        return at, at
    if "start" in params:
        return datetime.fromisoformat(params["start"]), datetime.fromisoformat(params["end"])
    return None, None


def _gap(
    scope: Scope, tool: str, params: dict[str, Any], reason: str, key: str, now: datetime
) -> Collected:
    reason = redact_text(reason)
    start, end = _window(params)
    summary = f"{tool} returned nothing usable: {reason}. Not collected; not a sign of health."
    evidence = Evidence(
        evidence_id=uuid7(),
        incident_id=UUID(scope.incident_id),
        kind=EvidenceKind.gap,
        source_system=f"{tool.removeprefix('get_')} source",
        tool=tool,
        query=_canonical({"tool": tool, "params": params}),
        window_start=start,
        window_end=end,
        collected_at=now,
        source_as_of=None,
        source_link=None,
        summary=summary,
        content_sha256=_sha(summary.encode()),
        gap_reason=reason,
    )
    attributes = {"collection_key": key, "params": params, "missing_kind": TOOL_KIND[tool].value}
    return Collected(flag_injection(evidence), attributes, None)


def _normalise(
    scope: Scope,
    tool: str,
    params: dict[str, Any],
    gate: ToolGate,
    key: str,
    now: datetime,
    source_system: str,
    source_link: str,
    records: list[Record],
    source_as_of: datetime | None,
    partial: bool,
) -> Collected:
    records = redact(records)
    limit = REGISTRY[tool].max_output_bytes

    def body(rs: list[Record]) -> bytes:
        return _canonical({"tool": tool, "params": params, "records": rs}).encode()

    returned = len(records)
    snapshot = body(records)
    _, over = gate.cap_output(tool, snapshot)
    while over and records:  # drop whole records, so the snapshot stays readable
        records = records[:-1]
        snapshot = body(records)
        over = len(snapshot) > limit
    partial = partial or len(records) < returned
    start, end = _window(params)
    if tool == "get_alert" and records:  # point in time: the breach up to firing
        times = [records[0].get("breach_started_at"), records[0].get("fired_at")]
        start, end = (datetime.fromisoformat(t) if t else None for t in times)
    kind = TOOL_KIND[tool]
    evidence = Evidence(
        evidence_id=uuid7(),
        incident_id=UUID(scope.incident_id),
        kind=kind,
        source_system=source_system,
        tool=tool,
        query=_canonical({"tool": tool, "params": params}),
        window_start=start,
        window_end=end,
        collected_at=now,
        source_as_of=source_as_of,
        source_link=source_link,
        summary=summarise(tool, params, records),
        content_sha256=_sha(snapshot),
        is_partial=partial,
    )
    attributes = {
        "collection_key": key,
        "params": params,
        "record_count": len(records),
        "terms": search_terms(kind, records),
    }
    evidence = flag_injection(evidence)  # the extract
    if not evidence.flagged_injection and any(flags_injection(t) for t in _texts(records)):
        evidence = evidence.model_copy(update={"flagged_injection": True})  # the full records
    return Collected(evidence, attributes, snapshot, records)


def _texts(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _texts(v)
    elif isinstance(value, list):
        for v in value:
            yield from _texts(v)


# ---------------------------------------------------------------------------
# Extracts: what the model sees of each record (9.4), and search terms for retrieval
# ---------------------------------------------------------------------------
def _clip(text: str) -> str:
    return text if len(text) <= SUMMARY_MAX else text[: SUMMARY_MAX - 1] + "…"


def _hhmm(stamp: str | None) -> str:
    return (stamp or "")[11:16]


def _top_frames(stack: str | None, n: int = 2) -> str:
    lines = [ln.strip() for ln in (stack or "").splitlines() if ln.strip()]
    return " | ".join(lines[-1 - 2 * n : -1]) if len(lines) > 1 else ""


def summarise(tool: str, params: dict[str, Any], records: list[Record]) -> str:
    """A short, redacted extract. Deterministic, so the same records give the same text."""
    if not records:
        return _clip(f"{tool}: no records in the window for {params.get('service')}.")
    lines: list[str] = []
    if tool == "get_alert":
        a = records[0]
        lines.append(
            f"Alert {a['alert_name']} ({a['alert_id']}) {a['state']} on "
            f"{a['service']}/{a['environment']}: value {a['value']} against threshold "
            f"{a['threshold']}; breach from {a['breach_started_at']}, fired {a['fired_at']}. "
            f"Rule: {a['rule']}"
        )
    elif tool == "get_metric":
        lines.append(f"Metric {params['metric']}, {len(records)} points, UTC minute: value.")
        for p in records:
            if "versions" in p:
                parts = [
                    f"v{v} {d['rate'] if d['rate'] is not None else '-'} ({d['requests']:g})"
                    for v, d in p["versions"].items()
                    if d["requests"]
                ]
                lines.append(f"{_hhmm(p['at'])} " + "; ".join(parts))
            else:
                req = f" ({p['requests']:g} req)" if p.get("requests") is not None else ""
                lines.append(f"{_hhmm(p['at'])} {p['value']}{req}")
    elif tool == "get_logs":
        groups: dict[tuple[Any, ...], list[Record]] = {}
        for r in records:
            what = r.get("error_type") or r.get("message")
            sig = (r.get("level"), what, r.get("error_message"), r.get("path"))
            groups.setdefault(sig, []).append(r)
        lines.append(f"{len(records)} log lines in {len(groups)} groups.")
        for (level, what, msg, path), rs in groups.items():
            versions = ",".join(sorted({str(r.get("version")) for r in rs}))
            lines.append(
                f"{len(rs)}x {level} {what}{': ' + msg if msg else ''} on {path} "
                f"[versions {versions}] {rs[0].get('ts')} to {rs[-1].get('ts')}. "
                f"{_top_frames(rs[0].get('stack'))}".strip()
            )
    elif tool == "get_traces":
        for t in records:
            lines.append(f"Trace {t['trace_id']}:")
            for s in t["spans"]:
                if s["status"] != "ERROR":
                    continue
                exc = "; ".join(f"{e['type']}: {e['message']}" for e in s["exceptions"])
                lines.append(f"{s['name']} ERROR {_canonical(s['attributes'])} {exc}".strip())
    elif tool == "get_changes":
        for c in records:
            keys = "; ".join(f"{k['key']} {k['old']} -> {k['new']}" for k in c["config_changes"])
            rollout = ", ".join(f"{r['traffic_pct']}% at {r['at']}" for r in c["rollout"])
            commits = "; ".join(f"{m['sha']} {m['message']}" for m in c.get("commits", []))
            lines.append(
                f"Change {c['change_id']} ({c['previous_version']} -> {c['version']}), code "
                f"changed: {c['code_changed']}. Config: {keys or 'none'} (revision "
                f"{c['config_revision']}). Files: {', '.join(c['files']) or 'none'}. "
                f"Commits: {commits or 'none'}. Rollout: {rollout or 'none'}."
            )
    elif tool == "get_error_groups":
        for g in records:
            frame = f" at {g['frames'][-1][0]}:{g['frames'][-1][1]}" if g["frames"] else ""
            lines.append(
                f"{g['type']}: {g['message']} - {g['count']} seen, first {g['first_seen']}, "
                f"last {g['last_seen']}, versions {','.join(g['versions'])}{frame}"
            )
    elif tool == "get_dependency_health":
        for d in records:
            lines.append(
                f"{d['name']} ({d['kind']}): {d['status']} at {d['observed_at']}. {d['detail']}"
            )
    return _clip("\n".join(lines))


def search_terms(kind: EvidenceKind, records: list[Record]) -> list[str]:
    """Identifiers the Knowledge Retriever searches for (KnowledgeIn: code builds the terms)."""
    terms: list[str] = []
    for r in records:
        if kind == EvidenceKind.alert:
            terms.append(r["alert_name"])
        elif kind in (EvidenceKind.log, EvidenceKind.error_group):
            terms.append(r.get("error_type") or r.get("type") or "")
        elif kind == EvidenceKind.change:
            terms += [c["key"] for c in r["config_changes"]]
        elif kind == EvidenceKind.trace:
            for s in r["spans"]:
                terms += [e["type"] for e in s["exceptions"]]
                read = s["attributes"].get("app.config.read")
                terms += [read] if read else []
    return list(dict.fromkeys(t for t in terms if t))
