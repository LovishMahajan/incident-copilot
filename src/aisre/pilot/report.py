"""Artifact 13.5 T5.2, T5.3 (Artifact 1.6, ADR-65, ADR-75): the baseline and the pilot report.

Built from incident records only, read as `aisre_reporting` (SELECT on what it reads, migration
0013), for incidents whose alert arrived in `[start, end)`. Every rate carries its denominator
and a 95% interval (12.9); nothing missing is dropped silently: an incident without a review
counts as "not reviewed", never as confirmed.

Measures, pilot report (T5.3):

    accuracy            proposed cause confirmed by the incident review / proposed and reviewed
                        (Artifact 1.6, target 80%), all and change-caused only
    false_established   proposed and the review did not confirm it / proposed and reviewed
                        (ADR-65, at most 2%)
    under_escalation    a cause proposed where the review says the evidence didn't support one
                        and the platform didn't hand over / incidents where it should have
                        handed over (ADR-65, below 2%; "handed over" is the harness's:
                        inconclusive, or no action proposed)
    time_to_cause       alert (the incident's creation) to the first diagnosis naming a cause the
                        review confirmed; and alert to the responders' identified cause, from
                        the review. Change-caused incidents only, as the baseline is (1.6)
    first_summary       alert to the first hypothesis (ADR-65: p95 at most 300 s)
    approvals           requests, decisions, expiries, refusals by code, time to decide
    hand_overs          hand-over events by reason (the text before the first colon)
    cost                model spend per incident (incident_budgets), mean and median
    engineer_hours, fixes_reversed  from the review (1.6, against the baseline)

Baseline (T5.2): Artifact 1.6's four relative targets' baselines: median time from alert to
identified cause, median engineer hours and the share of fixes reversed, change-caused only,
from the first `first` reviewed change-caused incidents in the range (the 30-incident
fallback), or from a JSON file of earlier incident records. Time to verified recovery is
Release B's and is not computed.

`label` is "pilot" only when the report runs with `AISRE_ENV=production` and no approval in
range was signed with a development key; otherwise "synthetic", and the Markdown says so.
"""

from __future__ import annotations

import json
import statistics
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, text

from aisre.evaluation.eval_harness import Rate, percentile, wilson

CHANGE_CAUSED = frozenset({"deployment_code", "deployment_config"})
HANDED_OVER = "aisre.incident.handed_over.v1"
FIRST_SUMMARY = "aisre.hypothesis.created.v1"
REFUSED = "aisre.approval.refused.v1"
REPORTING_ROLE = "aisre_reporting"
BASELINE_FALLBACK = 30  # Artifact 1.6: the first 30 pilot incidents, provisional


@dataclass(frozen=True)
class Review:
    cause_category: str
    cause_identified_at: datetime
    proposed_cause_confirmed: bool | None
    should_have_handed_over: bool
    engineer_hours: float | None = None
    fix_reversed: bool | None = None

    @property
    def change_caused(self) -> bool:
        return self.cause_category in CHANGE_CAUSED


@dataclass(frozen=True)
class IncidentRecord:
    incident_id: str
    service: str
    alert_at: datetime
    outcome: str | None  # the current diagnosis, as the APIs show it; None: no diagnosis
    first_summary_at: datetime | None = None
    named_cause_at: datetime | None = None  # the first diagnosis that named a cause
    action_proposed: bool = False
    cost_micros: int = 0
    hand_overs: tuple[str, ...] = ()
    review: Review | None = None

    @property
    def named(self) -> bool:
        return self.outcome == "established"


@dataclass(frozen=True)
class ApprovalRecord:
    requested_at: datetime
    close_reason: str | None
    decisions: tuple[tuple[str, datetime], ...] = ()  # (decision, decided_at)
    development_key: bool = False


@dataclass
class Records:
    incidents: list[IncidentRecord]
    approvals: list[ApprovalRecord] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)  # each refused attempt's code


