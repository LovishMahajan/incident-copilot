"""Artifact 13.5 T5.4, 13.9 D3 (ADR-75): the approval review, from the record.

For each approval request whose incident's alert arrived in `[start, end)`: what the approver
was shown (FR-25: the diff, the evidence, the risk and the flagged items, as
`GET /v1/actions/{id}/approval-request` assembles them) and how long each decision took. Read
as `aisre_reporting`. The diff itself is in the object store, out of this role's reach: the
review names its artifact, hash and size, to open through `GET /v1/artifacts/{id}/content`.

Demo D3 asks a person whether diff, evidence, risk and flagged items can be judged in the time.
That judgement is the approver's: the Markdown leaves a place for it under each request, and
the tooling never fills it in. In Release A no fix is sent for approval (ADR-44), so a pilot
has none to review until Release B; on a test target (D3 "Release A on a test target") the
records are synthetic and the output says so.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, text

from aisre.pilot.report import REPORTING_ROLE, NotReadOnly

_REQUESTS = """
SELECT q.approval_request_id::text, q.action_id::text, i.display_ref, s.slug, a.kind::text,
  a.description, a.risk, a.undo_plan, a.high_impact, q.approvers_needed, q.requested_at,
  q.expires_at, q.close_reason,
  p.files_changed, p.lines_added, p.lines_removed, p.patch_sha256, p.review_ref,
  p.diff_artifact_id::text,
  (SELECT t.outcome::text || CASE WHEN t.reproduced THEN ', reproduced' ELSE ', not reproduced'
    END FROM test_runs t WHERE t.action_id = a.action_id ORDER BY t.finished_at DESC LIMIT 1),
  d.summary, COALESCE(d.final_outcome, d.outcome)::text, COALESCE(d.final_confidence,
  d.confidence)::text, d.claims, a.diagnosis_id::text
FROM approval_requests q
JOIN actions a ON a.action_id = q.action_id
JOIN incidents i ON i.incident_id = a.incident_id
JOIN services s ON s.service_id = i.service_id
JOIN diagnoses d ON d.diagnosis_id = a.diagnosis_id
LEFT JOIN patches p ON p.action_id = a.action_id
WHERE i.created_at >= :start AND i.created_at < :end
ORDER BY q.requested_at
"""
_EVIDENCE = """
SELECT e.evidence_id::text, e.kind::text, e.strength_tier, e.flagged_injection, e.is_partial,
  e.gap_reason
