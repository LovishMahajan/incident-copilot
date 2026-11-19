"""Artifact 11.9–11.11: the trusted dispatcher contract, the idempotency ledger, and reconciliation.

The dispatcher accepts only `action_id`. It reloads the immutable action, its status, its decisions
and the stored patch from trusted storage, re-verifies everything, checks the write fence, records
intent in its ledger, and then calls one allowlisted operation on the release process with the
action ID as the execution key. It never retries a write whose outcome is unknown: it reconciles,
and that outcome blocks the deployment until reconciliation or a person settles it. A `started`
row may belong to a call still running, so it counts as dead only after the in-flight window.
The block is its own record per deployment (ADR-69): a call that lands after reconciliation
recorded `not_applied` puts it back, and then only a person's outcome resolution clears it.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, Protocol

from aisre.remediation.action import (
    TAMPERED,
    ApprovalError,
    ApprovalPolicy,
    Decision,
    Directory,
    ImmutableAction,
    ImpactFacts,
    Perm,
    Standing,
    evaluate,
)


class Outcome(str, Enum):
    APPLIED = "applied"
    NOT_APPLIED = "not_applied"
    REFUSED = "refused"  # pre-checks failed; nothing sent
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TargetState:
    revision: str  # what the target runs now
    in_progress_key: Optional[str]  # a deployment currently running, and its key


class ReleaseProcess(Protocol):
    """The ONLY write interface (Q11.67): three keyed operations (apply, undo through apply, lookup)
    plus unkeyed `current_state`. An undo is a rollback action's own apply, with no patch_sha256."""

    async def current_state(self, deployment: str) -> TargetState: ...
    async def apply(
        self,
        deployment: str,
        execution_key: str,
        base_revision: str,
        patch_sha256: Optional[str],
        to_revision: str,
    ) -> str: ...  # "applied" | "rejected" | "in_progress"
    async def lookup(
        self, deployment: str, execution_key: str
    ) -> Optional[str]: ...  # None | "applied" | "in_progress" | "failed"


class TrustedStore(Protocol):
    def action(self, action_id: str) -> ImmutableAction: ...
    def decisions(self, action_id: str) -> list[Decision]: ...
    def requested_at(self, action_id: str) -> datetime: ...
    def verdict(self, action_id: str) -> Optional[str]: ...  # recorded verification verdict, if any
    def status(self, action_id: str) -> str: ...  # the action's current status
    def impact(self, action_id: str) -> ImpactFacts: ...  # input to the high-impact rule (ADR-49)
    def stop(self, action_id: str) -> Optional[str]:
        ...  # who raised a stop on its incident since the
        # action was created, if anyone (ADR-69)

    def patch_content(self, patch_sha256: str) -> bytes: ...  # the bytes in the artifact store


class WriteFence(Protocol):
    """fence.FenceState: one switch, plus per-deployment fences. Layer 12 owns it."""

    epoch: int

    def writes_allowed(self, deployment: str, token_epoch: int) -> tuple[bool, str]: ...


# ---------------------------------------------------------------------------
# 11.10 Idempotency ledger
# ---------------------------------------------------------------------------
class LedgerState(str, Enum):
    STARTED = "started"  # intent: about to send
    APPLIED = "applied"
    NOT_APPLIED = "not_applied"
    UNKNOWN = "unknown"


ACTION_STATUS = {
    LedgerState.STARTED: "applying",
    LedgerState.APPLIED: "executed",
    LedgerState.NOT_APPLIED: "not_applied",
    LedgerState.UNKNOWN: "outcome_unknown",
}


@dataclass(frozen=True)
class OutcomeResolution:
    """What a person found on the target (11.11). Stays with the attempt."""

    subject: str
    outcome: str
    observed_revision: str
    reason: str


@dataclass(frozen=True)
class LedgerEntry:
    """One execution_attempts row: same key on every attempt, increasing attempt_no (ADR-47)."""

    execution_key: str
    action_digest: str
    deployment: str
    approval_id: str  # the approval that completed the count, for audit
    attempt_no: int
    state: LedgerState
    reconciliation: bool = False  # a looked-up result; nothing was sent
    resolution: Optional[OutcomeResolution] = None
    started_at: Optional[datetime] = None
    external_ref: Optional[str] = None
    notes: list[str] = field(default_factory=list)
    attempt_id: Optional[str] = None  # the row's ID, once stored (record.execution.PgLedger)


LATE_APPLY = "late_apply"  # deployment_blocks.reason; the other is `unresolved`


