"""Artifact 9.1, 9.9, 9.10: the coordinator — the deterministic control loop (ADR-52).

The coordinator decides the next step; skills (activities) do the work; the model never
decides whether to continue. Pure Python, standard library only, no I/O, no clock of its
own: it runs inside the Temporal workflow (Artifact 8), which passes it workflow.now().
Every decision is recorded.

The workflow's loop: call next_step(state, now); on a FINISH step leave, reading
state.stop_reason and state.stop_kind; otherwise run the step as one activity and hand its
SkillResult to apply_result(state, step, result). A skill failure arrives as a value in that
result, never as an exception, and the next call to next_step stops the loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Optional


class Step(str, Enum):
    COLLECT_BASELINE = "collect_baseline"     # fixed evidence plan, no model
    RETRIEVE_KNOWLEDGE = "retrieve_knowledge" # runbook and past-incident passages, no model
    HYPOTHESISE = "hypothesise"               # model: 2-5 hypotheses with claims
    COLLECT_TARGETED = "collect_targeted"     # tools chosen from hypotheses or critic requests
    DIAGNOSE = "diagnose"                     # model + code scoring
    CRITIQUE = "critique"                     # code checks, then the independent critic
    FINISH_ESTABLISHED = "finish_established"
    FINISH_INCONCLUSIVE = "finish_inconclusive"


class StopKind(str, Enum):
    """Which incident path a FINISH_INCONCLUSIVE takes (ADR-46, 9.10)."""
    SECURITY = "security"            # security or data risk: investigating -> handed_over
    INCONCLUSIVE = "inconclusive"    # every other stop: investigating -> inconclusive -> handed_over


SECURITY_FAILURE = "security_risk"   # the one SkillFailure code that takes the direct path


@dataclass(frozen=True)
class Limits:
    """The one configuration object (ADR-52), read by the workflow, the gateway's budget
    defaults and the ToolGate. All values provisional; the money value is from ADR-56."""
    investigation: timedelta = timedelta(minutes=15)
    max_model_calls: int = 30
    max_input_tokens: int = 200_000
    max_output_tokens: int = 20_000
    max_cost_micros: int = 2_000_000
    max_tool_calls: int = 20
    max_revisions: int = 2           # diagnose may run at most 1 + 2 times
    max_targeted_rounds: int = 3
    max_requests_per_round: int = 3
    max_critic_failures: int = 2
    max_attempts_per_step: int = 3   # enforced by the gateway (ADR-27)


@dataclass
class AgentState:
    """Everything the loop needs between steps. Persisted by the workflow history (Q9.8)."""
    started_at: datetime
    limits: Limits = field(default_factory=Limits)
    evidence_only: bool = False      # started after the daily limit: no model steps (ADR-55)
    evidence_ids: list[str] = field(default_factory=list)
    gap_count: int = 0
    knowledge_done: bool = False
    hypotheses_ready: bool = False
    pending_requests: list[dict] = field(default_factory=list)
    targeted_rounds: int = 0
    last_round_new_evidence: Optional[int] = None    # fresh IDs minus gaps, last targeted round
    diagnoses: int = 0
    diagnosis_fingerprints: list[str] = field(default_factory=list)
    supported_ratios: list[float] = field(default_factory=list)
    observations_at_diagnosis: list[int] = field(default_factory=list)
    last_verdict: Optional[str] = None       # accept | revise | escalate | invalid
    last_outcome: Optional[str] = None       # established | inconclusive (from the diagnoser)
    confidence: Optional[str] = None         # final: low | medium | high, from validation.combine
    critic_failures: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_micros: int = 0
    daily_spend_exhausted: bool = False
    baseline_done: bool = False
    failure: Optional[dict] = None           # the first SkillFailure: skill, code, detail
    stop_reason: Optional[str] = None
    stop_kind: Optional[StopKind] = None     # set with FINISH_INCONCLUSIVE only
    decisions: list[str] = field(default_factory=list)

    @property
    def revisions(self) -> int:
        return max(0, self.diagnoses - 1)

    @property
    def observations(self) -> int:
        return len(self.evidence_ids) - self.gap_count


def next_step(s: AgentState, now: datetime) -> Step:
    """The loop termination policy (9.10) and revision policy (9.9), in one function.

    Deterministic: the result depends only on the state and `now`, so a workflow replay
    gives the same answer. The only effect is on `s`: the decision log and, on a FINISH
    step, stop_reason and stop_kind."""
    step, why = _decide(s, now)
    s.decisions.append(f"{step.value}: {why}")
    if step in (Step.FINISH_ESTABLISHED, Step.FINISH_INCONCLUSIVE):
        s.stop_reason = why
    if step == Step.FINISH_INCONCLUSIVE:
        security = s.failure is not None and s.failure["code"] == SECURITY_FAILURE
        s.stop_kind = StopKind.SECURITY if security else StopKind.INCONCLUSIVE
    return step


def _decide(s: AgentState, now: datetime) -> tuple[Step, str]:
    L = s.limits
    # ---- Hard stops, checked before every step
    if s.failure is not None and s.failure["code"] == SECURITY_FAILURE:
        return Step.FINISH_INCONCLUSIVE, _failure_reason(s)
    if s.last_verdict == "accept":
        if s.confidence in ("medium", "high"):
            return Step.FINISH_ESTABLISHED, "critic accepted"
        return Step.FINISH_INCONCLUSIVE, "critic accepted, but final confidence is low"
    if now - s.started_at >= L.investigation:
        return Step.FINISH_INCONCLUSIVE, "investigation time limit reached"
    if s.model_calls >= L.max_model_calls:
        return Step.FINISH_INCONCLUSIVE, "model call budget spent"
    if s.input_tokens >= L.max_input_tokens or s.output_tokens >= L.max_output_tokens:
        return Step.FINISH_INCONCLUSIVE, "token budget spent"
    if s.cost_micros >= L.max_cost_micros:
        return Step.FINISH_INCONCLUSIVE, "incident spend limit reached"
    if s.daily_spend_exhausted and not s.evidence_only:
        return Step.FINISH_INCONCLUSIVE, "daily spend limit reached"
    if s.failure is not None:
        return Step.FINISH_INCONCLUSIVE, _failure_reason(s)
    if s.last_verdict == "escalate":
        return Step.FINISH_INCONCLUSIVE, "critic escalated"
    if s.critic_failures >= L.max_critic_failures:
        return Step.FINISH_INCONCLUSIVE, "critic output invalid twice; failing closed"
    if s.last_round_new_evidence == 0:
        return Step.FINISH_INCONCLUSIVE, "targeted round added nothing new"

    # ---- Normal progression
    if not s.baseline_done:
        return Step.COLLECT_BASELINE, "fixed evidence plan first"
    if not s.knowledge_done:
        return Step.RETRIEVE_KNOWLEDGE, "baseline collected"
    if s.evidence_only:
        return Step.FINISH_INCONCLUSIVE, "evidence-only: daily spend limit reached"
    if not s.hypotheses_ready:
        return Step.HYPOTHESISE, "evidence ready"
    if s.last_verdict is None and s.last_outcome == "inconclusive":
        return Step.FINISH_INCONCLUSIVE, "diagnoser could not establish a cause"

    # ---- Revision policy, before any pending requests (last verdict was 'revise')
    if s.last_verdict == "revise":
        if s.revisions >= L.max_revisions:
            return Step.FINISH_INCONCLUSIVE, f"revision limit ({L.max_revisions}) reached"
        if len(s.diagnosis_fingerprints) >= 2 and s.diagnosis_fingerprints[-1] == s.diagnosis_fingerprints[-2]:
            return Step.FINISH_INCONCLUSIVE, "revision repeated the same diagnosis"
        if len(s.supported_ratios) >= 2 and s.supported_ratios[-1] <= s.supported_ratios[-2] \
                and s.observations == s.observations_at_diagnosis[-2]:
            return Step.FINISH_INCONCLUSIVE, "revision did not improve support and no new evidence"

    if s.pending_requests and s.targeted_rounds < L.max_targeted_rounds and s.tool_calls < L.max_tool_calls:
        return Step.COLLECT_TARGETED, f"{len(s.pending_requests)} evidence requests pending"
    if s.diagnoses == 0:
        return Step.DIAGNOSE, "hypotheses ready"
    if s.last_verdict == "revise":
        return Step.DIAGNOSE, f"revision {s.revisions + 1} of {L.max_revisions}"
    return Step.CRITIQUE, "diagnosis awaiting independent review"


def _failure_reason(s: AgentState) -> str:
    f = s.failure
    return f"{f['skill']} failed ({f['code']}): {f['detail']}"


# ---------------------------------------------------------------------------
# State updates, one per skill result (called by the workflow after each activity)
# ---------------------------------------------------------------------------
def apply_result(s: AgentState, step: Step, result: Any) -> None:
    """Feed one activity's SkillResult (agent_contracts) back into the state.

    Duck-typed so this module stays free of Pydantic and safe in workflow code."""
    u = result.usage
    s.input_tokens += u.input_tokens
    s.output_tokens += u.output_tokens
    s.cost_micros += u.cost_micros
    s.daily_spend_exhausted = s.daily_spend_exhausted or u.daily_spend_exhausted
    if result.failure is not None:
        s.model_calls += u.model_calls
        s.tool_calls += u.tool_calls
        after_failure(s, result.failure.skill, result.failure.code, result.failure.detail)
        return
    o = result.output
    if step in (Step.COLLECT_BASELINE, Step.COLLECT_TARGETED):
        after_collect(s, o.evidence_ids, o.gap_count, u.tool_calls, targeted=step == Step.COLLECT_TARGETED)
    elif step == Step.RETRIEVE_KNOWLEDGE:
        after_knowledge(s, o.evidence_ids, o.gap_count, u.tool_calls)
    elif step == Step.HYPOTHESISE:
        after_hypotheses(s, u.model_calls, [[r.model_dump() for r in h.evidence_needed] for h in o.hypotheses])
    elif step == Step.DIAGNOSE:
        after_diagnosis(s, o.fingerprint, o.supported_ratio, u.model_calls, o.draft.outcome)
    elif step == Step.CRITIQUE:
        v = o.verdict
        after_critique(s, v.verdict if v else None, [r.model_dump() for r in v.evidence_requests] if v else [],
                       u.model_calls, o.confidence)
    else:
        raise ValueError(f"{step} runs no skill")


def after_failure(s: AgentState, skill: str, code: str, detail: str) -> None:
    """A typed SkillFailure. The first one is kept, and next_step stops the loop on it."""
    s.decisions.append(f"failure: {skill} {code}")
    if s.failure is None:
        s.failure = {"skill": skill, "code": code, "detail": detail}


def after_collect(s: AgentState, new_ids: list[str], gaps: int, tool_calls: int, targeted: bool) -> None:
    fresh = [i for i in new_ids if i not in s.evidence_ids]
    s.evidence_ids.extend(fresh)
    s.gap_count += gaps
    s.tool_calls += tool_calls
    if targeted:
        s.targeted_rounds += 1
        s.pending_requests = []
        s.last_round_new_evidence = max(0, len(fresh) - gaps)     # a gap is not new evidence
        # last_verdict stays 'revise' (if set), so the revision policy decides what follows
    else:
        s.baseline_done = True


def after_knowledge(s: AgentState, new_ids: list[str], gaps: int, tool_calls: int) -> None:
    s.evidence_ids.extend(i for i in new_ids if i not in s.evidence_ids)
    s.gap_count += gaps
    s.tool_calls += tool_calls
    s.knowledge_done = True


def after_hypotheses(s: AgentState, model_calls: int, requests: list[list[dict]]) -> None:
    """`requests` holds one list per hypothesis. They are taken one per hypothesis in turn,
    so alternatives get a test; what doesn't fit is logged as not run."""
    s.hypotheses_ready = True
    s.model_calls += model_calls
    left = [list(r) for r in requests]
    picked: list[dict] = []
    while any(left) and len(picked) < s.limits.max_requests_per_round:
        for r in left:
            if r and len(picked) < s.limits.max_requests_per_round:
                picked.append(r.pop(0))
    s.pending_requests = picked
    _log_not_run(s, [r for rest in left for r in rest])


def after_diagnosis(s: AgentState, fingerprint: str, supported_ratio: float, model_calls: int,
                    outcome: str = "established") -> None:
    s.diagnoses += 1
    s.last_outcome = outcome
    s.model_calls += model_calls
    s.diagnosis_fingerprints.append(fingerprint)
    s.supported_ratios.append(supported_ratio)
    s.observations_at_diagnosis.append(s.observations)
    s.last_verdict = None


def after_critique(s: AgentState, verdict: Optional[str], requests: list[dict], model_calls: int,
                   confidence: Optional[str] = None) -> None:
    """verdict None = invalid critic output after its own retry. `confidence` is the final
    confidence from validation.combine; accept without medium or high ends inconclusive."""
    s.model_calls += model_calls
    if verdict is None:
        s.critic_failures += 1
        s.last_verdict = "invalid"
        return
    s.last_verdict = verdict
    s.confidence = confidence
    if verdict == "revise":
        s.pending_requests = requests[: s.limits.max_requests_per_round]
        _log_not_run(s, requests[s.limits.max_requests_per_round:])


def _log_not_run(s: AgentState, requests: list[dict]) -> None:
    for r in requests:
        s.decisions.append(f"not run: evidence request for {r.get('tool')} over the round limit")