FROM evidence e WHERE e.evidence_id = ANY(CAST(:ids AS uuid[]))
"""
_LEADING = """
SELECT l.evidence_id::text, l.relation::text FROM hypothesis_evidence l
JOIN diagnoses d ON d.leading_hypothesis_id = l.hypothesis_id
WHERE d.diagnosis_id = CAST(:d AS uuid)
"""
_DECISIONS = """
SELECT decision::text, decided_by_role, decided_at, reason, signing_key_id
FROM approval_decisions WHERE approval_request_id = CAST(:r AS uuid) ORDER BY decided_at
"""


@dataclass(frozen=True)
class Decision:
    decision: str
    role: str
    decided_at: str
    seconds_after_request: float
    reason: str | None
    development_key: bool


@dataclass(frozen=True)
class Reviewed:
    approval_request_id: str
    action_id: str
    incident: str
    service: str
    kind: str
    description: str
    risk: str
    undo_plan: str
    high_impact: bool
    approvers_needed: int
    requested_at: str
    expires_at: str
    close_reason: str | None
    diff: dict[str, Any] | None  # stats, hash, review reference, artifact ID
    test: str | None
    diagnosis: dict[str, Any]
    evidence: dict[str, Any]  # what the diagnosis cites, by kind, with what is flagged
    decisions: list[Decision]


def _iso(t: datetime) -> str:
    return t.isoformat()


def read(conn: Connection, start: datetime, end: datetime) -> list[Reviewed]:
    if conn.execute(text("SELECT current_user")).scalar() != REPORTING_ROLE:
        raise NotReadOnly(f"connect as {REPORTING_ROLE} (AISRE_REPORTING_DATABASE_URL)")
    out = []
    for r in conn.execute(text(_REQUESTS), {"start": start, "end": end}):
        links = list(conn.execute(text(_LEADING), {"d": r[24]}))
        claimed = {e for c in (r[23] or []) for e in c.get("evidence_ids", [])}
        cited = sorted(claimed | {e for e, _ in links})
        ev = list(conn.execute(text(_EVIDENCE), {"ids": cited}))
        contradicting = sum(1 for _, rel in links if rel == "contradicts")
        kinds: dict[str, int] = {}
        for e in ev:
            kinds[e[1]] = kinds.get(e[1], 0) + 1
        requested: datetime = r[10]
        decisions = [
            Decision(
                d[0],
                d[1],
                _iso(d[2]),
                (d[2] - requested).total_seconds(),
                d[3],
                (d[4] or "").startswith("development-"),
            )
            for d in conn.execute(text(_DECISIONS), {"r": r[0]})
        ]
        out.append(
            Reviewed(
                approval_request_id=r[0],
                action_id=r[1],
                incident=r[2],
                service=r[3],
                kind=r[4],
                description=r[5],
                risk=r[6],
                undo_plan=r[7],
                high_impact=r[8],
                approvers_needed=r[9],
                requested_at=_iso(requested),
                expires_at=_iso(r[11]),
                close_reason=r[12],
                diff=None
                if r[16] is None
                else {
                    "files_changed": r[13],
                    "lines_added": r[14],
                    "lines_removed": r[15],
                    "patch_sha256": r[16],
                    "review_ref": r[17],
                    "artifact_id": r[18],
                },
                test=r[19],
                diagnosis={"summary": r[20], "outcome": r[21], "confidence": r[22]},
                evidence={
                    "cited": len(cited),
                    "found": len(ev),
                    "by_kind": dict(sorted(kinds.items())),
                    "flagged_injection": sum(1 for e in ev if e[3]),
                    "partial": sum(1 for e in ev if e[4]),
                    "gaps": sum(1 for e in ev if e[5] is not None),
                    "contradicting_leading_cause": int(contradicting),
                },
                decisions=decisions,
            )
        )
    return out


def report(rows: list[Reviewed], *, label: str, start: datetime, end: datetime) -> dict[str, Any]:
    firsts = [r.decisions[0].seconds_after_request for r in rows if r.decisions]
    return {
        "report": "approval_review",
        "label": label,
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "requests": len(rows),
        "decided": len(firsts),
        "seconds_to_first_decision": sorted(firsts),
        "items": [asdict(r) for r in rows],
    }


def to_json(rep: Mapping[str, Any]) -> str:
    return json.dumps(rep, indent=2, default=str)


def to_markdown(rep: Mapping[str, Any]) -> str:
    lines = [f"# Approval review, Demo D3 ({rep['label']})", ""]
    if rep["label"] != "pilot":
        lines += [
            "> **SYNTHETIC.** Not pilot data: a development or test run, or approvals signed",
            "> with a development key. It shows what an approver is shown, not a real request.",
            "",
        ]
    lines += [
        f"Approval requests for incidents with the alert in [{rep['range']['start']}, "
        f"{rep['range']['end']}): {rep['requests']}, of which {rep['decided']} decided.",
        "",
    ]
    for item in rep["items"]:
        d, ev = item["diff"], item["evidence"]
        diff = (
            f"{d['files_changed']} file(s), +{d['lines_added']} -{d['lines_removed']}, "
            f"sha256 {d['patch_sha256'][:12]}, review {d['review_ref'] or 'none'}, "
            f"artifact {d['artifact_id']}"
            if d
            else "no patch"
        )
        lines += [
            f"## {item['incident']} ({item['service']}): {item['kind']}",
            "",
            f"- Requested {item['requested_at']}, expires {item['expires_at']}, approvers "
            f"needed {item['approvers_needed']}{', high impact' if item['high_impact'] else ''}; "
            f"closed: {item['close_reason'] or 'open'}",
            f"- Change: {item['description']}",
            f"- Diff: {diff}",
            f"- Test: {item['test'] or 'none recorded'}",
            f"- Diagnosis ({item['diagnosis']['outcome']}, {item['diagnosis']['confidence']}): "
            f"{item['diagnosis']['summary']}",
            f"- Evidence cited: {ev['cited']} ({ev['by_kind'] or 'none'}); flagged as injection "
            f"{ev['flagged_injection']}, partial {ev['partial']}, gaps {ev['gaps']}, "
            f"contradicting the leading cause {ev['contradicting_leading_cause']}",
            f"- Risk: {item['risk']}",
            f"- Undo: {item['undo_plan']}",
        ]
        for dec in item["decisions"]:
            key = ", development key" if dec["development_key"] else ""
            lines.append(
                f"- Decision: {dec['decision']} by {dec['role']} at {dec['decided_at']}, "
                f"{dec['seconds_after_request']:.0f} s after the request{key}"
                + (f": {dec['reason']}" if dec["reason"] else "")
            )
        if not item["decisions"]:
            lines.append("- Decision: none")
        lines += [
            "",
            "Approver's judgement (D3, filled in by the approver, not the tooling): could the "
            "diff, evidence, risk and flagged items be judged in the time? What was missing?",
            "",
            "> _not yet recorded_",
            "",
        ]
    return "\n".join(lines)
