"""Artifact 12.5, 12.6: the production-write fence used by failure handling, backups and restores.

One switch decides whether the dispatcher may write anywhere, plus per-deployment fences. Every
write checks the current fence epoch. Only a restore bumps the epoch, so any dispatcher holding an
old token is refused even if it 'thinks' writes are on (Q12.43, Q12.44). Other fences only set a
reason, and writes resume with the same token once it lifts.

Who may act (ADR-50): most fences lift when their cause clears, from a platform component. A manual
fence is set by an Incident lead or Platform admin, with a reason, and lifted by the Platform admin
with a lead's sign-off. An unknown-outcome fence lifts when an Incident lead records the actual
state (ADR-47). A restore fence lifts by complete_restore(): the lead signs, the Platform admin runs it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

INCIDENT_LEAD = "incident_lead"         # the values of action.Role
PLATFORM_ADMIN = "platform_admin"


class FenceReason(str, Enum):
    APPROVAL_STORE_DOWN = "approval_store_unavailable"      # Q12.31
    LEDGER_DOWN = "ledger_unavailable"                      # PostgreSQL down: no intent record possible (Q12.28)
    ARTIFACT_STORE_DOWN = "artifact_store_unavailable"      # can't verify patch content (Q12.29)
    WORKFLOW_ENGINE_DOWN = "workflow_engine_unavailable"    # Q12.30
    UNKNOWN_OUTCOME = "unknown_outcome_on_deployment"       # per deployment (Q12.33)
    RELEASE_PROCESS_DOWN = "release_process_unavailable"    # no lookup by key (Q12.33)
    CLOCK_DRIFT = "clock_drift"                             # beyond 5 s: expiry checks unreliable (Q12.33)
    BACKUP_QUIESCE = "backup_quiesce"                       # Q12.37
    RESTORED = "restored_pending_reconciliation"            # Q12.43, Q12.44
    MANUAL = "manual"


@dataclass(frozen=True)
class Actor:
    """A person and the role they act in, as the identity system reports it."""

    subject: str
    role: str


@dataclass(frozen=True)
class RecordedState:
    """What a person found on the target; the POST /v1/actions/{id}/outcome-resolutions body (ADR-49)."""

    by: Actor
    outcome: str                    # applied | not_applied
    observed_revision: str
    reason: str


@dataclass
class FenceState:
    epoch: int = 1
    global_reasons: set[FenceReason] = field(default_factory=set)
    deployment_reasons: dict[str, set[FenceReason]] = field(default_factory=dict)
    log: list[tuple[str, str]] = field(default_factory=list)
    manual: dict[str, tuple[str, str]] = field(default_factory=dict)    # scope -> (who set it, why)

    def writes_allowed(self, deployment: str, token_epoch: int) -> tuple[bool, str]:
        if token_epoch != self.epoch:
            return False, f"stale fence token (epoch {token_epoch}, current {self.epoch})"
        if self.global_reasons:
            return False, "writes disabled: " + ", ".join(sorted(r.value for r in self.global_reasons))
        if self.deployment_reasons.get(deployment):
            return False, f"{deployment} fenced: " + ", ".join(sorted(r.value for r in self.deployment_reasons[deployment]))
        return True, "ok"

    def fence(self, reason: FenceReason, deployment: Optional[str] = None, now: Optional[datetime] = None,
              actor: Optional[Actor] = None, why: str = "") -> None:
        if reason == FenceReason.MANUAL:
            if actor is None or actor.role not in (INCIDENT_LEAD, PLATFORM_ADMIN):
                raise PermissionError("only an incident lead or platform admin sets a manual fence")
            if not why:
                raise PermissionError("a manual fence records why it was set")
            self.manual[deployment or "global"] = (actor.subject, why)
        if deployment:
            self.deployment_reasons.setdefault(deployment, set()).add(reason)
        else:
            self.global_reasons.add(reason)
        who = f" by {actor.subject}: {why}" if actor else ""
        self.log.append(((now or datetime.now(timezone.utc)).isoformat(), f"fence {reason.value} {deployment or 'global'}{who}"))

    def lift(self, reason: FenceReason, deployment: Optional[str] = None, now: Optional[datetime] = None,
             actor: Optional[Actor] = None, sign_off: Optional[Actor] = None,
             recorded_state: Optional[RecordedState] = None) -> None:
        if reason == FenceReason.RESTORED:
            raise PermissionError("use complete_restore(); a restore fence lifts only after reconciliation")
        if reason == FenceReason.UNKNOWN_OUTCOME and not (
                recorded_state and recorded_state.by.role == INCIDENT_LEAD and recorded_state.outcome in ("applied", "not_applied")
                and recorded_state.observed_revision and recorded_state.reason):
            raise PermissionError("an incident lead must record the actual state before an unknown outcome lifts")
        if reason == FenceReason.MANUAL:
            if actor is None or actor.role != PLATFORM_ADMIN:
                raise PermissionError("only the platform admin lifts a manual fence")
            if sign_off is None or sign_off.role != INCIDENT_LEAD:
                raise PermissionError("lifting a manual fence needs an incident lead's sign-off")
            self.manual.pop(deployment or "global", None)
        (self.deployment_reasons.get(deployment, set()) if deployment else self.global_reasons).discard(reason)
        who = f" by {actor.subject}, signed off by {sign_off.subject}" if actor and sign_off else \
            f" by {recorded_state.by.subject}: {recorded_state.outcome} at {recorded_state.observed_revision}" if recorded_state else ""
        self.log.append(((now or datetime.now(timezone.utc)).isoformat(), f"lift {reason.value} {deployment or 'global'}{who}"))

    # -- restore (Q12.43–Q12.45) ------------------------------------------------
    def begin_restore(self) -> int:
        """Called first on a restored environment. New epoch invalidates every old token."""
        self.epoch += 1
        self.global_reasons.add(FenceReason.RESTORED)
        self.log.append((datetime.now(timezone.utc).isoformat(), f"restore began; epoch {self.epoch}"))
        return self.epoch

    def complete_restore(self, report: "ReconciliationReport", signed_by: Actor, run_by: Actor) -> None:
        if report.unresolved:
            raise PermissionError(f"{len(report.unresolved)} action(s) unresolved; writes stay disabled")
        if signed_by.role != INCIDENT_LEAD:
            raise PermissionError("an incident lead must sign off re-enabling writes")
        if run_by.role != PLATFORM_ADMIN:
            raise PermissionError("only the platform admin runs a restore")
        self.global_reasons.discard(FenceReason.RESTORED)
        self.log.append((datetime.now(timezone.utc).isoformat(),
                         f"restore completed; signed off by {signed_by.subject}, run by {run_by.subject}"))


@dataclass
class ActionRecord:
    action_id: str
    deployment: str
    ledger_state: str               # what the restored database says: started | applied | not_applied | unknown | pending
    expected_after_revision: str    # required on every action, so the lookup alone never gives applied (C17)
    base_revision: str


@dataclass
class ReconciliationReport:
    resolved: dict[str, str] = field(default_factory=dict)     # action_id -> applied | not_applied | void
    unresolved: list[str] = field(default_factory=list)


def reconcile_after_restore(actions: list[ActionRecord], target_lookup, target_revision,
                            recheck_every_s: float = 60.0, recheck_limit_s: float = 900.0,
                            sleep=time.sleep) -> ReconciliationReport:
    """Q12.45: the external system is the source of truth for what happened externally, so every
    action asks the target, whatever the restored ledger says.
    target_lookup(deployment, key) -> None | 'applied' | 'in_progress' | 'failed'
    target_revision(deployment) -> current revision.
    An action still in progress is re-checked for up to 15 minutes, then a person decides."""
    rep = ReconciliationReport()
    waiting, waited = actions, 0.0
    while True:
        in_progress = []
        for a in waiting:
            found, rev = target_lookup(a.deployment, a.action_id), target_revision(a.deployment)
            if found == "in_progress":
                in_progress.append(a)
            elif found == "applied" and a.ledger_state != "not_applied" and rev == a.expected_after_revision:
                rep.resolved[a.action_id] = "applied"        # the database said pending: the target wins
            elif found in (None, "failed") and rev == a.base_revision and a.ledger_state != "applied":
                rep.resolved[a.action_id] = "void" if a.ledger_state == "pending" else "not_applied"
            else:
                rep.unresolved.append(a.action_id)             # disagreement: a person decides
        if not in_progress or waited >= recheck_limit_s:
            rep.unresolved.extend(a.action_id for a in in_progress)
            return rep
        sleep(recheck_every_s)
        waited += recheck_every_s
        waiting = in_progress
