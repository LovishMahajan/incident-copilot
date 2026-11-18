"""Artifact 8.2, 9.2, 9.3, 9.7, 9.8, 13.5 T1.14: the `hypothesise`, `diagnose` and
`review_diagnosis` activities.

Each one loads its incident's records through a SkillStore (aisre.agent.skills.store over
PostgreSQL), assembles its context (reasoning.build_request), and asks the Model Gateway, the
only owner of model retries (ADR-27): up to three attempts per step, re-asking only to say what
failed validation. The activity heartbeats while the gateway works, and a cancellation stops the
call. Every answer goes through activities.checked_result before anyone sees it; one the gateway
stopped under ADR-59 goes to activities.unsafe_result instead: no retry, a masked audit copy, and
for a secret the security stop that starts the leak procedure (ST-23, ST-24).

Failures are values (ADR-52): a SkillResult with a SkillFailure the coordinator stops on. A
database outage raises instead, and MODEL_RETRY runs the activity again; the gateway then reuses
the recorded answer for the same input, so the provider isn't asked twice (ADR-61).

- hypothesise: 2 to 5 hypotheses (HypothesesOut), every citation one of the given records.
  Evidence requests are pre-checked under `hypothesise` (9.4); a refused one is dropped.
- diagnose: a DiagnosisDraft weighed against the hypotheses and any reviewer issues. Code
  computes what the coordinator reads: the fingerprint, the share of supported claims, and the
  confidence each hypothesis reaches from its cited observations (ADR-53). The draft is stored,
  with its hypotheses, before the result is returned.
- review_diagnosis: code checks first (validation.code_checks), then the critic, who sees the
  evidence and the draft only. Requests are pre-checked under `critique`. validation.combine
  gives the verdict and final confidence, which are written once (ADR-69). An answer that
  fails validation three times is ReviewResult(), not a failure: the coordinator decides.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Optional, Protocol, TypeVar
from uuid import UUID, uuid5

from pydantic import BaseModel, ValidationError
from temporalio import activity

from aisre.agent.agent_contracts import (
    Confidence,
    CriticVerdict,
    DiagnosisDraft,
    DiagnosisResult,
    HypothesesOut,
    ReviewResult,
    SkillFailure,
    SkillResult,
    SkillUsage,
)
from aisre.agent.skills import reasoning as r
from aisre.agent.tool_registry import ToolGate
from aisre.agent.validation import (
    UnsupportedReference,
    check_citations,
    code_checks,
    combine,
    fingerprint,
    missing_required_sources,
    score_confidence,
)
from aisre.gateway.adapters import sha256
from aisre.gateway.gateway import UNSAFE_OUTPUT, ModelGateway, unsafe_kind
from aisre.gateway.gateway_contracts import ModelRequest, ModelResult, ResultStatus, TextBlock
from aisre.retrieval.context import PromptRegistry
from aisre.workflow import activities as act

DIAGNOSIS_NAMESPACE = UUID("6f1d0c52-7a43-5b0e-9c1d-3e8b2a47f915")
DEFAULT_STEP_S = 300.0  # outside an activity: the skill steps' timeout (8.5)
HEARTBEAT_S = 5.0  # well inside the 20 s heartbeat timeout of the skill steps
M = TypeVar("M", bound=BaseModel)


class SkillStore(Protocol):
    """What the skills read and write. aisre.agent.skills.store.PgSkillStore is the real one."""

    async def inputs(self, incident_id: str, evidence_ids: Sequence[str]) -> Optional[r.Inputs]: ...

    async def save_diagnosis(
        self,
        diagnosis_id: UUID,
        inputs: r.Inputs,
        draft: DiagnosisDraft,
        confidence: Confidence,
        hypothesis_confidence: list[Confidence],
        produced_by: str,
    ) -> None: ...

    async def save_review(
        self, diagnosis_id: UUID, inputs: r.Inputs, draft: DiagnosisDraft, review: ReviewResult
    ) -> None: ...


def diagnosis_id_for(incident_id: str, idempotency_key: str) -> UUID:
    """The same step gives the same diagnosis ID, so a retried activity stores nothing twice."""
    return uuid5(DIAGNOSIS_NAMESPACE, f"{incident_id}:{idempotency_key}")


def scored(draft: DiagnosisDraft, inputs: r.Inputs) -> tuple[Confidence, list[Confidence]]:
    """ADR-53, by code: the leading hypothesis's level (low with no leader) and each one's."""
    ev, missing = inputs.by_id, missing_required_sources(inputs.evidence)
    each = [score_confidence(h, ev, inputs.gaps, missing) for h in draft.hypotheses]
    lead = each[draft.leading_index] if draft.leading_index is not None else Confidence.low
    return lead, each


class ModelSkills:
    """The three activities, for `Worker(activities=...)`: `.all()`."""

    def __init__(
        self,
        gateway: ModelGateway,
        prompts: PromptRegistry,
        store: SkillStore,
        clock: Callable[[], float] = time.time,
        heartbeat_s: float = HEARTBEAT_S,
    ) -> None:
        self.gateway, self.prompts, self.store = gateway, prompts, store
        self.clock, self.heartbeat_s = clock, heartbeat_s

    def all(self) -> list[Callable[..., Any]]:
        return [self.hypothesise, self.diagnose, self.review_diagnosis]

    # ------------------------------------------------------------------ activities
    @activity.defn(name="hypothesise")
    async def hypothesise(self, inp: act.SkillIn) -> SkillResult[HypothesesOut]:
        inputs = await self.store.inputs(inp.incident_id, inp.evidence_ids)
        if inputs is None:
            return _not_found("hypothesise")
        known = list(inputs.by_id)
        asked = await self._ask(
            "hypothesise",
            HypothesesOut,
            inputs,
            inp.idempotency_key,
            check=lambda out: r.citation_problem(out.hypotheses, known),
        )
        if isinstance(asked, SkillResult):
            return asked
        out, usage = asked
        gate = self._gate(inputs)
        kept = []
        for h in out.hypotheses:
            requests = [x.model_dump() for x in h.evidence_needed]
            allowed, _refused = act.precheck_requests(gate, requests, "hypothesise")
            kept.append(h.model_dump() | {"evidence_needed": allowed})
        out = HypothesesOut.model_validate({"hypotheses": kept})
        return act.checked_result("hypothesise", out, usage, inp.incident_id)

    @activity.defn(name="diagnose")
    async def diagnose(self, inp: act.SkillIn) -> SkillResult[DiagnosisResult]:
        inputs = await self.store.inputs(inp.incident_id, inp.evidence_ids)
        if inputs is None or inp.hypotheses is None:
            return _not_found("diagnose")
        ev, incident = inputs.by_id, inputs.context.incident_id
        extras = [r.data_item("hypotheses", "hypotheses", inp.hypotheses)]
        if inp.critic is not None:
            extras.append(r.data_item("review:previous", "reviewer_issues", inp.critic))
        must = {f"evidence:{e}" for e in r.cited(inp.hypotheses.hypotheses) if e in ev}

        def check(d: DiagnosisDraft) -> Optional[str]:
            try:
                check_citations(d, ev, incident)
            except UnsupportedReference:
                return r.citation_problem(d.hypotheses, ev) or "a citation is not valid"
            return None

        asked = await self._ask(
            "diagnose", DiagnosisDraft, inputs, inp.idempotency_key, check, extras, must
        )
        if isinstance(asked, SkillResult):
            return asked
        draft, usage = asked
        checked = act.checked_result("diagnose", draft, usage, inp.incident_id)
        if checked.failure is not None:
            return checked
        report = check_citations(draft, ev, incident)
        confidence, each = scored(draft, inputs)
        diagnosis_id = diagnosis_id_for(inp.incident_id, inp.idempotency_key)
        prompt = self.prompts.current("diagnose")
        await self.store.save_diagnosis(
            diagnosis_id, inputs, draft, confidence, each, f"diagnose:{prompt.ref}"
        )
        out = DiagnosisResult(
            diagnosis_id=diagnosis_id,
            draft=draft,
            fingerprint=fingerprint(draft),
            supported_ratio=report.supported_ratio,
        )
        return SkillResult(output=out, usage=usage)

    @activity.defn(name="review_diagnosis")
    async def review_diagnosis(self, inp: act.SkillIn) -> SkillResult[ReviewResult]:
        inputs = await self.store.inputs(inp.incident_id, inp.evidence_ids)
        if inputs is None or inp.diagnosis is None or inp.diagnosis_id is None:
            return _not_found("critique")
        draft, ev = inp.diagnosis, inputs.by_id
        # Cited records whose source changed since collection need the sources re-read; the
        # collector doesn't offer that yet, so none is reported stale here.
        issues = code_checks(draft, ev, inputs.context.incident_id, inputs.gaps, changed=[])
        level, _each = scored(draft, inputs)
        extras = [r.data_item(f"diagnosis:{inp.diagnosis_id}", "diagnosis_draft", draft)]
        asked = await self._ask(
            "critique", CriticVerdict, inputs, inp.idempotency_key, lambda v: None, extras
        )
        if isinstance(asked, SkillResult):
            failure = asked.failure
            if failure is not None and failure.code == "invalid_output" and not _unsafe(failure):
                return SkillResult(output=ReviewResult(), usage=asked.usage)
            return asked
        verdict, usage = asked
        requests = [x.model_dump() for x in verdict.evidence_requests]
        allowed, _refused = act.precheck_requests(self._gate(inputs), requests, "critique")
        try:
            verdict = CriticVerdict.model_validate(
                verdict.model_dump() | {"evidence_requests": allowed}
            )
        except ValidationError:  # a revise whose only content was refused requests
            return SkillResult(output=ReviewResult(), usage=usage)
        checked = act.checked_result("critique", verdict, usage, inp.incident_id)
        if checked.failure is not None:
            return checked
        review = combine(verdict, issues, level)
        await self.store.save_review(UUID(inp.diagnosis_id), inputs, draft, review)
        return SkillResult(output=review, usage=usage)

    # ------------------------------------------------------------------ the call
    async def _ask(
        self,
        task: str,
        model: type[M],
        inputs: r.Inputs,
        key: str,
        check: Callable[[M], Optional[str]],
        extras: Sequence[dict[str, Any]] = (),
        must_include: set[str] | frozenset[str] = frozenset(),
    ) -> tuple[M, SkillUsage] | SkillResult[Any]:
        """Ask until the answer passes `check` or the step's attempts are spent. Returns the
        answer and the usage, or the SkillResult to return."""
        prompt = self.prompts.current(task)
        usage, feedback, used = SkillUsage(), list[dict[str, Any]](), 0
        while True:
            req = r.build_request(
                task,
                prompt,
                inputs,
                request_id=f"{key}:{task}:{used + 1}",
                deadline_epoch_s=self._deadline(),
                extras=[*extras, *feedback],
                must_include=must_include,
                attempts_used=used,
            )
            result, calls = await self._call(req)
            spent = await self.gateway.ledger.evidence_only(self.clock())
            usage = r.add_usage(usage, r.usage_of(result, calls, spent))
            if result.status != ResultStatus.OK or result.parsed is None:
                kind = unsafe_kind(result)
                if kind is not None:  # stopped in the gateway: its text is masked already
                    shown = "".join(b.text for b in result.content if isinstance(b, TextBlock))
                    return act.unsafe_result(
                        task, kind, shown, usage, str(inputs.context.incident_id)
                    )
                return SkillResult(failure=r.failure_for(task, result), usage=usage)
            answer = model.model_validate(result.parsed)
            problem = check(answer)
            if problem is None:
                return answer, usage
            used = max(used + 1, result.attempt)
            if used >= r.MAX_STEP_ATTEMPTS:
                failure = SkillFailure(skill=task, code="invalid_output", detail=problem[:500])
                return SkillResult(failure=failure, usage=usage)
            feedback = [r.feedback_item(used, problem)]

    async def _call(self, req: ModelRequest) -> tuple[ModelResult, int]:
        """The gateway call, heartbeating while it runs. Returns the result and the provider
        calls it made: none when the gateway reused a recorded answer (ADR-61)."""
        reused = await self.gateway.ledger.reusable(
            req.incident_id, sha256(self.gateway.adapter.build(req))
        )
        call = asyncio.create_task(self.gateway.call(req))
        try:
            while True:
                done, _ = await asyncio.wait({call}, timeout=self.heartbeat_s)
                if done:
                    break
                if activity.in_activity():
                    activity.heartbeat()
        finally:
            if not call.done():  # cancelled: stop the call with the activity
                call.cancel()
        result = call.result()
        calls = 0 if reused is not None else max(0, result.attempt - req.step_attempts_used)
        return result, calls

    def _deadline(self) -> float:
        """The activity's own end: the workflow sets it no later than the investigation's."""
        if activity.in_activity():
            info = activity.info()
            if info.start_to_close_timeout is not None:
                start = info.current_attempt_scheduled_time.timestamp()
                return start + info.start_to_close_timeout.total_seconds()
        return self.clock() + DEFAULT_STEP_S

    def _gate(self, inputs: r.Inputs) -> ToolGate:
        """For pre-checks only: schema, scope and skill list. Calls are counted when they run."""
        c = inputs.context
        now = datetime.fromtimestamp(self.clock(), UTC)
        return ToolGate(str(c.incident_id), c.service, c.started_at, max(now, c.started_at))


def _unsafe(failure: SkillFailure) -> bool:
    return failure.detail.startswith(UNSAFE_OUTPUT)


def _not_found(skill: str) -> SkillResult[Any]:
    failure = SkillFailure(skill=skill, code="store_failed", detail="incident or input not found")
    return SkillResult(failure=failure, usage=SkillUsage())
