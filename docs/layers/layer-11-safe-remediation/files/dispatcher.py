"""Artifact 11.9–11.11: the trusted dispatcher contract, the idempotency ledger, and reconciliation.

The dispatcher accepts only (action_id, approval_ids). It reloads the immutable action and its
decisions from trusted storage, re-verifies everything, records intent in its ledger, and then
calls one allowlisted operation on the release process with the action ID as the execution key.
It never retries a write whose outcome is unknown: it reconciles.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Protocol

from action import ApprovalPolicy, Decision, Directory, ImmutableAction, Standing, evaluate


class Outcome(str, Enum):
    APPLIED = "applied"
    NOT_APPLIED = "not_applied"
    REFUSED = "refused"            # pre-checks failed; nothing sent
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TargetState:
    revision: str                  # what the target runs now
    in_progress_key: Optional[str] # a deployment currently running, and its key


class ReleaseProcess(Protocol):
    """The ONLY write interface (Q11.67). Two operations, both keyed; one read."""

    async def current_state(self, deployment: str) -> TargetState: ...
    async def apply(self, deployment: str, execution_key: str, base_revision: str, patch_digest: Optional[str],
                    to_revision: Optional[str]) -> str: ...            # returns "applied" | "rejected"
    async def lookup(self, deployment: str, execution_key: str) -> Optional[str]: ...   # None | "applied" | "in_progress" | "failed"


class TrustedStore(Protocol):
    def action(self, action_id: str) -> ImmutableAction: ...
    def decisions(self, action_id: str) -> list[Decision]: ...
    def requested_at(self, action_id: str) -> datetime: ...


# ---------------------------------------------------------------------------
# 11.10 Idempotency ledger
# ---------------------------------------------------------------------------
class LedgerState(str, Enum):
    INTENT = "intent_recorded"     # about to send
    APPLIED = "applied"
    NOT_APPLIED = "not_applied"
    UNKNOWN = "unknown"


@dataclass
class LedgerEntry:
    execution_key: str
    action_digest: str
    state: LedgerState
    attempts: int = 0
    external_ref: Optional[str] = None
    notes: list[str] = field(default_factory=list)


class Ledger:
    """Stand-in for execution_attempts (Artifact 7): one entry per execution key; at most one 'applied'."""

    def __init__(self):
        self.rows: dict[str, LedgerEntry] = {}

    def get(self, key: str) -> Optional[LedgerEntry]:
        return self.rows.get(key)

    def put(self, e: LedgerEntry) -> None:
        old = self.rows.get(e.execution_key)
        if old and old.state == LedgerState.APPLIED and e.state != LedgerState.APPLIED:
            raise RuntimeError("an applied execution can never change state")
        self.rows[e.execution_key] = e


@dataclass(frozen=True)
class DispatchResult:
    outcome: Outcome
    reason: str
    execution_key: str


class Dispatcher:
    def __init__(self, store: TrustedStore, release: ReleaseProcess, ledger: Ledger, keys, directory: Directory,
                 policy: ApprovalPolicy, clock, call_timeout_s: float = 30.0):
        self.store, self.release, self.ledger, self.keys = store, release, ledger, keys
        self.directory, self.policy, self.clock, self.timeout = directory, policy, clock, call_timeout_s

    async def execute(self, action_id: str) -> DispatchResult:
        """Q11.68: the caller supplies only the action ID; everything else is reloaded."""
        action = self.store.action(action_id)
        key = action.action_id                                     # stable execution key (Q11.70, Q11.71)

        prior = self.ledger.get(key)
        if prior:
            if prior.state == LedgerState.APPLIED:
                return DispatchResult(Outcome.APPLIED, "already applied (idempotent)", key)
            if prior.state in (LedgerState.UNKNOWN, LedgerState.INTENT):
                return await self.reconcile(action_id)             # Q11.78: never re-send
            if prior.state == LedgerState.NOT_APPLIED:
                return DispatchResult(Outcome.NOT_APPLIED, "previously confirmed not applied; needs a new action", key)

        refusal = await self._prechecks(action)
        if refusal:
            return DispatchResult(Outcome.REFUSED, refusal, key)

        entry = LedgerEntry(key, action.digest, LedgerState.INTENT, attempts=1)
        self.ledger.put(entry)                                     # intent BEFORE the write (Artifact 8)
        try:
            resp = await asyncio.wait_for(self.release.apply(
                action.target.deployment, key, action.base_revision, action.patch_digest,
                action.expected_after_revision), timeout=self.timeout)
        except (asyncio.TimeoutError, ConnectionError) as e:
            entry.state = LedgerState.UNKNOWN                      # Q11.75: a timeout is not a failure
            entry.notes.append(f"no answer: {type(e).__name__}")
            self.ledger.put(entry)
            return await self.reconcile(action_id)
        entry.state = LedgerState.APPLIED if resp == "applied" else LedgerState.NOT_APPLIED
        self.ledger.put(entry)
        return DispatchResult(Outcome.APPLIED if resp == "applied" else Outcome.NOT_APPLIED, f"release process: {resp}", key)

    async def _prechecks(self, a: ImmutableAction) -> Optional[str]:
        """Q11.69, Q11.73: everything re-validated immediately before the write."""
        decisions = self.store.decisions(a.action_id)
        standing, notes = evaluate(a, decisions, now=self.clock(), keys=self.keys, directory=self.directory,
                                   policy=self.policy, requested_at=self.store.requested_at(a.action_id))
        if standing != Standing.APPROVED:
            return f"approval {standing.value}: {'; '.join(notes)}"
        if a.policy_version.split("|")[-1] != self.policy.version:
            return f"action created under {a.policy_version}; current approval policy {self.policy.version}"
        st = await self.release.current_state(a.target.deployment)
        if st.in_progress_key and st.in_progress_key != a.action_id:
            return f"another deployment is in progress ({st.in_progress_key})"
        if st.revision != a.base_revision:
            return f"target runs {st.revision}, action was approved for {a.base_revision}"   # Q11.73
        return None

    async def reconcile(self, action_id: str, max_checks: int = 10, wait_s: float = 0.0) -> DispatchResult:
        """Q11.74, Q11.76: ask the release process by key, then compare the live revision."""
        action = self.store.action(action_id)
        key = action.action_id
        entry = self.ledger.get(key) or LedgerEntry(key, action.digest, LedgerState.UNKNOWN)
        for _ in range(max_checks):
            found = await self.release.lookup(action.target.deployment, key)
            st = await self.release.current_state(action.target.deployment)
            if found == "in_progress" or st.in_progress_key == key:
                await asyncio.sleep(wait_s)
                continue
            at_target = action.expected_after_revision is not None and st.revision == action.expected_after_revision
            at_base = st.revision == action.base_revision
            if found == "applied" and (at_target or action.expected_after_revision is None):
                entry.state = LedgerState.APPLIED
                self.ledger.put(entry)
                return DispatchResult(Outcome.APPLIED, "reconciled: applied", key)
            if found in (None, "failed") and at_base:
                entry.state = LedgerState.NOT_APPLIED
                self.ledger.put(entry)
                return DispatchResult(Outcome.NOT_APPLIED, "reconciled: not applied", key)
            entry.notes.append(f"disagreement: ledger={found}, revision={st.revision}")
            break
        entry.state = LedgerState.UNKNOWN
        self.ledger.put(entry)
        return DispatchResult(Outcome.UNKNOWN, "outcome still unknown; a person must check before any further change", key)