# ---------------------------------------------------------------- reading
_INCIDENTS = """
SELECT i.incident_id::text, s.slug, i.created_at,
  (SELECT COALESCE(d.final_outcome, d.outcome)::text FROM diagnoses d
    WHERE d.incident_id = i.incident_id ORDER BY d.revision DESC LIMIT 1),
  (SELECT min(t.occurred_at) FROM timeline_events t
    WHERE t.incident_id = i.incident_id AND t.type = :first_summary),
  (SELECT min(d.created_at) FROM diagnoses d WHERE d.incident_id = i.incident_id
    AND COALESCE(d.final_outcome, d.outcome) = 'established'),
  EXISTS (SELECT 1 FROM actions a WHERE a.incident_id = i.incident_id AND a.kind <> 'rollback'),
  COALESCE((SELECT b.spent_cost_micros FROM incident_budgets b
    WHERE b.incident_id = i.incident_id), 0),
  ARRAY(SELECT t.data->>'reason' FROM timeline_events t
    WHERE t.incident_id = i.incident_id AND t.type = :handed_over ORDER BY t.seq),
  r.cause_category::text, r.cause_identified_at, r.proposed_cause_confirmed,
  r.should_have_handed_over, r.engineer_hours, r.fix_reversed
FROM incidents i
JOIN services s ON s.service_id = i.service_id
LEFT JOIN incident_reviews r ON r.incident_id = i.incident_id
WHERE i.created_at >= :start AND i.created_at < :end
ORDER BY i.created_at, i.incident_id
"""
_APPROVALS = """
SELECT q.requested_at, q.close_reason,
  ARRAY(SELECT d.decision::text FROM approval_decisions d
    WHERE d.approval_request_id = q.approval_request_id ORDER BY d.decided_at),
  ARRAY(SELECT d.decided_at FROM approval_decisions d
    WHERE d.approval_request_id = q.approval_request_id ORDER BY d.decided_at),
  EXISTS (SELECT 1 FROM approval_decisions d WHERE d.approval_request_id = q.approval_request_id
    AND d.signing_key_id LIKE 'development-%')
FROM approval_requests q
JOIN actions a ON a.action_id = q.action_id
JOIN incidents i ON i.incident_id = a.incident_id
WHERE i.created_at >= :start AND i.created_at < :end
ORDER BY q.requested_at
"""
_REFUSALS = """
SELECT t.data->>'code' FROM timeline_events t JOIN incidents i ON i.incident_id = t.incident_id
WHERE t.type = :refused AND i.created_at >= :start AND i.created_at < :end ORDER BY t.recorded_at
"""


class NotReadOnly(RuntimeError):
    """The report connects only as the reporting role."""


def read(conn: Connection, start: datetime, end: datetime) -> Records:
    role = conn.execute(text("SELECT current_user")).scalar()
    if role != REPORTING_ROLE:
        raise NotReadOnly(f"connect as {REPORTING_ROLE} (AISRE_REPORTING_DATABASE_URL)")
    window = {"start": start, "end": end}
    incidents = []
    for r in conn.execute(
        text(_INCIDENTS), {**window, "first_summary": FIRST_SUMMARY, "handed_over": HANDED_OVER}
    ):
        review = (
            Review(r[9], r[10], r[11], r[12], float(r[13]) if r[13] is not None else None, r[14])
            if r[9] is not None
            else None
        )
        incidents.append(
            IncidentRecord(r[0], r[1], r[2], r[3], r[4], r[5], r[6], int(r[7]), tuple(r[8]), review)
        )
    approvals = [
        ApprovalRecord(r[0], r[1], tuple(zip(r[2], r[3], strict=True)), r[4])
        for r in conn.execute(text(_APPROVALS), window)
    ]
    refusals = [
        str(c) for c in conn.execute(text(_REFUSALS), {**window, "refused": REFUSED}).scalars()
    ]
    return Records(incidents, approvals, refusals)


# ---------------------------------------------------------------- measures
def rate(k: int, n: int) -> dict[str, Any]:
    lo, hi = wilson(k, n)
    return {
        "k": k,
        "n": n,
        "value": round(k / n, 4) if n else None,
        "ci95": [round(lo, 4), round(hi, 4)] if n else None,
        "text": str(Rate(k, n)),
    }


def seconds(xs: Iterable[float]) -> dict[str, Any]:
    values = sorted(xs)
    return {
        "n": len(values),
        "median_s": statistics.median(values) if values else None,
        "p95_s": percentile(values, 95),
    }


def _since(a: datetime, b: datetime | None) -> float | None:
    return (b - a).total_seconds() if b is not None else None


def is_pilot(environ: Mapping[str, str], records: Records) -> bool:
    return environ.get("AISRE_ENV") == "production" and not any(
        a.development_key for a in records.approvals
    )


