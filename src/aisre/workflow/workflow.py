"""Artifact 8.1: the Temporal workflow definition for one incident.

One workflow per run, ID ``incident-<incident_id>``, then ``-r2`` and on for a re-investigation
(ADR-46). The workflow decides; activities act. Workflow code here is deterministic: no I/O, no
clocks other than workflow.now(), no randomness, no threads. The investigating phase is the
Layer 9 coordinator's loop: pure next_step, one activity per step, its result fed back
(ADR-52). Every business state change goes through the ``record_transition`` activity, which
writes the incident's status and timeline event — Temporal's own history is never the
product's timeline (Q8.24). The dispatcher writes action `applying` and its outcomes (ADR-51),
so the workflow only tracks those in memory. A failed fix is undone by a separate rollback
action with its own approval and execution key, never by cancellation (ADR-48).

activities pulls in Pydantic and Layer 9's contracts, so it passes through the sandbox.
coordinator and states are pure; coordinator is imported plainly so the sandbox checks that.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, NamedTuple, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError
from temporalio.exceptions import CancelledError as ActivityCancelled
from temporalio.exceptions import TimeoutError as ActivityTimeout

from aisre.agent.coordinator import AgentState, Limits, Step, StopKind, after_failure, apply_result, next_step

with workflow.unsafe.imports_passed_through():
    import aisre.workflow.activities as act
    from aisre.workflow.states import (
        INCIDENT_TRANSITIONS,
        PROTECTED_INCIDENT,
        ROLLBACK_KIND,
        ActionState as A,
        IncidentState as I,
        check_action,
        check_incident,
    )


# ---------------------------------------------------------------------------
# Limits (Artifact 4; provisional)
# ---------------------------------------------------------------------------
LIMITS = Limits()                               # the one configuration object (ADR-52)
INVESTIGATION_LIMIT = LIMITS.investigation      # from the slot grant
APPROVAL_ESCALATE_AFTER = timedelta(minutes=15)
APPROVAL_EXPIRES_AFTER = timedelta(minutes=60)
APPROVAL_VALID_FOR = timedelta(minutes=15)      # a granted approval must be used within this
WORKFLOW_LIMIT = timedelta(hours=24)
LIMIT_REASON = "24-hour workflow limit reached"


# ---------------------------------------------------------------------------
# Retry policies (8.5). Only one layer retries each operation.
# ---------------------------------------------------------------------------
DB_RETRY = RetryPolicy(initial_interval=timedelta(seconds=1), backoff_coefficient=2.0,
                       maximum_interval=timedelta(seconds=30), maximum_attempts=0,  # until schedule_to_close, where set
                       non_retryable_error_types=["InvalidTransition"])
TOOL_RETRY = RetryPolicy(initial_interval=timedelta(seconds=2), backoff_coefficient=2.0,
                         maximum_interval=timedelta(seconds=20), maximum_attempts=3)
NO_RETRY = RetryPolicy(maximum_attempts=1)       # forbidden: the sandbox and production writes
MODEL_RETRY = RetryPolicy(maximum_attempts=2)    # failures return as values; this is one re-run for a lost worker
READ_RETRY = RetryPolicy(initial_interval=timedelta(seconds=5), backoff_coefficient=2.0,
                         maximum_interval=timedelta(minutes=1), maximum_attempts=10,
                         non_retryable_error_types=["PermissionDenied"])
SLOT_RETRY = RetryPolicy(initial_interval=timedelta(seconds=15), backoff_coefficient=1.0,
                         maximum_interval=timedelta(seconds=15), maximum_attempts=0)  # NoSlot is normal while queued


class StepPlan(NamedTuple):
    fn: Callable
    skill: str                                   # SkillFailure.skill
    timeout_s: int
    retry: RetryPolicy
    heartbeat_s: Optional[int]


_COLLECT = StepPlan(act.collect_evidence, "collect_evidence", 180, TOOL_RETRY, 20)
STEP_PLANS: dict[Step, StepPlan] = {
    Step.COLLECT_BASELINE: _COLLECT,
    Step.RETRIEVE_KNOWLEDGE: StepPlan(act.retrieve_knowledge, "retrieve_knowledge", 30, TOOL_RETRY, None),
    Step.HYPOTHESISE: StepPlan(act.hypothesise, "hypothesise", 300, MODEL_RETRY, 20),
    Step.COLLECT_TARGETED: _COLLECT,
    Step.DIAGNOSE: StepPlan(act.diagnose, "diagnose", 300, MODEL_RETRY, 20),
    Step.CRITIQUE: StepPlan(act.review_diagnosis, "critique", 300, MODEL_RETRY, 20),
}
FINISH_STEPS = (Step.FINISH_ESTABLISHED, Step.FINISH_INCONCLUSIVE)
WITHDRAWABLE = (A.PROPOSED, A.TESTING, A.AWAITING_APPROVAL)
ROLLBACK_AUDIENCE = ["on_call", "incident_lead", "approvers"]


# ---------------------------------------------------------------------------
# Workflow input and signals
# ---------------------------------------------------------------------------
@dataclass
class IncidentWorkflowInput:
    incident_id: str
    run: int = 1                  # 2 and on for a re-investigation


@dataclass
class ApprovalSignal:
    decision_id: str              # dedupe key; one signal per decision (ADR-49)
    action_id: str
    decision: str                 # "approved" | "rejected" | "revoked"
    action_digest: str
    decided_by: str


@dataclass
class CancelSignal:
    request_id: str               # dedupe key
    requested_by: str
    reason: str


@dataclass
class LeadSignal:
    request_id: str               # dedupe key
    kind: str                     # "confirm_severity" | "close" | "reinvestigate"
    by: str
    value: Optional[str] = None


class Cancelled(Exception):
    """Internal: raised at a safe point when cancellation was requested."""


class HandOverNow(Exception):
    """Internal: no way forward (8.10, or the 24-hour limit); hand over from the current state."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@workflow.defn(name="IncidentWorkflow")