@dataclass
class DeploymentBlock:
    """One deployment_blocks row (ADR-69). An `unresolved` block opens with intent and clears when
    its attempt settles. A `late_apply` block is cleared only by a person's outcome resolution."""

    deployment: str
    execution_key: str
    reason: str = "unresolved"
    resolution: Optional[OutcomeResolution] = None
    cleared: bool = False


class Ledger:
    """Stand-in for execution_attempts and deployment_blocks (Artifact 7), plus the only other things
    the dispatcher writes (ADR-51): an action's status and version, in the same transaction as the
    attempt row, and its own timeline events. A row is final once it leaves `started`; at most one is
    'applied' per key."""

    def __init__(self):
        self.rows: dict[str, list[LedgerEntry]] = {}
        self.blocks: list[DeploymentBlock] = []
        self.action_status: dict[str, str] = {}
        self.action_version: dict[str, int] = {}
        self.events: list[dict] = []

    def get(self, key: str) -> Optional[LedgerEntry]:
        rows = self.rows.get(key)
        return rows[-1] if rows else None

    def put(self, e: LedgerEntry) -> None:
        rows = self.rows.setdefault(e.execution_key, [])
        if rows and e.attempt_no == rows[-1].attempt_no:
            if rows[-1].state != LedgerState.STARTED:
                raise RuntimeError("an attempt is final once it leaves started")
            rows[-1] = e
        elif e.attempt_no != len(rows) + 1:
            raise RuntimeError(f"attempt_no must be {len(rows) + 1}")
        elif any(r.state == LedgerState.APPLIED for r in rows):
            raise RuntimeError("an applied execution can never change state")
        else:
            rows.append(e)
        status = ACTION_STATUS[e.state]
        if self.action_status.get(e.execution_key) != status:
            self.action_status[e.execution_key] = status
            self.action_version[e.execution_key] = self.action_version.get(e.execution_key, 1) + 1
        # The block follows the attempt, as track_deployment_block does in schema.sql.
        open_ = [
            b
            for b in self.unresolved(e.deployment)
            if b.execution_key == e.execution_key and b.reason != LATE_APPLY
        ]
        if e.state in (LedgerState.STARTED, LedgerState.UNKNOWN):
            if not open_:
                self.blocks.append(DeploymentBlock(e.deployment, e.execution_key))
        else:
            for b in open_:
                b.cleared, b.resolution = True, e.resolution

    def unresolved(self, deployment: str) -> list[DeploymentBlock]:
        """ADR-47, ADR-69: the open blocks on this deployment. Read from the block records, not
        worked out from attempt rows, so a late apply can block after `not_applied`."""
        return [b for b in self.blocks if b.deployment == deployment and not b.cleared]

    def block_late_apply(self, e: LedgerEntry) -> None:
        """A call answered after reconciliation settled its key as not_applied. No attempt row can
        follow, so the block goes back on its own; once per key."""
        if not any(
            b.execution_key == e.execution_key and b.reason == LATE_APPLY for b in self.blocks
        ):
            self.blocks.append(DeploymentBlock(e.deployment, e.execution_key, LATE_APPLY))

    def clear_late_apply(self, execution_key: str, resolution: OutcomeResolution) -> bool:
        """A person's resolution of a late apply's block; it stays on the block (ADR-69)."""
        for b in self.blocks:
            if b.execution_key == execution_key and b.reason == LATE_APPLY and not b.cleared:
                b.cleared, b.resolution = True, resolution
                return True
        return False

    def begin(self, e: LedgerEntry) -> Optional[str]:
        """Check 9 and the intent write, as one transaction: there is no await between them, and
        the database holds a per-deployment lock. Returns a refusal, or None if intent is recorded."""
        blocked = self.unresolved(e.deployment)
        if blocked:
            return f"unresolved change on {e.deployment} ({blocked[0].execution_key}); a person must check (ADR-47)"
        if self.get(e.execution_key):
            return "execution already started for this key"
        self.put(e)
        return None


@dataclass(frozen=True)
class DispatchResult:
    outcome: Outcome
    reason: str
    execution_key: str
    intent_recorded: bool = True
    void_approval: bool = (
        False  # what was approved isn't what is stored, or the target moved on: the
    )
    # workflow closes the request as voided and tells security (ST-01, ST-02, ST-13)


class ReleaseInProgress(Exception):
    """Retryable: reconcile_execution's READ_RETRY is the wait between checks, up to the 10-minute limit."""