def pilot_report(records: Records, *, label: str, start: datetime, end: datetime) -> dict[str, Any]:
    inc = records.incidents
    reviewed = [i for i in inc if i.review is not None]
    proposed = [i for i in reviewed if i.named]
    change = [i for i in proposed if i.review and i.review.change_caused]
    confirmed = [i for i in proposed if i.review and i.review.proposed_cause_confirmed]
    should = [i for i in reviewed if i.review and i.review.should_have_handed_over]
    under = [i for i in should if i.named and i.action_proposed]
    platform_ttc = [
        t
        for i in confirmed
        if i.review and i.review.change_caused
        if (t := _since(i.alert_at, i.named_cause_at)) is not None
    ]
    responders_ttc = [
        (i.review.cause_identified_at - i.alert_at).total_seconds()
        for i in reviewed
        if i.review and i.review.change_caused
    ]
    first = [t for i in inc if (t := _since(i.alert_at, i.first_summary_at)) is not None]
    costs = [i.cost_micros for i in inc]
    hours = [i.review.engineer_hours for i in reviewed if i.review and i.review.engineer_hours]
    fixes = [
        i.review.fix_reversed for i in reviewed if i.review and i.review.fix_reversed is not None
    ]
    reasons = Counter(r.split(":", 1)[0].strip() for i in inc for r in i.hand_overs if r)
    decisions = Counter(d for a in records.approvals for d, _ in a.decisions)
    waits = [
        (a.decisions[0][1] - a.requested_at).total_seconds()
        for a in records.approvals
        if a.decisions
    ]
    return {
        "report": "pilot",
        "label": label,
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "incidents": len(inc),
        "by_service": dict(sorted(Counter(i.service for i in inc).items())),
        "reviewed": len(reviewed),
        "proposed_not_reviewed": sum(1 for i in inc if i.named and i.review is None),
        "accuracy": rate(len(confirmed), len(proposed)),
        "accuracy_change_caused": rate(
            sum(1 for i in change if i.review and i.review.proposed_cause_confirmed), len(change)
        ),
        "false_established": rate(len(proposed) - len(confirmed), len(proposed)),
        "under_escalation": rate(len(under), len(should)),
        "time_to_cause": {
            "platform": seconds(platform_ttc),
            "responders": seconds(responders_ttc),
        },
        "first_summary": seconds(first),
        "approvals": {
            "requests": len(records.approvals),
            "decisions": dict(sorted(decisions.items())),
            "expired": sum(1 for a in records.approvals if a.close_reason == "expired"),
            "undecided": sum(1 for a in records.approvals if not a.decisions),
            "refusals": dict(sorted(Counter(records.refusals).items())),
            "time_to_first_decision": seconds(waits),
        },
        "hand_overs": dict(sorted(reasons.items())),
        "cost": {
            "incidents": len(costs),
            "mean_usd": round(statistics.mean(costs) / 1e6, 4) if costs else None,
            "median_usd": round(statistics.median(costs) / 1e6, 4) if costs else None,
            "total_usd": round(sum(costs) / 1e6, 4),
        },
        "engineer_hours": {
            "n": len(hours),
            "median": statistics.median(hours) if hours else None,
        },
        "fixes_reversed": rate(sum(fixes), len(fixes)),
    }


@dataclass(frozen=True)
class EarlierIncident:
    """One incident from the records before the pilot (a baseline input file's row)."""

    alert_at: datetime
    cause_identified_at: datetime
    change_caused: bool
    engineer_hours: float | None = None
    fix_reversed: bool | None = None


def earlier_from_json(rows: Sequence[Mapping[str, Any]]) -> list[EarlierIncident]:
    return [
        EarlierIncident(
            datetime.fromisoformat(r["alert_at"]),
            datetime.fromisoformat(r["cause_identified_at"]),
            bool(r["change_caused"]),
            r.get("engineer_hours"),
            r.get("fix_reversed"),
        )
        for r in rows
    ]


def from_pilot(records: Records, first: int = BASELINE_FALLBACK) -> list[EarlierIncident]:
    """The first `first` reviewed change-caused incidents, in alert order."""
    rows = [
        EarlierIncident(
            i.alert_at,
            i.review.cause_identified_at,
            True,
            i.review.engineer_hours,
            i.review.fix_reversed,
        )
        for i in records.incidents
        if i.review is not None and i.review.change_caused
    ]
    return rows[:first]


def baseline_report(
    rows: Sequence[EarlierIncident], *, label: str, source: str, wanted: int | None = None
) -> dict[str, Any]:
    change = [r for r in rows if r.change_caused]
    hours = [r.engineer_hours for r in change if r.engineer_hours is not None]
    fixes = [r.fix_reversed for r in change if r.fix_reversed is not None]
    ttc = [(r.cause_identified_at - r.alert_at).total_seconds() for r in change]
    return {
        "report": "baseline",
        "label": label,
        "source": source,
        "change_caused_incidents": len(change),
        "complete": wanted is None or len(change) >= wanted,
        "wanted": wanted,
        "time_to_identified_cause": seconds(ttc),
        "engineer_hours": {"n": len(hours), "median": statistics.median(hours) if hours else None},
        "fixes_reversed": rate(sum(fixes), len(fixes)),
        "time_to_verified_recovery": "Release B: not measured in Release A",
    }


