"""Artifact 12.5, 12.6: the production-write fence used by failure handling, backups and restores.

One switch decides whether the dispatcher may write anywhere, plus per-deployment fences. Every
write checks the current fence epoch; a restore or a critical failure bumps the epoch, so any
dispatcher holding an old token is refused even if it 'thinks' writes are on (Q12.43, Q12.44).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class FenceReason(str, Enum):
    APPROVAL_STORE_DOWN = "approval_store_unavailable"      # Q12.31
    LEDGER_DOWN = "ledger_unavailable"                      # PostgreSQL down: no intent record possible (Q12.28)
    ARTIFACT_STORE_DOWN = "artifact_store_unavailable"      # can't verify patch content (Q12.29)
    WORKFLOW_ENGINE_DOWN = "workflow_engine_unavailable"    # Q12.30
    UNKNOWN_OUTCOME = "unknown_outcome_on_deployment"       # per deployment (Q12.33)
    BACKUP_QUIESCE = "backup_quiesce"                       # Q12.37
    RESTORED = "restored_pending_reconciliation"            # Q12.43, Q12.44
    MANUAL = "manual"


@dataclass
class FenceState:
    epoch: int = 1
    global_reasons: set[FenceReason] = field(default_factory=set)
    deployment_reasons: dict[str, set[FenceReason]] = field(default_factory=dict)
    log: list[tuple[str, str]] = field(default_factory=list)

    def writes_allowed(self, deployment: str, token_epoch: int) -> tuple[bool, str]:
        if token_epoch != self.epoch:
            return False, f"stale fence token (epoch {token_epoch}, current {self.epoch})"
        if self.global_reasons:
            return False, "writes disabled: " + ", ".join(sorted(r.value for r in self.global_reasons))
        if self.deployment_reasons.get(deployment):
            return False, f"{deployment} fenced: " + ", ".join(sorted(r.value for r in self.deployment_reasons[deployment]))
        return True, "ok"

    def fence(self, reason: FenceReason, deployment: Optional[str] = None, now: Optional[datetime] = None) -> None:
        if deployment:
            self.deployment_reasons.setdefault(deployment, set()).add(reason)
        else:
            self.global_reasons.add(reason)
        self.log.append(((now or datetime.now(timezone.utc)).isoformat(), f"fence {reason.value} {deployment or 'global'}"))

    def lift(self, reason: FenceReason, deployment: Optional[str] = None, now: Optional[datetime] = None) -> None:
        if reason == FenceReason.RESTORED:
            raise PermissionError("use complete_restore(); a restore fence lifts only after reconciliation")
        (self.deployment_reasons.get(deployment, set()) if deployment else self.global_reasons).discard(reason)
        self.log.append(((now or datetime.now(timezone.utc)).isoformat(), f"lift {reason.value} {deployment or 'global'}"))

    # -- restore (Q12.43–Q12.45) ------------------------------------------------
    def begin_restore(self) -> int:
        """Called first on a restored environment. New epoch invalidates every old token."""
        self.epoch += 1
        self.global_reasons.add(FenceReason.RESTORED)
        self.log.append((datetime.now(timezone.utc).isoformat(), f"restore began; epoch {self.epoch}"))
        return self.epoch

    def complete_restore(self, report: "ReconciliationReport", approved_by: str) -> None:
        if report.unresolved:
            raise PermissionError(f"{len(report.unresolved)} action(s) unresolved; writes stay disabled")
        if not approved_by:
            raise PermissionError("an incident lead must approve re-enabling writes")
        self.global_reasons.discard(FenceReason.RESTORED)
        self.log.append((datetime.now(timezone.utc).isoformat(), f"restore completed; writes re-enabled by {approved_by}"))


@dataclass
class ActionRecord:
    action_id: str
    deployment: str
    ledger_state: str               # what the restored database says: intent_recorded | applied | not_applied | unknown | pending
    expected_after_revision: Optional[str]
    base_revision: str


@dataclass
class ReconciliationReport:
    resolved: dict[str, str] = field(default_factory=dict)     # action_id -> applied | not_applied | void
    unresolved: list[str] = field(default_factory=list)


def reconcile_after_restore(actions: list[ActionRecord], target_lookup, target_revision) -> ReconciliationReport:
    """Q12.45: the external system is the source of truth for what happened externally.
    target_lookup(deployment, key) -> None | 'applied' | 'in_progress' | 'failed'
    target_revision(deployment) -> current revision."""
    rep = ReconciliationReport()
    for a in actions:
        if a.ledger_state in ("not_applied",):
            rep.resolved[a.action_id] = "not_applied"
            continue
        found, rev = target_lookup(a.deployment, a.action_id), target_revision(a.deployment)
        if found == "applied" and (a.expected_after_revision is None or rev == a.expected_after_revision):
            rep.resolved[a.action_id] = "applied"            # the database said pending: the target wins
        elif found in (None, "failed") and rev == a.base_revision and a.ledger_state != "applied":
            rep.resolved[a.action_id] = "void" if a.ledger_state == "pending" else "not_applied"
        else:
            rep.unresolved.append(a.action_id)                 # disagreement or in progress: a person decides
    return rep
