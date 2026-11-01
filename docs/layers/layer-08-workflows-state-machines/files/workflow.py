"""Artifact 8.1: the Temporal workflow definition for one incident.

One workflow per incident, ID ``incident-<incident_id>`` (Artifact 7, Q7.63). The workflow
decides; activities act. Workflow code here is deterministic: no I/O, no clocks other than
workflow.now(), no randomness, no threads. Every business state change goes through the
``record_transition`` activity, which writes the incident's status and timeline event —
Temporal's own history is never the product's timeline (Q8.24).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    import activities as act
    from states import (
        INCIDENT_TRANSITIONS,
        PROTECTED_INCIDENT,
        ActionState as A,
        IncidentState as I,
        check_action,
        check_incident,
    )


# ---------------------------------------------------------------------------
# Limits (Artifact 4; provisional)
# ---------------------------------------------------------------------------
INVESTIGATION_LIMIT = timedelta(minutes=15)
APPROVAL_ESCALATE_AFTER = timedelta(minutes=15)
APPROVAL_EXPIRES_AFTER = timedelta(minutes=60)
APPROVAL_VALID_FOR = timedelta(minutes=15)      # a granted approval must be used within this
WORKFLOW_LIMIT = timedelta(hours=24)
RECOVERY_HOLD_SECONDS = 600


# ---------------------------------------------------------------------------
# Retry policies (8.5). Only one layer retries each operation.
# ---------------------------------------------------------------------------
DB_RETRY = RetryPolicy(initial_interval=timedelta(seconds=1), backoff_coefficient=2.0,
                       maximum_interval=timedelta(seconds=30), maximum_attempts=0)  # until schedule_to_close
TOOL_RETRY = RetryPolicy(initial_interval=timedelta(seconds=2), backoff_coefficient=2.0,
                         maximum_interval=timedelta(seconds=20), maximum_attempts=3)
NO_RETRY = RetryPolicy(maximum_attempts=1)       # retries owned inside the activity (model gateway) or forbidden (writes)
READ_RETRY = RetryPolicy(initial_interval=timedelta(seconds=5), backoff_coefficient=2.0,
                         maximum_interval=timedelta(minutes=1), maximum_attempts=10,
                         non_retryable_error_types=["PermissionDenied"])


# ---------------------------------------------------------------------------
# Workflow input and signals
# ---------------------------------------------------------------------------
@dataclass
class IncidentWorkflowInput:
    incident_id: str


@dataclass
class ApprovalSignal:
    approval_id: str              # dedupe key
    action_id: str
    decision: str                 # "approved" | "rejected"
    patch_sha256: str
    decided_by: str


@dataclass
class CancelSignal:
    request_id: str               # dedupe key
    requested_by: str
    reason: str


@dataclass
class LeadSignal:
    request_id: str               # dedupe key
    kind: str                     # "confirm_severity" | "close"
    by: str
    value: Optional[str] = None


class Cancelled(Exception):
    """Internal: raised at a safe point when cancellation was requested."""


@workflow.defn(name="IncidentWorkflow")
class IncidentWorkflow:
    def __init__(self) -> None:
        self.state: I = I.OPEN
        self.action_state: Optional[A] = None
        self.action_id: Optional[str] = None
        self.patch_sha256: Optional[str] = None
        self.version: int = 0
        self.step: int = 0
        self.seen_signals: set[str] = set()
        self.approval: Optional[ApprovalSignal] = None
        self.cancel: Optional[CancelSignal] = None
        self.close_requested: bool = False
        self.incident_id: str = ""

    # ----------------------------------------------------------------- signals
    @workflow.signal
    def approval_decided(self, sig: ApprovalSignal) -> None:
        if sig.approval_id in self.seen_signals:
            return                                    # duplicate delivery from the outbox
        self.seen_signals.add(sig.approval_id)
        if self.state != I.AWAITING_APPROVAL or sig.action_id != self.action_id:
            return                                    # stale or misdirected; the API already rejects these
        if sig.patch_sha256 != self.patch_sha256:
            return                                    # approval for a different patch is ignored
        self.approval = sig

    @workflow.signal
    def cancellation_requested(self, sig: CancelSignal) -> None:
        if sig.request_id in self.seen_signals:
            return
        self.seen_signals.add(sig.request_id)
        if self.cancel is None:
            self.cancel = sig

    @workflow.signal
    def lead_decision(self, sig: LeadSignal) -> None:
        if sig.request_id in self.seen_signals:
            return
        self.seen_signals.add(sig.request_id)
        if sig.kind == "close":
            self.close_requested = True
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
        snap = await self._act(act.load_incident, act.IncidentIn(inp.incident_id), timeout_s=10, retry=DB_RETRY)
        if snap is None:
            return "incident_missing"                 # Q7.55: never run without a record
        self.version = snap.version
        try:
            await self._lifecycle()
        except (Cancelled, asyncio.CancelledError):
            # Only reached at safe points: never while a production write is in flight.
            if I.CANCELLED in INCIDENT_TRANSITIONS[self.state]:
                await self._finish_cancelled()
            # Already handed over or recovered: a person owns it; cancellation changes nothing.
        return self.state.value

    async def _lifecycle(self) -> None:
        deadline = workflow.now() + INVESTIGATION_LIMIT

        # ---- Investigate
        await self._to(I.INVESTIGATING, "workflow started")
        await self._notify("opened", ["on_call", "service_owner", "change_author"])
        await self._safe_point()
        ev = await self._investigation_act(act.collect_evidence, deadline, timeout_s=180)
        await self._safe_point()
        await self._investigation_act(act.retrieve_knowledge, deadline, timeout_s=30)
        await self._safe_point()
        dx = await self._investigation_act(act.diagnose, deadline, timeout_s=300, retry=NO_RETRY)
        await self._notify("summary", ["on_call"])
        await self._safe_point()

        if dx is None or dx.outcome != "established":
            await self._to(I.INCONCLUSIVE, dx.stop_reason if dx else "investigation limit reached")
            await self._hand_over("root cause not established")
            return await self._wait_for_close()

        review = await self._investigation_act(act.review_diagnosis, deadline, timeout_s=60)
        if review is None or not review.accepted:
            await self._to(I.INCONCLUSIVE, "review did not accept the diagnosis")
            await self._hand_over("diagnosis rejected by review")
            return await self._wait_for_close()
        await self._to(I.DIAGNOSED, "reviewed diagnosis established")
        await self._safe_point()

        # ---- Patch and test
        patch = await self._act(act.draft_patch, act.IncidentIn(self.incident_id), timeout_s=120, retry=NO_RETRY)
        self.action_id, self.patch_sha256, self.action_state = patch.action_id, patch.patch_sha256, A.PROPOSED
        await self._to(I.TESTING, "patch drafted", action_to=A.TESTING)
        test = await self._act(
            act.run_sandbox_test,
            act.TestIn(patch.action_id, patch.patch_sha256, test_key=f"{self.action_id}:test:1"),
            timeout_s=900, heartbeat_s=60, retry=NO_RETRY)
        await self._safe_point()
        if not (test.reproduced and test.outcome == "passed"):
            await self._to(I.HANDED_OVER, f"test {test.outcome}", action_to=A.TEST_FAILED)
            await self._hand_over(f"fix not proven: {test.outcome}")
            return await self._wait_for_close()

        # ---- Approval wait (8.6)
        await self._to(I.AWAITING_APPROVAL, "tested; approval requested", action_to=A.AWAITING_APPROVAL)
        req = act.ApprovalRequestIn(self.action_id, self.patch_sha256, request_key=f"{self.action_id}:approval:1")
        await self._act(act.request_approval, req, timeout_s=30, retry=DB_RETRY)
        await self._notify("approval_needed", ["approvers"])
        decided = await self._wait(lambda: self.approval is not None or self.cancel is not None,
                                   APPROVAL_ESCALATE_AFTER)
        if not decided:
            await self._act(act.escalate_approval, req, timeout_s=30, retry=DB_RETRY)
            decided = await self._wait(lambda: self.approval is not None or self.cancel is not None,
                                       APPROVAL_EXPIRES_AFTER - APPROVAL_ESCALATE_AFTER)
        await self._safe_point()
        if not decided:
            await self._act(act.close_approval_request, req, timeout_s=30, retry=DB_RETRY)
            await self._to(I.HANDED_OVER, "approval request expired", action_to=A.EXPIRED)
            await self._hand_over("approval expired; no action taken")
            return await self._wait_for_close()
        if self.approval.decision != "approved":
            await self._to(I.HANDED_OVER, "approval rejected", action_to=A.REJECTED)
            await self._hand_over("fix rejected by approver")
            return await self._wait_for_close()

        # ---- Execute (protected: cancellation waits for the outcome) (8.7)
        # The dispatcher itself refuses an approval older than APPROVAL_VALID_FOR, a changed
        # patch fingerprint or a changed service state, and returns not_applied.
        await self._to(I.EXECUTING, "approved; dispatching", action_to=A.APPROVED)
        await self._record_action(A.APPLYING)
        dispatch_in = act.DispatchIn(self.action_id, self.approval.approval_id,
                                     execution_key=f"exec:{self.approval.approval_id}")
        out = await self._protected(act.dispatch_change, dispatch_in, timeout_s=300)

        if out.outcome == "unknown":
            await self._to(I.RECONCILING, "dispatch outcome unknown", action_to=A.OUTCOME_UNKNOWN)
            out = await self._protected(act.reconcile_execution, dispatch_in, timeout_s=600, retry=READ_RETRY)
        if out.outcome != "applied":
            target_action = A.NOT_APPLIED if out.outcome in ("not_applied", "failed") else None
            if target_action is None:                # still unknown after reconciliation
                await self._to(I.HANDED_OVER, "outcome still unknown after reconciliation")
                await self._hand_over("production state uncertain; do not retry until a person checks")
            else:
                await self._to(I.HANDED_OVER, f"change {out.outcome}", action_to=target_action)
                await self._hand_over(f"change {out.outcome}")
            return await self._wait_for_close()

        # ---- Verify (8.9 covers the undo path)
        await self._to(I.VERIFYING, "change applied", action_to=A.EXECUTED)
        ver = await self._protected(
            act.verify_recovery, act.VerifyIn(self.action_id, out.attempt_id, RECOVERY_HOLD_SECONDS),
            timeout_s=RECOVERY_HOLD_SECONDS + 300, heartbeat_s=60, retry=READ_RETRY)
        if ver.result == "recovered":
            await self._to(I.RECOVERED, "recovery held", action_to=A.VERIFIED)
            await self._notify("recovered", ["on_call", "incident_lead"])
            return await self._wait_for_close()

        await self._to(I.NOT_RECOVERED, f"verification {ver.result}")
        undo = await self._protected(
            act.dispatch_change,
            act.DispatchIn(self.action_id, self.approval.approval_id,
                           execution_key=f"undo:{self.approval.approval_id}", is_undo=True),
            timeout_s=300)
        if undo.outcome == "unknown":
            undo = await self._protected(act.reconcile_execution, act.DispatchIn(
                self.action_id, self.approval.approval_id,
                execution_key=f"undo:{self.approval.approval_id}", is_undo=True),
                timeout_s=600, retry=READ_RETRY)
        undone = undo.outcome == "applied"
        await self._to(I.HANDED_OVER, "fix did not hold; undo " + ("applied" if undone else undo.outcome),
                       action_to=A.UNDONE if undone else None)
        await self._hand_over("fix did not hold; person decides next step")
        await self._wait_for_close()

    # ------------------------------------------------------------- helpers
    async def _act(self, fn, arg, *, timeout_s: int, retry: RetryPolicy = TOOL_RETRY,
                   heartbeat_s: Optional[int] = None, queue: str = act.INVESTIGATION_QUEUE):
        return await workflow.execute_activity(
            fn, arg, task_queue=queue,
            start_to_close_timeout=timedelta(seconds=timeout_s),
            schedule_to_close_timeout=timedelta(seconds=timeout_s * 4),
            heartbeat_timeout=timedelta(seconds=heartbeat_s) if heartbeat_s else None,
            retry_policy=retry)

    async def _investigation_act(self, fn, deadline, *, timeout_s: int, retry: RetryPolicy = TOOL_RETRY):
        """Runs an investigation step inside the 15-minute budget; None if the budget is spent."""
        remaining = (deadline - workflow.now()).total_seconds()
        if remaining <= 0:
            return None
        bound = max(1, int(min(timeout_s, remaining)))
        try:
            return await workflow.execute_activity(
                fn, act.IncidentIn(self.incident_id), task_queue=act.INVESTIGATION_QUEUE,
                start_to_close_timeout=timedelta(seconds=bound),
                schedule_to_close_timeout=timedelta(seconds=max(1, int(remaining))),
                retry_policy=retry)
        except ActivityError:
            return None                                # the caller treats None as "hand over"

    async def _protected(self, fn, arg, *, timeout_s: int, retry: RetryPolicy = NO_RETRY,
                         heartbeat_s: Optional[int] = None):
        """Runs an operation that must finish even if cancellation arrives. Writes go to the
        dispatcher queue with NO automatic retry: an unanswered write becomes 'unknown'."""
        queue = act.DISPATCHER_QUEUE if fn in (act.dispatch_change, act.reconcile_execution) else act.INVESTIGATION_QUEUE
        handle = workflow.start_activity(
            fn, arg, task_queue=queue,
            start_to_close_timeout=timedelta(seconds=timeout_s),
            heartbeat_timeout=timedelta(seconds=heartbeat_s) if heartbeat_s else None,
            retry_policy=retry,
            cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED)
        try:
            return await asyncio.shield(handle)
        except asyncio.CancelledError:
            # A Temporal-level cancel arrived mid-write: remember it, keep waiting for the outcome.
            if self.cancel is None:
                self.cancel = CancelSignal("temporal-cancel", "system", "workflow cancellation")
            return await handle
        except ActivityError:
            if fn is act.dispatch_change:
                return act.DispatchOut(attempt_id="", outcome="unknown")   # never assume
            raise

    async def _wait(self, condition, timeout: timedelta) -> bool:
        """Durable wait: no process is held; a timer lives in Temporal's history."""
        try:
            await workflow.wait_condition(condition, timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def _safe_point(self) -> None:
        if self.cancel is not None and self.state not in PROTECTED_INCIDENT:
            raise Cancelled()

    async def _to(self, target: I, reason: str, action_to: Optional[A] = None) -> None:
        check_incident(self.state, target)              # refuse invalid transitions before any write
        action_from = self.action_state
        if action_to is not None:
            check_action(self.action_state, action_to)
        self.step += 1
        out = await self._act(act.record_transition, act.TransitionIn(
            incident_id=self.incident_id, transition_id=f"{self.incident_id}:t{self.step}",
            from_state=self.state.value, to_state=target.value, expected_version=self.version,
            reason=reason, action_id=self.action_id,
            action_from=action_from.value if action_from and action_to else None,
            action_to=action_to.value if action_to else None), timeout_s=10, retry=DB_RETRY)
        self.version = out.new_version
        self.state = target
        if action_to is not None:
            self.action_state = action_to

    async def _record_action(self, action_to: A) -> None:
        check_action(self.action_state, action_to)
        self.step += 1
        out = await self._act(act.record_transition, act.TransitionIn(
            incident_id=self.incident_id, transition_id=f"{self.incident_id}:t{self.step}",
            from_state=self.state.value, to_state=self.state.value, expected_version=self.version,
            reason=f"action {action_to.value}", action_id=self.action_id,
            action_from=self.action_state.value, action_to=action_to.value), timeout_s=10, retry=DB_RETRY)
        self.version = out.new_version
        self.action_state = action_to

    async def _notify(self, kind: str, audience: list[str]) -> None:
        self.step += 1
        await self._act(act.notify, act.NotifyIn(self.incident_id, f"{self.incident_id}:n{self.step}",
                                                 kind, audience), timeout_s=15)

    async def _hand_over(self, reason: str) -> None:
        if self.state != I.HANDED_OVER:
            await self._to(I.HANDED_OVER, reason)        # e.g. inconclusive -> handed_over
        self.step += 1
        await self._act(act.hand_over, act.HandOverIn(self.incident_id, reason, f"{self.incident_id}:h{self.step}"),
                        timeout_s=15, retry=DB_RETRY)
        await self._notify("handed_over", ["on_call", "incident_lead"])

    async def _wait_for_close(self) -> None:
        remaining = WORKFLOW_LIMIT - (workflow.now() - workflow.info().start_time)
        await self._wait(lambda: self.close_requested, max(remaining, timedelta(seconds=1)))
        if self.state in (I.RECOVERED, I.HANDED_OVER) and self.close_requested:
            await self._to(I.CLOSED, "closed by incident lead")
        # Not closed within 24 h: the workflow ends; the incident stays handed over for people.

    async def _finish_cancelled(self) -> None:
        if self.action_state in (A.AWAITING_APPROVAL, A.APPROVED):
            await self._act(act.close_approval_request, act.ApprovalRequestIn(
                self.action_id, self.patch_sha256 or "", request_key=f"{self.action_id}:approval:1"),
                timeout_s=30, retry=DB_RETRY)
        action_to = A.WITHDRAWN if self.action_state in (A.PROPOSED, A.TESTING, A.AWAITING_APPROVAL, A.APPROVED) else None
        reason = f"cancelled by {self.cancel.requested_by}: {self.cancel.reason}" if self.cancel else "cancelled"
        await self._to(I.CANCELLED, reason, action_to=action_to)