class IncidentWorkflow:
    def __init__(self) -> None:
        self.state: I = I.OPEN
        self.action_state: Optional[A] = None
        self.action_id: Optional[str] = None
        self.action_kind: str = ""
        self.patch_sha256: Optional[str] = None
        self.action_digest: Optional[str] = None
        self.hold_seconds: int = 0
        self.rollback_state: Optional[A] = None
        self.rollback_id: Optional[str] = None
        self.rollback_digest: Optional[str] = None
        self.version: int = 0
        self.step: int = 0
        self.seen_signals: set[str] = set()
        self.awaited: Optional[tuple[str, str]] = None     # action ID and digest open for decisions
        self.decisions: dict[str, tuple[str, datetime]] = {}   # by person: decision, when received
        self.approvers_needed: int = 1
        self.open_request: Optional[act.ApprovalRequestIn] = None
        self.cancel: Optional[CancelSignal] = None
        self.close_requested: bool = False
        self.reinvestigate_requested: bool = False
        self.limit_reached: bool = False
        self.running = None                                # the running cancellable activity
        self.incident_id: str = ""
        self.run_key: str = ""
        # AgentState holds only IDs and counters; the skills' outputs are kept here.
        self.hypotheses = None
        self.diagnosis = None
        self.critic = None

    # ----------------------------------------------------------------- signals
    @workflow.signal
    def approval_decided(self, sig: ApprovalSignal) -> None:
        if sig.decision_id in self.seen_signals:
            return                                    # duplicate delivery from the outbox
        self.seen_signals.add(sig.decision_id)
        if (sig.action_id, sig.action_digest) != self.awaited:
            return                                    # stale, misdirected or for a different digest
        if sig.decision == "revoked":
            if self.decisions.get(sig.decided_by, ("",))[0] == "approved":
                del self.decisions[sig.decided_by]
            return
        self.decisions[sig.decided_by] = (sig.decision, workflow.now())

    @workflow.signal
    def cancellation_requested(self, sig: CancelSignal) -> None:
        if sig.request_id in self.seen_signals:
            return
        self.seen_signals.add(sig.request_id)
        if self.cancel is None:
            self.cancel = sig
        if self.running is not None:                  # never set while a write is in flight
            self.running.cancel()

    @workflow.signal
    def lead_decision(self, sig: LeadSignal) -> None:
        if sig.request_id in self.seen_signals:
            return
        self.seen_signals.add(sig.request_id)
        if sig.kind == "close":
            self.close_requested = True               # the API has already recorded `closed`
        elif sig.kind == "reinvestigate":
            self.reinvestigate_requested = True       # the relay starts the next run
        # severity confirmation is recorded by the API in the same transaction; nothing to steer here

    @workflow.query
    def current_state(self) -> dict:
        return {"incident": self.state.value,
                "action": self.action_state.value if self.action_state else None,
                "version": self.version}

    # -------------------------------------------------------------------- run
    @workflow.run
    async def run(self, inp: IncidentWorkflowInput) -> str:
        self.incident_id = inp.incident_id
        self.run_key = inp.incident_id if inp.run == 1 else f"{inp.incident_id}-r{inp.run}"
        snap = await self._act(act.load_incident, act.IncidentIn(inp.incident_id, f"{self.run_key}:load"),
                               timeout_s=10, retry=DB_RETRY, unlimited=True)
        if snap is None:
            return "incident_missing"                 # Q7.55: never run without a record
        self.version = snap.version
        self.state = I(snap.status)                   # `handed_over` on a re-investigation run
        if self.state not in (I.OPEN, I.HANDED_OVER):
            return self.state.value                   # nothing starts from here
        limit_timer = asyncio.create_task(self._limit_timer())
        try:
            try:
                await self._lifecycle(snap)
            except HandOverNow as e:
                await self._abandon(e.reason)
            await self._wait_for_close()
        except (Cancelled, asyncio.CancelledError):
            # Only reached at safe points: never while a production write is in flight.
            self._note_cancel()
            if I.CANCELLED in INCIDENT_TRANSITIONS[self.state]:
                await self._finish_cancelled()
            # Already handed over or recovered: a person owns it; cancellation changes nothing.
        limit_timer.cancel()
        return self.state.value

    async def _lifecycle(self, snap: act.IncidentSnapshot) -> None:
        # ---- Queue for a slot (the incident stays as it is until the grant)
        await self._cancellable(act.acquire_investigation_slot, act.IncidentIn(self.incident_id, f"{self.run_key}:slot"),
                                timeout_s=10, retry=SLOT_RETRY, unlimited=True)
        granted_at = workflow.now()                   # the investigation limit starts here

        # ---- Investigate
        await self._to(I.INVESTIGATING, "slot granted")
        await self._notify("opened", ["on_call", "service_owner", "change_author"])
        s, finish = await self._investigate(granted_at)
        await self._safe_point()
        if finish == Step.FINISH_INCONCLUSIVE:
            if s.stop_kind != StopKind.SECURITY:      # only a security or data-risk stop goes direct
                await self._to(I.INCONCLUSIVE, s.stop_reason)
            return await self._hand_over(s.stop_reason)
        await self._to(I.DIAGNOSED, "reviewed diagnosis established")
        await self._safe_point()

        # ---- Patch and test
        step_key = self._step_key()
        try:
            res = await self._cancellable(act.draft_patch, act.SkillIn(
                self.incident_id, step_key, evidence_ids=list(s.evidence_ids), diagnosis=self.diagnosis.draft,
                diagnosis_id=str(self.diagnosis.diagnosis_id)), timeout_s=300, retry=MODEL_RETRY, heartbeat_s=20)
        except ActivityError:
            return await self._hand_over("no fix drafted: draft_patch failed")
        if res.failure is not None:
            return await self._hand_over(f"no fix drafted: {res.failure.detail}")
        patch = res.output
        self.action_id, self.patch_sha256, self.action_state = patch.action_id, patch.patch_sha256, A.PROPOSED
        self.action_kind, self.hold_seconds = patch.action_kind, patch.hold_seconds
        await self._safe_point()
        if not snap.remediation_enabled and not patch.sandbox_available:
            return await self._hand_over("sandbox unavailable; untested draft handed over", action_to=A.WITHDRAWN)
        await self._to(I.TESTING, "patch drafted", action_to=A.TESTING)
        try:
            test = await self._cancellable(
                act.run_sandbox_test,
                act.TestIn(patch.action_id, patch.patch_sha256, test_key=f"{self.action_id}:test:1"),
                timeout_s=900, heartbeat_s=20, retry=NO_RETRY)
        except ActivityError:
            return await self._hand_over("fix not proven: sandbox test failed", action_to=A.TEST_FAILED)
        await self._safe_point()
        if not (test.reproduced and test.outcome == "passed"):
            return await self._hand_over(f"fix not proven: {test.outcome}", action_to=A.TEST_FAILED)
        if not snap.remediation_enabled:              # Release A: people change production (ADR-44)
            return await self._hand_over("release_a_recommendation", action_to=A.WITHDRAWN)

        # ---- Approval wait (8.6)
        self.action_digest = test.action_digest
        self.awaited = (self.action_id, self.action_digest)
        await self._to(I.AWAITING_APPROVAL, "tested; approval requested", action_to=A.AWAITING_APPROVAL)
        out = await self._request_approval(self.action_id, self.action_digest)
        if out.rollback_action_id:
            self.rollback_id, self.rollback_digest = out.rollback_action_id, out.rollback_digest
            self.rollback_state = A.PROPOSED          # withdrawn unless a `failed` verdict needs it
        decision = await self._await_decision(out.approvers_needed)
        await self._safe_point()
        if decision == "expired":
            await self._close_open_request()
            return await self._hand_over("approval expired; no action taken", action_to=A.EXPIRED)
        if decision == "rejected":
            return await self._hand_over("fix rejected by approver", action_to=A.REJECTED)

        # ---- Execute (protected: cancellation waits for the outcome) (8.7)
        # The dispatcher reloads everything and refuses an approval past valid_until, a revoked
        # one, a changed patch fingerprint or a changed service state. It records `applying`.
        self.open_request = None
        await self._to(I.EXECUTING, "approved; dispatching", action_to=A.APPROVED)
        if self._rejected():                          # arrived during that write (ADR-49)
            return await self._hand_over("rejected after approval, before intent", action_to=A.WITHDRAWN)
        out = await self._execute(self.action_id)
        if out.outcome != "applied":
            return await self._hand_over(self._not_applied_reason(out), action_to=self._unapplied_state(out))
        await self._safe_point()

        # ---- Verify (8.9 covers the rollback path)
        await self._to(I.VERIFYING, "change applied")
        verdict = await self._verify(self.action_id, out.attempt_id)
        if verdict == "verified":
            await self._to(I.RECOVERED, "recovery held", action_to=A.VERIFIED)
            await self._notify("recovered", ["on_call", "incident_lead"])
        elif verdict == "failed":
            await self._roll_back()
        else:                                         # too little data, or no result: never a rollback
            await self._hand_over(f"verification {verdict or 'gave no result'}; no rollback, a person decides")

    # --------------------------------------------------------------- investigate
    async def _investigate(self, granted_at: datetime) -> tuple[AgentState, Step]:
        """The coordinator's loop (ADR-52). It decides; each step is one activity."""
        s = AgentState(started_at=granted_at, limits=LIMITS, evidence_only=await self._evidence_only())
        deadline = granted_at + LIMITS.investigation
        while True:
            step = next_step(s, workflow.now())
            if step in FINISH_STEPS:
                return s, step
            await self._safe_point()
            plan = STEP_PLANS[step]
            remaining = (deadline - workflow.now()).total_seconds()
            try:
                result = await self._cancellable(
                    plan.fn, self._step_input(step, s), timeout_s=max(1, int(min(plan.timeout_s, remaining))),
                    schedule_s=max(1, int(min(plan.timeout_s * 4, remaining))),
                    retry=plan.retry, heartbeat_s=plan.heartbeat_s)
            except ActivityError as e:                # skills return failures as values, so this is no result at all
                code = "deadline_passed" if isinstance(e.cause, ActivityTimeout) else "store_failed"
                after_failure(s, plan.skill, code, f"no result: {type(e.cause).__name__}")
                continue
            apply_result(s, step, result)
            if result.failure is None:
                self._keep(step, result.output)
            if step == Step.HYPOTHESISE and result.failure is None:
                await self._notify("summary", ["on_call"], detail="not yet reviewed")

    def _step_input(self, step: Step, s: AgentState):
        key = self._step_key()
        if step == Step.COLLECT_BASELINE:
            return act.EvidenceIn(self.incident_id, key, mode="baseline")
        if step == Step.COLLECT_TARGETED:
            return act.EvidenceIn(self.incident_id, key, mode="targeted", requests=list(s.pending_requests))
        ids = list(s.evidence_ids)
        if step == Step.DIAGNOSE:
            return act.SkillIn(self.incident_id, key, evidence_ids=ids, hypotheses=self.hypotheses, critic=self.critic)
        if step == Step.CRITIQUE:                     # evidence and diagnosis only, never the reasoning
            return act.SkillIn(self.incident_id, key, evidence_ids=ids, diagnosis=self.diagnosis.draft,
                               diagnosis_id=str(self.diagnosis.diagnosis_id))
        return act.SkillIn(self.incident_id, key, evidence_ids=ids)

    def _keep(self, step: Step, output) -> None:
        if step == Step.HYPOTHESISE:
            self.hypotheses = output
        elif step == Step.DIAGNOSE:
            self.diagnosis = output
        elif step == Step.CRITIQUE and output.verdict is not None:
            self.critic = output.verdict

    async def _evidence_only(self) -> bool:
        try:
            out = await self._act(act.read_spend_state, act.IncidentIn(self.incident_id, f"{self.run_key}:spend"),
                                  timeout_s=10, retry=DB_RETRY)
            return out.evidence_only
        except ActivityError:
            return True                               # spend can't be read, so spend nothing

    def _step_key(self) -> str:
        self.step += 1
        return f"{self.run_key}:s{self.step}"

    # ------------------------------------------------------------------ approval
    async def _request_approval(self, action_id: str, digest: str) -> act.ApprovalRequestOut:
        self.open_request = act.ApprovalRequestIn(action_id, digest, request_key=f"{action_id}:approval:1")
        try:
            return await self._act(act.request_approval, self.open_request, timeout_s=30, retry=DB_RETRY)
        except ActivityError as e:
            raise HandOverNow("approval could not be requested") from e

    async def _await_decision(self, approvers_needed: int) -> str:
        """Returns approved, rejected, expired or cancelled. Escalates at 15 min, expires at 60."""
        self.approvers_needed = approvers_needed
        await self._notify("approval_needed", ["approvers"])
        settled = await self._wait(self._decided, APPROVAL_ESCALATE_AFTER)
        if not settled:
            try:
                await self._act(act.escalate_approval, self.open_request, timeout_s=30, retry=DB_RETRY)
            except ActivityError as e:
                raise HandOverNow("approval could not be escalated") from e
            settled = await self._wait(self._decided, APPROVAL_EXPIRES_AFTER - APPROVAL_ESCALATE_AFTER)
        if self.cancel is not None:
            return "cancelled"
        if not settled:
            return "expired"
        return "rejected" if self._rejected() else "approved"

    def _decided(self) -> bool:
        return self.cancel is not None or self._rejected() or self._valid_approvals() >= self.approvers_needed

    def _rejected(self) -> bool:
        return any(d == "rejected" for d, _ in self.decisions.values())

    def _valid_approvals(self) -> int:
        oldest = workflow.now() - APPROVAL_VALID_FOR  # an older approval stops counting (ADR-49)
        return sum(1 for d, at in self.decisions.values() if d == "approved" and at >= oldest)

    async def _close_open_request(self) -> None:
        if self.open_request is None:
            return
        try:
            await self._act(act.close_approval_request, self.open_request, timeout_s=30, retry=DB_RETRY)
        except ActivityError:
            pass                                      # the path is chosen; the API refuses late approvals
        self.open_request = None

    # ------------------------------------------------------------------- execute
    async def _execute(self, action_id: str) -> act.DispatchOut:
        """Dispatches and, if there's no definite answer, reconciles. The dispatcher records the
        action's status; here it is only followed in memory."""
        try:
            out = await self._protected(act.dispatch_change, act.DispatchIn(action_id), timeout_s=300)
            self._follow(action_id, out)
        except ActivityError:
            out = None                                # no answer: never assumed, action state unknown here
        if out is None or out.outcome == "unknown":
            await self._to(I.RECONCILING, "dispatch outcome unknown")
            try:
                out = await self._protected(act.reconcile_execution, act.DispatchIn(action_id), timeout_s=150, retry=READ_RETRY)
            except ActivityError:
                out = act.DispatchOut(attempt_id="", outcome="unknown")   # still unknown
            self._follow(action_id, out)
        return out

    def _follow(self, action_id: str, out: act.DispatchOut) -> None:
        if out.outcome != "refused" and out.intent_recorded:
            to = {"applied": A.EXECUTED, "not_applied": A.NOT_APPLIED, "unknown": A.OUTCOME_UNKNOWN}[out.outcome]
            if action_id == self.rollback_id:
                self.rollback_state = to
            else:
                self.action_state = to

    @staticmethod
    def _unapplied_state(out: act.DispatchOut) -> Optional[A]:
        """Nothing was sent, so the action is still `approved`; the dispatcher can't write this."""
        if out.outcome == "refused" and (out.refusal_reason or "").startswith(act.REFUSED_EXPIRED):
            return A.EXPIRED
        return A.WITHDRAWN if out.outcome == "refused" or not out.intent_recorded else None

    @staticmethod
    def _not_applied_reason(out: act.DispatchOut) -> str:
        if out.outcome == "unknown":
            return "production state uncertain; do not retry until a person checks"
        if out.outcome == "refused":
            return f"dispatcher refused: {out.refusal_reason}"
        return f"change {out.outcome}" if out.intent_recorded else "no intent found: nothing was sent"

    async def _verify(self, action_id: str, attempt_id: str) -> Optional[str]:
        """The verdict, or None if verify_recovery gave none."""
        try:
            ver = await self._protected(
                act.verify_recovery,
                act.VerifyIn(action_id, attempt_id, self.hold_seconds, verification_key=f"{action_id}:verify:1"),
                timeout_s=self.hold_seconds + 300, heartbeat_s=60, retry=READ_RETRY)
            return ver.result
        except ActivityError:
            return None

    async def _roll_back(self) -> None:
        """Verdict `failed` (ADR-48). The workflow triggers it; the rollback is its own action.
        The incident stays not_recovered, or reconciling if the outcome was unknown."""
        if self.rollback_id is None:
            await self._to(I.NOT_RECOVERED, "verification failed")
            return await self._hand_over("fix did not hold; no rollback action exists", audience=ROLLBACK_AUDIENCE)
        self.awaited, self.decisions = (self.rollback_id, self.rollback_digest), {}
        await self._to(I.NOT_RECOVERED, "verification failed", rollback_to=A.AWAITING_APPROVAL)
        out = await self._request_approval(self.rollback_id, self.rollback_digest)
        decision = "approved" if out.conditional_approval else await self._await_decision(out.approvers_needed)
        if decision != "approved":
            await self._close_open_request()
            to = {"rejected": A.REJECTED, "expired": A.EXPIRED, "cancelled": A.WITHDRAWN}[decision]
            return await self._hand_over(f"fix did not hold; rollback {decision}; person decides next step",
                                         rollback_to=to, audience=ROLLBACK_AUDIENCE)
        self.open_request = None
        await self._write(self.state, "rollback approved", rollback_to=A.APPROVED)
        out = await self._execute(self.rollback_id)
        if out.outcome != "applied":
            return await self._hand_over(f"fix did not hold; rollback: {self._not_applied_reason(out)}",
                                         rollback_to=self._unapplied_state(out), audience=ROLLBACK_AUDIENCE)
        await self._write(self.state, "rollback applied", action_to=A.UNDONE)
        verdict = await self._verify(self.rollback_id, out.attempt_id)   # reported; triggers nothing
        await self._hand_over(f"fix did not hold; rollback applied, check {verdict or 'gave no result'}; "
                              "person decides next step",
                              rollback_to=A.VERIFIED if verdict == "verified" else None, audience=ROLLBACK_AUDIENCE)

    # ------------------------------------------------------------- helpers
    async def _act(self, fn, arg, *, timeout_s: int, retry: RetryPolicy = TOOL_RETRY,
                   heartbeat_s: Optional[int] = None, queue: str = act.INVESTIGATION_QUEUE,
                   unlimited: bool = False):
        """Runs an activity to its outcome: a Temporal cancel can't interrupt it, so the outcome
        is always recorded before `run` handles the cancel. `unlimited` sets no schedule-to-close
        limit, so a database outage pauses the run, within the 24-hour limit."""
        return await self._shielded(workflow.start_activity(
            fn, arg, task_queue=queue,
            start_to_close_timeout=timedelta(seconds=timeout_s),
            schedule_to_close_timeout=None if unlimited else timedelta(seconds=timeout_s * 4),
            heartbeat_timeout=timedelta(seconds=heartbeat_s) if heartbeat_s else None,
            retry_policy=retry))

    async def _shielded(self, handle):
        while True:
            try:
                return await asyncio.shield(handle)
            except asyncio.CancelledError:
                self._note_cancel()                   # remember it; keep waiting for the outcome

    async def _cancellable(self, fn, arg, *, timeout_s: int, retry: RetryPolicy = TOOL_RETRY,
                           heartbeat_s: Optional[int] = None, schedule_s: Optional[int] = None,
                           unlimited: bool = False):
        """Runs an activity that cancellation stops (8.7). The handle is kept so the signal
        handler and the 24-hour timer can cancel it; WAIT_CANCELLATION_COMPLETED lets it
        finish, so the sandbox is destroyed first. A Temporal cancel arrives the same way."""
        await self._safe_point()
        handle = workflow.start_activity(
            fn, arg, task_queue=act.INVESTIGATION_QUEUE,
            start_to_close_timeout=timedelta(seconds=timeout_s),
            schedule_to_close_timeout=None if unlimited else timedelta(seconds=schedule_s or timeout_s * 4),
            heartbeat_timeout=timedelta(seconds=heartbeat_s) if heartbeat_s else None,
            retry_policy=retry,
            cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED)
        self.running = handle
        try:
            return await handle
        except ActivityError as e:
            if not isinstance(e.cause, ActivityCancelled):
                raise
            if self.limit_reached and self.cancel is None:
                raise HandOverNow(LIMIT_REASON) from e
            self._note_cancel()
            raise Cancelled() from e
        finally:
            self.running = None

    async def _protected(self, fn, arg, *, timeout_s: int, retry: RetryPolicy = NO_RETRY,
                         heartbeat_s: Optional[int] = None):
        """Runs an operation that must finish even if cancellation arrives. Writes go to the
        dispatcher queue with NO automatic retry: an unanswered write becomes 'unknown'."""
        queue = act.DISPATCHER_QUEUE if fn in (act.dispatch_change, act.reconcile_execution) else act.INVESTIGATION_QUEUE
        return await self._act(fn, arg, timeout_s=timeout_s, retry=retry, heartbeat_s=heartbeat_s, queue=queue)

    def _note_cancel(self) -> None:
        if self.cancel is None:
            self.cancel = CancelSignal("temporal-cancel", "system", "workflow cancellation")

    async def _wait(self, condition, timeout: Optional[timedelta]) -> bool:
        """Durable wait: no process is held; a timer lives in Temporal's history."""
        try:
            await workflow.wait_condition(condition, timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False
        except asyncio.CancelledError:
            self._note_cancel()
            if self.state in PROTECTED_INCIDENT:      # a rollback's wait: the caller sees self.cancel
                return True
            raise Cancelled() from None

    async def _limit_timer(self) -> None:
        """The whole-run limit (ADR-46). Work stops at the next safe point; a write in flight
        reaches its outcome first. Waits for a person end by themselves."""
        await workflow.sleep(max(WORKFLOW_LIMIT - (workflow.now() - workflow.info().start_time), timedelta(seconds=1)))
        self.limit_reached = True
        if self.running is not None:
            self.running.cancel()

    async def _safe_point(self) -> None:
        if self.cancel is not None and self.state not in PROTECTED_INCIDENT:
            raise Cancelled()
        if self.limit_reached:
            raise HandOverNow(LIMIT_REASON)

    async def _to(self, target: I, reason: str, action_to: Optional[A] = None,
                  rollback_to: Optional[A] = None) -> None:
        check_incident(self.state, target)              # refuse invalid transitions before any write
        if rollback_to is None and target in (I.RECOVERED, I.HANDED_OVER, I.CANCELLED) \
                and self.rollback_state in (A.PROPOSED, A.AWAITING_APPROVAL):
            rollback_to = A.WITHDRAWN                   # the fix ended, and nothing needs its rollback
        await self._write(target, reason, action_to, rollback_to)

    async def _write(self, target: I, reason: str, action_to: Optional[A] = None,
                     rollback_to: Optional[A] = None) -> None:
        """One record_transition. target == self.state records action changes alone."""
        if action_to is not None:
            check_action(self.action_state, action_to, self.action_kind)
        if rollback_to is not None:
            check_action(self.rollback_state, rollback_to, ROLLBACK_KIND)
        self.step += 1
        out = await self._act(act.record_transition, act.TransitionIn(
            incident_id=self.incident_id, transition_id=f"{self.run_key}:t{self.step}",
            from_state=self.state.value, to_state=target.value, expected_version=self.version,
            reason=reason, action_id=self.action_id,
            action_from=self.action_state.value if action_to else None,
            action_to=action_to.value if action_to else None,
            second_action_id=self.rollback_id if rollback_to else None,
            second_action_from=self.rollback_state.value if rollback_to else None,
            second_action_to=rollback_to.value if rollback_to else None),
            timeout_s=10, retry=DB_RETRY, unlimited=True)
        self.version = out.new_version
        self.state = target
        if action_to is not None:
            self.action_state = action_to
        if rollback_to is not None:
            self.rollback_state = rollback_to

    async def _notify(self, kind: str, audience: list[str], detail: Optional[str] = None) -> None:
        self.step += 1
        inp = act.NotifyIn(self.incident_id, f"{self.run_key}:n{self.step}", kind, audience, detail)
        try:
            await self._act(act.notify, inp, timeout_s=15)
        except ActivityError as e:
            if self.state not in (I.RECOVERED, I.HANDED_OVER):
                raise HandOverNow(f"notification {kind} failed") from e
            await self._act(act.record_notification_failed, inp,   # no further transition exists (8.10)
                            timeout_s=10, retry=DB_RETRY, unlimited=True)

    async def _hand_over(self, reason: str, action_to: Optional[A] = None, rollback_to: Optional[A] = None,
                         audience: Optional[list[str]] = None) -> None:
        if self.state != I.HANDED_OVER:
            await self._to(I.HANDED_OVER, reason, action_to, rollback_to)  # e.g. inconclusive -> handed_over
        self.step += 1
        await self._act(act.hand_over, act.HandOverIn(self.incident_id, reason, f"{self.run_key}:h{self.step}"),
                        timeout_s=15, retry=DB_RETRY, unlimited=True)
        await self._notify("handed_over", audience or ["on_call", "incident_lead"])

    async def _abandon(self, reason: str) -> None:
        """Hands over from wherever the run stopped. `open` and `investigating` have to pass
        through their table paths; `recovered` and `handed_over` have nothing left to do."""
        if self.state == I.OPEN:
            await self._to(I.INVESTIGATING, reason)
        if self.state == I.INVESTIGATING:
            await self._to(I.INCONCLUSIVE, reason)
        if self.state in (I.RECOVERED, I.HANDED_OVER):
            return
        await self._close_open_request()
        await self._hand_over(reason, action_to=A.WITHDRAWN if self.action_state in WITHDRAWABLE else None)

    async def _wait_for_close(self) -> None:
        await self._wait(lambda: self.close_requested or self.reinvestigate_requested or self.limit_reached, None)
        if self.close_requested:
            self.state = I.CLOSED                       # recorded by the API, in or after this run
        # At 24 h or on `reinvestigate` the run ends without a write; closure still works.

    async def _finish_cancelled(self) -> None:
        await self._close_open_request()
        action_to = A.WITHDRAWN if self.action_state in WITHDRAWABLE else None
        reason = f"cancelled by {self.cancel.requested_by}: {self.cancel.reason}" if self.cancel else "cancelled"
        await self._to(I.CANCELLED, reason, action_to=action_to)


@workflow.defn(name="ReconciliationWorkflow")
class ReconciliationWorkflow:
    """Started by the aisre-reconciliation schedule (8.9): runs the one read-only activity."""

    @workflow.run
    async def run(self) -> int:
        out = await workflow.execute_activity(
            act.reconcile_workflows, act.ReconciliationIn(workflow.info().run_id),
            task_queue=act.INVESTIGATION_QUEUE, start_to_close_timeout=timedelta(seconds=60),
            schedule_to_close_timeout=timedelta(seconds=240), retry_policy=TOOL_RETRY)
        return out.mismatches