class Dispatcher:
    def __init__(
        self,
        store: TrustedStore,
        release: ReleaseProcess,
        ledger: Ledger,
        keys,
        directory: Directory,
        policy: ApprovalPolicy,
        clock,
        call_timeout_s: float = 30.0,
        *,
        fence: WriteFence,
    ):
        self.store, self.release, self.ledger, self.keys = store, release, ledger, keys
        self.directory, self.policy, self.clock, self.timeout = (
            directory,
            policy,
            clock,
            call_timeout_s,
        )
        self.fence, self.fence_token = (
            fence,
            fence.epoch,
        )  # a restore's new epoch makes this token stale
        # The longest a live call can hold a `started` row: its two timed calls, plus the 5 s of
        # clock drift the fence allows. Past this, the row belongs to a dispatcher that died.
        self.in_flight_window = timedelta(seconds=2 * call_timeout_s + 5)

    async def execute(self, action_id: str) -> DispatchResult:
        """Q11.68: the caller supplies only the action ID; everything else is reloaded."""
        action = self.store.action(action_id)
        key = action.action_id  # stable execution key (Q11.70, ADR-47)

        prior = self.ledger.get(key)
        if prior:
            if prior.state == LedgerState.APPLIED:
                return DispatchResult(Outcome.APPLIED, "already applied (idempotent)", key)
            if prior.state in (LedgerState.UNKNOWN, LedgerState.STARTED):
                return await self._reconcile_once(action_id)  # Q11.78: never re-send
            if prior.state == LedgerState.NOT_APPLIED:
                return DispatchResult(
                    Outcome.NOT_APPLIED, "previously confirmed not applied; needs a new action", key
                )

        refusal, approval_id, void = await self._prechecks(action)
        refusal = refusal or self._fenced(action)  # again: no await from here to the intent write
        if refusal:
            return self._refused(action, refusal, void)

        entry = LedgerEntry(
            key,
            action.digest,
            action.target.deployment,
            approval_id,
            1,
            LedgerState.STARTED,
            started_at=self.clock(),
        )
        refusal = self.ledger.begin(entry)  # intent BEFORE the write (Artifact 8)
        if refusal:
            return self._refused(action, refusal)
        try:
            resp = await asyncio.wait_for(
                self.release.apply(
                    action.target.deployment,
                    key,
                    action.base_revision,
                    action.patch_sha256,
                    action.expected_after_revision,
                ),
                timeout=self.timeout,
            )
        except (asyncio.TimeoutError, ConnectionError) as e:
            self._record(
                entry,
                LedgerState.UNKNOWN,
                "no answer",  # Q11.75: a timeout is not a failure
                f"no answer: {type(e).__name__}",
            )
            return await self._reconcile_once(action_id)
        if resp == "rejected":
            return self._record(entry, LedgerState.NOT_APPLIED, f"release process: {resp}")
        if resp != "applied":  # e.g. "in_progress": neither a yes nor a no
            self._record(entry, LedgerState.UNKNOWN, "no clear answer", f"apply answered {resp}")
            return await self._reconcile_once(action_id)
        try:
            live = (
                await asyncio.wait_for(
                    self.release.current_state(action.target.deployment), self.timeout
                )
            ).revision
        except (asyncio.TimeoutError, ConnectionError) as e:
            live = f"unreadable ({type(e).__name__})"
        if live != action.expected_after_revision:  # ST-40: don't take the release process's word
            note = f"release said applied, target runs {live}, expected {action.expected_after_revision}"
            return self._record(
                entry,
                LedgerState.UNKNOWN,
                f"{note}; a person must check before any further change",
                note,
            )
        return self._record(entry, LedgerState.APPLIED, f"release process: {resp}")

    def _record(
        self, entry: LedgerEntry, state: LedgerState, reason: str, note: Optional[str] = None
    ) -> DispatchResult:
        """Writes this call's result. If this call outlived the in-flight window, a reconciliation
        may hold the row already. Attempts are final, so it writes nothing and reports what stands."""
        key = entry.execution_key
        current = self.ledger.get(key)
        if current.state == LedgerState.STARTED:
            self.ledger.put(replace(entry, state=state, notes=[note] if note else []))
            return DispatchResult(Outcome(state.value), reason, key)
        if current.state == LedgerState.NOT_APPLIED and state != LedgerState.NOT_APPLIED:
            self.ledger.block_late_apply(entry)  # the late apply: the block goes back (ADR-69)
            return DispatchResult(
                Outcome.UNKNOWN,
                f"{reason}, but reconciliation recorded not_applied; "
                "a person must check, and the deployment is blocked until they do",
                key,
            )
        return DispatchResult(
            Outcome(current.state.value), f"already settled: {current.state.value}", key
        )

    def _refused(self, a: ImmutableAction, reason: str, void: bool = False) -> DispatchResult:
        """Nothing was sent, so no attempt row: only the event (Artifact 7). `void` goes back to the
        workflow, which may write approval events; the dispatcher's role may not (ADR-51)."""
        self.ledger.events.append(
            {
                "type": "aisre.execution.refused.v1",
                "data": {"action_id": a.action_id, "execution_key": a.action_id, "reason": reason},
            }
        )
        return DispatchResult(
            Outcome.REFUSED, reason, a.action_id, intent_recorded=False, void_approval=void
        )

    def _fenced(self, a: ImmutableAction) -> Optional[str]:
        """Fence 12.5: the current epoch and every reason, global or for this deployment."""
        allowed, why = self.fence.writes_allowed(a.target.deployment, self.fence_token)
        return None if allowed else why

    def _patch_mismatch(self, a: ImmutableAction) -> tuple[Optional[str], bool]:
        """ST-13: hash the stored bytes. The approved fingerprint proves nothing about them until
        they are read. A store that can't answer is a refusal, as is a fix with no patch. Returns
        (refusal, void): only bytes that hash to something else void the approval."""
        if a.patch_sha256 is None:
            return (
                None if a.kind in ("release_rollback", "rollback") else f"a {a.kind} needs a patch"
            ), False
        try:
            content = self.store.patch_content(a.patch_sha256)
        except (KeyError, OSError) as e:
            return f"patch content can't be read ({type(e).__name__})", False
        found = hashlib.sha256(content).hexdigest()
        if found == a.patch_sha256:
            return None, False
        return f"patch content is {found}, approved as {a.patch_sha256}", True

    async def _prechecks(self, a: ImmutableAction) -> tuple[Optional[str], Optional[str], bool]:
        """Q11.69, Q11.73: re-validated immediately before the write. Returns (refusal, deciding
        approval_id, whether the refusal voids the approval). The deployment block, check 9, is in
        Ledger.begin, with the intent write."""
        fenced = self._fenced(a)
        if fenced:
            return fenced, None, False
        status = self.store.status(a.action_id)
        if status != "approved":  # withdrawn, expired: refused here, not only by the table
            return f"action is {status}, not approved", None, False
        stopped_by = (
            None if a.parent_action_id else self.store.stop(a.action_id)
        )  # a rollback is never stopped
        if stopped_by:
            return (
                f"stop requested by {stopped_by}; no approval was withdrawn (ADR-69)",
                None,
                False,
            )
        decisions = self.store.decisions(a.action_id)
        verdict = self.store.verdict(a.parent_action_id) if a.parent_action_id else None
        standing, notes, approval_id = evaluate(
            a,
            decisions,
            now=self.clock(),
            keys=self.keys,
            directory=self.directory,
            policy=self.policy,
            requested_at=self.store.requested_at(a.action_id),
            impact=self.store.impact(a.action_id),
            parent_verdict=verdict,
        )
        tampered = any(
            TAMPERED in n for n in notes
        )  # ST-01, ST-02: a forged or edited row is stored
        if standing != Standing.APPROVED:
            return f"approval {standing.value}: {'; '.join(notes)}", None, tampered
        if (
            a.parent_action_id and verdict != "failed"
        ):  # ST-39: any approval, plain or conditional (ADR-48)
            return f"a rollback needs a failed parent; its verdict is {verdict}", None, False
        if a.policy_version != self.policy.pair:  # edit and approval policy both (C19)
            return (
                f"action created under {a.policy_version}; current policy {self.policy.pair}",
                None,
                False,
            )
        mismatch, void = self._patch_mismatch(a)
        if mismatch:
            return mismatch, None, void
        st = await self.release.current_state(a.target.deployment)
        if st.in_progress_key and st.in_progress_key != a.action_id:
            return f"another deployment is in progress ({st.in_progress_key})", None, False
        if st.revision != a.base_revision:  # Q11.73: the approval was for another state
            return (
                f"target runs {st.revision}, action was approved for {a.base_revision}",
                None,
                True,
            )
        return None, approval_id, False

    async def _reconcile_once(self, action_id: str) -> DispatchResult:
        """Dispatch's own look: if it can't settle now, the answer is `unknown` and reconcile_execution
        takes over. The row stays as it is: `unknown`, or `started` while a live call may hold it."""
        try:
            return await self.reconcile(action_id)
        except (ReleaseInProgress, asyncio.TimeoutError, ConnectionError) as e:
            return DispatchResult(
                Outcome.UNKNOWN,
                f"not settled yet: {type(e).__name__}",
                self.store.action(action_id).action_id,
            )

    def _settle(
        self,
        entry: LedgerEntry,
        state: LedgerState,
        reason: str,
        resolution: Optional[OutcomeResolution] = None,
    ) -> DispatchResult:
        """An unknown attempt stays frozen: the result is a new row marked reconciliation (ADR-47)."""
        self.ledger.put(
            replace(
                entry,
                attempt_no=entry.attempt_no + 1,
                state=state,
                reconciliation=True,
                resolution=resolution,
                notes=[],
            )
        )
        outcome = Outcome.APPLIED if state == LedgerState.APPLIED else Outcome.NOT_APPLIED
        return DispatchResult(outcome, reason, entry.execution_key)

    async def reconcile(self, action_id: str) -> DispatchResult:
        """Q11.74, Q11.76: ask the release process by key, then compare the live revision. Both must
        agree. One look per call: raises ReleaseInProgress while the release is still running, and
        while a `started` row is inside the in-flight window, when no answer can mean `not_applied`
        (ST-37). A live call can still land, so only a person or a later look may close the row."""
        action = self.store.action(action_id)
        key = action.action_id
        entry = self.ledger.get(key)
        if entry is None:
            return DispatchResult(
                Outcome.NOT_APPLIED, "no intent: nothing was sent", key, intent_recorded=False
            )
        if entry.state in (LedgerState.APPLIED, LedgerState.NOT_APPLIED):
            return DispatchResult(Outcome(entry.state.value), "already settled", key)
        if entry.state == LedgerState.STARTED:
            if entry.started_at and self.clock() - entry.started_at < self.in_flight_window:
                raise ReleaseInProgress(key)  # the first call may be live; the block stays
            # the dispatcher died mid-call (Artifact 8, 8.9)
            entry = replace(
                entry, state=LedgerState.UNKNOWN, notes=["no answer: found still started"]
            )
            self.ledger.put(entry)
        found = await self.release.lookup(action.target.deployment, key)
        st = await self.release.current_state(action.target.deployment)
        if found == "in_progress" or st.in_progress_key == key:
            raise ReleaseInProgress(key)
        if found == "applied" and st.revision == action.expected_after_revision:
            return self._settle(entry, LedgerState.APPLIED, "reconciled: applied")
        if found in (None, "failed") and st.revision == action.base_revision:
            return self._settle(entry, LedgerState.NOT_APPLIED, "reconciled: not applied")
        return DispatchResult(
            Outcome.UNKNOWN,
            f"outcome still unknown (release: {found}, target: {st.revision}); "
            "a person must check before any further change",
            key,
        )

    def resolve_outcome(
        self,
        action_id: str,
        *,
        subject: str,
        outcome: str,
        observed_revision: str,
        reason: str,
        auth_time: datetime,
        mfa: bool,
    ) -> DispatchResult:
        """POST /v1/actions/{id}/outcome-resolutions (11.11): the incident lead records what the target
        actually runs. That clears the deployment's block, and the action leaves outcome_unknown.
        It is also the only thing that clears a late apply's block (ADR-69)."""
        action = self.store.action(action_id)
        now = self.clock()
        if now - auth_time > self.policy.max_auth_age or not mfa:
            raise ApprovalError("reauth_required", "sign in again with two-factor")
        if not self.directory.can(subject, action.target.service, Perm.RESOLVE_OUTCOME):
            raise ApprovalError(
                "forbidden", f"{subject} may not resolve outcomes for {action.target.service}"
            )
        if not reason:
            raise ApprovalError("reason_required")
        if outcome not in ("applied", "not_applied") or not observed_revision:
            raise ApprovalError(
                "invalid_request", "outcome is applied or not_applied, with the observed revision"
            )
        entry = self.ledger.get(action.action_id)
        late = [
            b
            for b in self.ledger.unresolved(action.target.deployment)
            if b.execution_key == action.action_id and b.reason == LATE_APPLY
        ]
        if late:  # attempts are final after not_applied: the resolution stays on the block
            self.ledger.clear_late_apply(
                action.action_id, OutcomeResolution(subject, outcome, observed_revision, reason)
            )
            return DispatchResult(
                Outcome(outcome), f"late apply resolved by {subject}", action.action_id
            )
        if entry is None or entry.state != LedgerState.UNKNOWN:
            raise ApprovalError("action_not_open", "this action has no unknown outcome")
        return self._settle(
            entry,
            LedgerState(outcome),
            f"resolved by {subject}",
            OutcomeResolution(subject, outcome, observed_revision, reason),
        )
