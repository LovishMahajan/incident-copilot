"""Artifact 11.12, 11.13: verification predicates and the rollback strategy.

The Recovery Verifier reads monitoring directly and evaluates predicates fixed before the change.
Every predicate must hold at every check across the whole observation window, with enough traffic
to judge. Otherwise the result is failed or inconclusive — never 'probably fine'.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Literal, Optional

from aisre.remediation.action import ImmutableAction

ERROR_RATE_FLOOR = 0.001                 # 0.1%: a limit of 1.1 × 0 would fail on one error (ADR-66)


@dataclass(frozen=True)
class Predicate:
    """One measurable recovery condition (Q11.81)."""

    name: str
    metric: str                          # e.g. http_5xx_rate{service,path}
    comparison: Literal["<=", ">="]
    threshold: float
    relative_to_baseline: bool = False   # threshold is a multiple of the pre-incident baseline
    required: bool = True
    floor: float = 0.0                   # a limit never goes below this


@dataclass(frozen=True)
class VerificationPlan:
    """Fixed before execution; its digest is part of the immutable action."""

    service: str
    path: str                            # the originally failing path (Q11.83)
    predicates: tuple[Predicate, ...]
    window_s: int = 600                  # Q11.86: must hold for the whole window; the action's hold_seconds
    interval_s: int = 60
    min_requests_per_interval: int = 50  # Q11.88: below this, there's too little traffic to judge
    synthetic_probe: bool = True         # Q11.82, Q11.83: replay the failing request against the service
    baseline: dict[str, float] = field(default_factory=dict)   # recorded here so the digest covers it (ADR-66)

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


def reference_plan(service: str, path: str, baseline: Optional[dict[str, float]] = None,
                   hold_seconds: int = 600) -> VerificationPlan:
    """The plan for the reference scenario (HTTP 500 after a configuration change). `hold_seconds`
    is the action's; `baseline` comes from baseline() below."""
    return VerificationPlan(service, path, (
        Predicate("error_rate_failing_path", "http_5xx_rate_path", "<=", 1.1, relative_to_baseline=True,
                  floor=ERROR_RATE_FLOOR),                                                                 # Q11.84
        Predicate("error_rate_service", "http_5xx_rate", "<=", 1.1, relative_to_baseline=True, floor=ERROR_RATE_FLOOR),
        Predicate("latency_p95", "http_latency_p95_ms", "<=", 1.2, relative_to_baseline=True),            # Q11.85
        Predicate("probe_success", "synthetic_probe_success_ratio", ">=", 0.99),                          # Q11.82
        Predicate("availability", "http_success_ratio", ">=", 0.995),
        Predicate("new_error_groups", "new_error_group_count", "<=", 0),                                  # Q11.87
    ), window_s=hold_seconds, baseline=baseline or {})


@dataclass(frozen=True)
class Sample:
    """One interval's measurements, read from monitoring by the verifier."""

    t: int
    requests: int
    values: dict[str, Optional[float]]   # metric -> value (None = not available)


def baseline(samples: list[Sample], started_at: int) -> dict[str, float]:
    """ADR-66: each metric's median over [start − 60 min, start − 5 min). A metric with no data is
    left out, so a predicate relative to it can't be judged."""
    values: dict[str, list[float]] = {}
    for s in samples:
        if started_at - 3600 <= s.t < started_at - 300:
            for metric, v in s.values.items():
                if v is not None:
                    values.setdefault(metric, []).append(v)
    return {metric: statistics.median(vs) for metric, vs in values.items()}


class Verdict(str, Enum):
    VERIFIED = "verified"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"


@dataclass
class VerificationResult:
    verdict: Verdict
    reasons: list[str] = field(default_factory=list)
    per_predicate: dict[str, str] = field(default_factory=dict)


def verify(plan: VerificationPlan, samples: list[Sample]) -> VerificationResult:
    """Q11.84–Q11.88, Q11.92. Failure beats inconclusive; inconclusive beats verified. The baseline
    is the plan's own, fixed before execution. A metric missing from it is insufficient data."""
    need = plan.window_s // plan.interval_s
    res = VerificationResult(Verdict.VERIFIED)
    if len(samples) < need:
        res.verdict = Verdict.INCONCLUSIVE
        res.reasons.append(f"observed {len(samples)} of {need} intervals")
    for p in plan.predicates:
        base = plan.baseline.get(p.metric)
        judged = not p.relative_to_baseline or base is not None
        limit = max(p.threshold * base, p.floor) if p.relative_to_baseline and judged else p.threshold
        status = "held" if judged else "insufficient_data"
        for s in samples if judged else ():
            v = s.values.get(p.metric)
            if v is None or s.requests < plan.min_requests_per_interval:
                status = "insufficient_data" if status == "held" else status
                continue
            ok = v <= limit if p.comparison == "<=" else v >= limit
            if not ok:
                status = f"violated at t={s.t}: {v} vs {p.comparison} {round(limit, 4)}"
                break
        res.per_predicate[p.name] = status
        if status.startswith("violated") and p.required:
            res.verdict = Verdict.FAILED
            res.reasons.append(f"{p.name} {status}")
        elif status == "insufficient_data" and p.required and res.verdict != Verdict.FAILED:
            res.verdict = Verdict.INCONCLUSIVE
            res.reasons.append(f"{p.name}: not enough data")
    return res


# ---------------------------------------------------------------------------
# 11.13 Rollback strategy
# ---------------------------------------------------------------------------
def rollback_action_fields(parent: ImmutableAction) -> dict:
    """Q11.90, Q11.91, ADR-48: a rollback is its own immutable action, linked to the original, with
    its own ID. It restores exactly the pre-change revision and is valid only while the target still
    runs the revision the original produced. It isn't re-tested, so it carries the parent's digests.
    It needs its own approval decision (given alongside the original as a conditional approval, or fresh)."""
    return {"incident_id": parent.incident_id, "diagnosis_id": parent.diagnosis_id, "kind": "rollback",
            "parent_action_id": parent.action_id, "patch_sha256": None,
            "base_revision": parent.expected_after_revision, "expected_after_revision": parent.base_revision,
            "target": parent.target, "policy_version": parent.policy_version,
            "test_bundle_digest": parent.test_bundle_digest,
            "recovery_predicates_digest": parent.recovery_predicates_digest,
            "hold_seconds": parent.hold_seconds, "high_impact": parent.high_impact}


def resolution_allowed(verdict: Verdict, incident_lead_accepted: bool, open_actions: int) -> tuple[bool, str]:
    """Q11.92: only a verified result, accepted by a person, with nothing still in flight."""
    if verdict != Verdict.VERIFIED:
        return False, f"verification is {verdict.value}"
    if open_actions:
        return False, f"{open_actions} action(s) still open"
    if not incident_lead_accepted:
        return False, "awaiting incident lead acceptance"
    return True, "resolved"
