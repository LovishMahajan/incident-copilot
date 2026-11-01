"""Artifact 9.1, 9.9, 9.10: the coordinator — the deterministic control loop.

The coordinator decides the next step; skills (activities) do the work; the model never
decides whether to continue. Pure Python, no I/O, no clock of its own: it runs inside the
Temporal workflow (Artifact 8), which passes it workflow.now(). Every decision is recorded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional


class Step(str, Enum):
    COLLECT_BASELINE = "collect_baseline"     # fixed evidence plan, no model
    HYPOTHESISE = "hypothesise"               # model: 2-5 hypotheses with claims
    COLLECT_TARGETED = "collect_targeted"     # tools chosen from hypotheses or critic requests
    DIAGNOSE = "diagnose"                     # model + code scoring
    CRITIQUE = "critique"                     # code checks, then the independent critic
    FINISH_ESTABLISHED = "finish_established"
    FINISH_INCONCLUSIVE = "finish_inconclusive"


@dataclass(frozen=True)
class Limits:
    """Provisional; Artifact 4 sets the totals."""
    investigation: timedelta = timedelta(minutes=15)
    max_model_calls: int = 30
    max_tool_calls: int = 20
    max_revisions: int = 2           # diagnose may run at most 1 + 2 times
    max_targeted_rounds: int = 3
    max_requests_per_round: int = 3


@dataclass
class AgentState:
    """Everything the loop needs between steps. Persisted by the workflow history (Q9.8)."""
    started_at: datetime
    limits: Limits = field(default_factory=Limits)
    evidence_ids: list[str] = field(default_factory=list)
    gap_count: int = 0
    hypotheses_ready: bool = False
    pending_requests: list[dict] = field(default_factory=list)
    targeted_rounds: int = 0
    last_round_new_evidence: Optional[int] = None
    diagnoses: int = 0
    diagnosis_fingerprints: list[str] = field(default_factory=list)
    supported_ratios: list[float] = field(default_factory=list)
    last_verdict: Optional[str] = None       # accept | revise | escalate | invalid
    last_outcome: Optional[str] = None       # established | inconclusive (from the diagnoser)
    critic_failures: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    baseline_done: bool = False
    stop_reason: Optional[str] = None
    decisions: list[str] = field(default_factory=list)

    @property
    def revisions(self) -> int:
        return max(0, self.diagnoses - 1)


def next_step(s: AgentState, now: datetime) -> Step:
    """The loop termination policy (9.10) and revision policy (9.9), in one function."""
    step, why = _decide(s, now)
    s.decisions.append(f"{step.value}: {why}")
    if step in (Step.FINISH_ESTABLISHED, Step.FINISH_INCONCLUSIVE):
        s.stop_reason = why
    return step


def _decide(s: AgentState, now: datetime) -> tuple[Step, str]:
    L = s.limits
    # ---- Hard stops, checked before every step
    if s.last_verdict == "accept":
        return Step.FINISH_ESTABLISHED, "critic accepted"
    if now - s.started_at >= L.investigation:
        return Step.FINISH_INCONCLUSIVE, "investigation time limit reached"
    if s.model_calls >= L.max_model_calls:
        return Step.FINISH_INCONCLUSIVE, "model call budget spent"
    if s.last_verdict == "escalate":
        return Step.FINISH_INCONCLUSIVE, "critic escalated"
    if s.critic_failures >= 2:
        return Step.FINISH_INCONCLUSIVE, "critic output invalid twice; failing closed"

    # ---- Normal progression
    if not s.baseline_done:
        return Step.COLLECT_BASELINE, "fixed evidence plan first"
    if not s.hypotheses_ready:
        return Step.HYPOTHESISE, "evidence ready"
    if s.pending_requests and s.targeted_rounds < L.max_targeted_rounds and s.tool_calls < L.max_tool_calls:
        return Step.COLLECT_TARGETED, f"{len(s.pending_requests)} evidence requests pending"
    if s.diagnoses == 0:
        return Step.DIAGNOSE, "hypotheses ready"
    if s.last_verdict is None and s.last_outcome == "inconclusive":
        return Step.FINISH_INCONCLUSIVE, "diagnoser could not establish a cause"
    if s.last_verdict is None or s.last_verdict == "invalid":
        return Step.CRITIQUE, "diagnosis awaiting independent review"

    # ---- Revision policy (last verdict was 'revise')
    if s.revisions >= L.max_revisions:
        return Step.FINISH_INCONCLUSIVE, f"revision limit ({L.max_revisions}) reached"
    if len(s.diagnosis_fingerprints) >= 2 and s.diagnosis_fingerprints[-1] == s.diagnosis_fingerprints[-2]:
        return Step.FINISH_INCONCLUSIVE, "revision repeated the same diagnosis"
    if len(s.supported_ratios) >= 2 and s.supported_ratios[-1] <= s.supported_ratios[-2] \
            and not s.last_round_new_evidence:
        return Step.FINISH_INCONCLUSIVE, "revision did not improve support and no new evidence"
    if s.last_round_new_evidence == 0 and s.pending_requests == [] and s.targeted_rounds > 0 \
            and s.supported_ratios and s.supported_ratios[-1] < 1.0:
        return Step.FINISH_INCONCLUSIVE, "requested evidence added nothing new"
    return Step.DIAGNOSE, f"revision {s.revisions + 1} of {L.max_revisions}"


# ---------------------------------------------------------------------------
# State updates, one per skill result (called by the workflow after each activity)
# ---------------------------------------------------------------------------
def after_collect(s: AgentState, new_ids: list[str], gaps: int, tool_calls: int, targeted: bool) -> None:
    fresh = [i for i in new_ids if i not in s.evidence_ids]
    s.evidence_ids.extend(fresh)
    s.gap_count += gaps
    s.tool_calls += tool_calls
    if targeted:
        s.targeted_rounds += 1
        s.pending_requests = []
        s.last_round_new_evidence = len(fresh)
        # last_verdict stays 'revise' (if set), so the revision policy decides what follows
    else:
        s.baseline_done = True


def after_hypotheses(s: AgentState, model_calls: int, requests: list[dict]) -> None:
    s.hypotheses_ready = True
    s.model_calls += model_calls
    s.pending_requests = requests[: s.limits.max_requests_per_round]


def after_diagnosis(s: AgentState, fingerprint: str, supported_ratio: float, model_calls: int,
                    outcome: str = "established") -> None:
    s.diagnoses += 1
    s.last_outcome = outcome
    s.model_calls += model_calls
    s.diagnosis_fingerprints.append(fingerprint)
    s.supported_ratios.append(supported_ratio)
    s.last_verdict = None
    s.last_round_new_evidence = None if s.diagnoses == 1 else s.last_round_new_evidence


def after_critique(s: AgentState, verdict: Optional[str], requests: list[dict], model_calls: int) -> None:
    """verdict None = invalid critic output after its own retry."""
    s.model_calls += model_calls
    if verdict is None:
        s.critic_failures += 1
        s.last_verdict = "invalid"
        return
    s.last_verdict = verdict
    if verdict == "revise":
        s.pending_requests = requests[: s.limits.max_requests_per_round]
        s.last_round_new_evidence = None