# ---------------------------------------------------------------- output
def to_json(report: Mapping[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=False, default=str)


def _s(m: Mapping[str, Any]) -> str:
    if not m["n"]:
        return "none (0 incidents)"
    p95 = f", p95 {m['p95_s']:.0f} s" if m.get("p95_s") is not None else ""
    return f"median {m['median_s']:.0f} s{p95} (n = {m['n']})"


def _counts(m: Mapping[str, int]) -> str:
    return ", ".join(f"{k} {v}" for k, v in m.items()) or "none"


def _banner(report: Mapping[str, Any]) -> list[str]:
    if report["label"] == "pilot":
        return ["Pilot data: incidents handled in production by the advisory pilot.", ""]
    return [
        "> **SYNTHETIC.** Not pilot data: these records come from a development or test run",
        "> (not `AISRE_ENV=production`, or approvals signed with a development key). They show",
        "> the report works, and say nothing about the platform's accuracy.",
        "",
    ]


def to_markdown(report: Mapping[str, Any]) -> str:
    if report["report"] == "baseline":
        return _baseline_markdown(report)
    r = report
    a = r["approvals"]
    lines = [
        f"# Pilot report ({r['label']})",
        "",
        *_banner(r),
        f"Incidents with the alert in [{r['range']['start']}, {r['range']['end']}): "
        f"{r['incidents']}; reviewed {r['reviewed']}; "
        f"a cause proposed but not yet reviewed {r['proposed_not_reviewed']}.",
        "",
        "| Measure | Result | Target |",
        "| --- | --- | --- |",
        f"| Proposed cause confirmed by the incident review | {r['accuracy']['text']} | 80% or more (1.6) |",
        f"| The same, change-caused incidents | {r['accuracy_change_caused']['text']} | 80% or more |",
        f"| False-established | {r['false_established']['text']} | at most 2% (ADR-65) |",
        f"| Under-escalation | {r['under_escalation']['text']} | below 2% (ADR-65) |",
        f"| Alert to confirmed cause, platform (change-caused) | {_s(r['time_to_cause']['platform'])} | 50% below the baseline (1.6) |",
        f"| Alert to identified cause, responders (change-caused) | {_s(r['time_to_cause']['responders'])} | for comparison |",
        f"| Alert to first summary | {_s(r['first_summary'])} | p95 at most 300 s (ADR-65) |",
        f"| Model cost per incident | mean {r['cost']['mean_usd']} USD, median {r['cost']['median_usd']} USD | at most 1.2x the baseline (ADR-65) |",
        f"| Engineer hours per incident | median {r['engineer_hours']['median']} (n = {r['engineer_hours']['n']}) | below the baseline (1.6) |",
        f"| Fixes later reversed | {r['fixes_reversed']['text']} | below the baseline (1.6) |",
        "",
        "## Approvals",
        "",
        f"Requests {a['requests']}; decisions: {_counts(a['decisions'])}; expired {a['expired']}; "
        f"undecided {a['undecided']}; refused attempts: {_counts(a['refusals'])}; "
        f"time to first decision {_s(a['time_to_first_decision'])}. In Release A no fix is "
        "sent for approval (ADR-44), so a pilot shows none.",
        "",
        "## Hand-overs by reason",
        "",
        *([f"- {k}: {v}" for k, v in r["hand_overs"].items()] or ["- none"]),
        "",
        "## By service",
        "",
        *([f"- {k}: {v}" for k, v in r["by_service"].items()] or ["- none"]),
        "",
    ]
    return "\n".join(lines)


def _baseline_markdown(r: Mapping[str, Any]) -> str:
    done = "" if r["complete"] else f" Incomplete: {r['wanted']} are needed."
    return "\n".join(
        [
            f"# Baselines ({r['label']})",
            "",
            *_banner(r),
            f"From {r['source']}: {r['change_caused_incidents']} change-caused incidents.{done}",
            "",
            "| Baseline (Artifact 1.6) | Value |",
            "| --- | --- |",
            f"| Alert to identified cause | {_s(r['time_to_identified_cause'])} |",
            f"| Engineer hours per incident | median {r['engineer_hours']['median']} (n = {r['engineer_hours']['n']}) |",
            f"| Fixes later reversed | {r['fixes_reversed']['text']} |",
            f"| Alert to verified recovery | {r['time_to_verified_recovery']} |",
            "",
        ]
    )
