"""Artifact 7.2, 11.6–11.8, 13.5 T3.2 (ADR-48, ADR-49, ADR-64): actions as Layer 11 sees them,
approval requests and signed decisions, in PostgreSQL.

`load_action` rebuilds an action's `action.ImmutableAction` from its row, its target's names and
its patch. The Approval Service, the workflow and the dispatcher all use it, so the digest an
approver signs, the digest stored when testing ends (`seal_digests`) and the digest the dispatcher
re-checks are one computation. It reads only what the dispatcher's role may read (migration 0009).

Decisions are written exactly as signed: timestamps go in as the UTC instants they were signed
as, and come back out in the same ISO form, so `action.verify_signature` passes on a stored row
and fails on an edited one (ST-01, ST-02). The signature is stored as bytes, with the key ID that
made it (ADR-73). Each function runs in the caller's transaction.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from aisre.contracts import ActionKind, ActionProposedData
from aisre.record.incidents import append_event, uuid7
from aisre.remediation.action import Decision, ImmutableAction, ImpactFacts, Target

REQUESTER = "system:workflow"  # who opens every request; nobody approves their own (ADR-49)
WORKER_SOURCE = "/aisre/worker/approvals"


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


def load_action(
    conn: Connection, action_id: UUID, *, test_bundle_digest: str | None = None
) -> ImmutableAction | None:
    """The immutable action for `action_id`. `test_bundle_digest` stands in for the stored one,
    for `seal_digests`, which computes the digest before it is written."""
    r = (
        conn.execute(
            text(
                "SELECT a.action_id, a.incident_id, a.diagnosis_id, a.kind, a.base_revision, "
                "a.expected_after_revision, a.target_deployment, a.policy_version, "
                "a.test_bundle_digest, a.recovery_predicates_digest, a.high_impact, "
                "a.hold_seconds, a.parent_action_id, s.slug AS service, e.slug AS environment, "
                "p.patch_sha256 FROM actions a "
                "JOIN services s ON s.service_id = a.target_service_id "
                "JOIN environments e ON e.environment_id = a.target_environment_id "
                "LEFT JOIN patches p ON p.action_id = a.action_id WHERE a.action_id = :a"
            ),
            {"a": action_id},
        )
        .mappings()
        .first()
    )
    if r is None:
        return None
    bundle = test_bundle_digest or r["test_bundle_digest"]
    return ImmutableAction(
        action_id=str(r["action_id"]),
        incident_id=str(r["incident_id"]),
        diagnosis_id=str(r["diagnosis_id"]),
        kind=str(r["kind"]),  # type: ignore[arg-type]  # the enum's values are ActionKind's
        patch_sha256=str(r["patch_sha256"]).strip() if r["patch_sha256"] else None,
        base_revision=str(r["base_revision"]),
        expected_after_revision=str(r["expected_after_revision"]),
        target=Target(str(r["service"]), str(r["environment"]), str(r["target_deployment"])),
        policy_version=str(r["policy_version"]),
        test_bundle_digest=str(bundle).strip() if bundle else "",
        recovery_predicates_digest=str(r["recovery_predicates_digest"]).strip(),
        high_impact=bool(r["high_impact"]),
        hold_seconds=int(r["hold_seconds"]),
        parent_action_id=str(r["parent_action_id"]) if r["parent_action_id"] else None,
    )


def impact(conn: Connection, action_id: UUID) -> ImpactFacts:
    """ADR-49's inputs, from trusted storage: confirmed severity, criticality, how many
    deployments the same patch is proposed for, and a rollback's parent kind."""
    r = (
        conn.execute(
            text(
                "SELECT i.confirmed_severity, s.critical, p.kind AS parent_kind, "
                "(SELECT count(DISTINCT o.target_deployment) FROM patches mine "
                " JOIN patches other ON other.patch_sha256 = mine.patch_sha256 "
                " JOIN actions o ON o.action_id = other.action_id "
                " WHERE mine.action_id = a.action_id AND o.incident_id = a.incident_id) AS n "
                "FROM actions a JOIN incidents i ON i.incident_id = a.incident_id "
                "JOIN services s ON s.service_id = a.target_service_id "
                "LEFT JOIN actions p ON p.action_id = a.parent_action_id WHERE a.action_id = :a"
            ),
            {"a": action_id},
        )
        .mappings()
        .one()
    )
    return ImpactFacts(
        severity=str(r["confirmed_severity"]) if r["confirmed_severity"] else None,
        critical=bool(r["critical"]),
        deployments=max(int(r["n"] or 0), 1),
        parent_kind=str(r["parent_kind"]) if r["parent_kind"] else None,
    )


def seal_digests(conn: Connection, action_id: UUID) -> dict[str, str]:
    """When testing ends (ADR-64): the passing run's bundle digest, and the action digest over it.
    record_transition writes both with `testing -> awaiting_approval`, in one statement."""
    bundle = conn.execute(
        text(
            "SELECT t.bundle_digest FROM test_runs t JOIN patches p ON p.action_id = t.action_id "
            "WHERE t.action_id = :a AND t.outcome = 'passed' AND t.reproduced "
            "AND t.patch_sha256 = p.patch_sha256 AND t.bundle_digest IS NOT NULL "
            "ORDER BY t.finished_at DESC LIMIT 1"
        ),
        {"a": action_id},
    ).scalar()
    if bundle is None:
        raise LookupError(f"action {action_id} has no passing test to seal")
    action = load_action(conn, action_id, test_bundle_digest=str(bundle).strip())
    assert action is not None
    return {"action_digest": action.digest, "test_bundle_digest": action.test_bundle_digest}


# ---------------------------------------------------------------------------
# Approval requests
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Request:
    approval_request_id: UUID
    action_id: UUID
    requested_at: datetime
    escalate_at: datetime
    expires_at: datetime
    approvers_needed: int
    closed_at: datetime | None
    close_reason: str | None


def _request(row: Any) -> Request:
    return Request(
        row["approval_request_id"],
        row["action_id"],
        row["requested_at"],
        row["escalate_at"],
        row["expires_at"],
        int(row["approvers_needed"]),
        row["closed_at"],
        row["close_reason"],
    )


def latest_request(conn: Connection, action_id: UUID) -> Request | None:
    row = (
        conn.execute(
            text(
                "SELECT * FROM approval_requests WHERE action_id = :a "
                "ORDER BY requested_at DESC, approval_request_id DESC LIMIT 1"
            ),
            {"a": action_id},
        )
        .mappings()
        .first()
    )
    return None if row is None else _request(row)


def open_request(
    conn: Connection,
    action_id: UUID,
    *,
    patch_sha256: str | None,
    approvers_needed: int,
    now: datetime,
    escalate_after: timedelta,
    expires_after: timedelta,
) -> Request:
    request_id = uuid7()
    conn.execute(
        text(
            "INSERT INTO approval_requests (approval_request_id, action_id, requested_by, "
            "patch_sha256, requested_at, escalate_at, expires_at, approvers_needed) VALUES "
            "(:r, :a, :by, :p, :now, :esc, :exp, :n)"
        ),
        {
            "r": request_id,
            "a": action_id,
            "by": REQUESTER,
            "p": patch_sha256,
            "now": now,
            "esc": now + escalate_after,
            "exp": now + expires_after,
            "n": approvers_needed,
        },
    )
    found = latest_request(conn, action_id)
    assert found is not None
    return found


def close_request(conn: Connection, request_id: UUID, reason: str, now: datetime) -> bool:
    """Write-once: a closed request keeps the reason it has. True if this call closed it."""
    return bool(
        conn.execute(
            text(
                "UPDATE approval_requests SET closed_at = :now, close_reason = :why "
                "WHERE approval_request_id = :r AND closed_at IS NULL"
            ),
            {"r": request_id, "why": reason, "now": now},
        ).rowcount
    )


# ---------------------------------------------------------------------------
# Signed decisions
# ---------------------------------------------------------------------------
def _decision(r: Any) -> Decision:
    return Decision(
        approval_id=str(r["approval_id"]),
        action_id=str(r["action_id"]),
        action_digest=str(r["action_digest"]).strip(),
        decision=str(r["decision"]),  # type: ignore[arg-type]
        subject=str(r["decided_by"]),
        role=str(r["decided_by_role"]),
        auth_time=_iso(r["auth_time"]) or "",
        mfa=bool(r["mfa"]),
        policy_version=str(r["policy_version"]),
        decided_at=_iso(r["decided_at"]) or "",
        valid_until=_iso(r["valid_until"]),
        reason=r["reason"],
        revokes=str(r["revokes_approval_id"]) if r["revokes_approval_id"] else None,
        conditional=bool(r["conditional"]),
        conditional_on_approval_id=(
            str(r["conditional_on_approval_id"]) if r["conditional_on_approval_id"] else None
        ),
        key_id=str(r["signing_key_id"] or ""),
        signature=base64.b64encode(bytes(r["signature"])).decode() if r["signature"] else "",
    )


def decisions(conn: Connection, action_id: UUID) -> list[Decision]:
    """Every decision on the action, oldest first, as signed."""
    rows = conn.execute(
        text(
            "SELECT * FROM approval_decisions WHERE action_id = :a ORDER BY decided_at, approval_id"
        ),
        {"a": action_id},
    ).mappings()
    return [_decision(r) for r in rows]


def decision_rows(conn: Connection, action_id: UUID) -> list[dict[str, Any]]:
    """The approval history for ApprovalDecisionView."""
    rows = conn.execute(
        text(
            "SELECT d.*, s.slug AS target_service, e.slug AS target_environment "
            "FROM approval_decisions d JOIN services s ON s.service_id = d.target_service_id "
            "JOIN environments e ON e.environment_id = d.target_environment_id "
            "WHERE d.action_id = :a ORDER BY d.decided_at, d.approval_id"
        ),
        {"a": action_id},
    ).mappings()
    return [dict(r) for r in rows]


def insert_decision(conn: Connection, d: Decision, request_id: UUID) -> None:
    """One signed row. The database checks it against the action, the request and ADR-49."""
    conn.execute(
        text(
            "INSERT INTO approval_decisions (approval_id, approval_request_id, action_id, "
            "decision, decided_by, decided_by_role, auth_time, mfa, policy_version, "
            "action_digest, patch_sha256, target_service_id, target_environment_id, reason, "
            "decided_at, valid_until, signature, signing_key_id, revokes_approval_id, "
            "conditional, conditional_on_approval_id) "
            "SELECT :id, :r, a.action_id, CAST(:d AS approval_decision), :by, :role, :auth, "
            ":mfa, :pol, :digest, p.patch_sha256, a.target_service_id, a.target_environment_id, "
            ":reason, :at, :until, :sig, :kid, :revokes, :cond, :cond_on FROM actions a "
            "LEFT JOIN patches p ON p.action_id = a.action_id WHERE a.action_id = :a"
        ),
        {
            "id": UUID(d.approval_id),
            "r": request_id,
            "a": UUID(d.action_id),
            "d": d.decision,
            "by": d.subject,
            "role": d.role or "none",
            "auth": datetime.fromisoformat(d.auth_time),
            "mfa": d.mfa,
            "pol": d.policy_version,
            "digest": d.action_digest,
            "reason": d.reason,
            "at": datetime.fromisoformat(d.decided_at),
            "until": datetime.fromisoformat(d.valid_until) if d.valid_until else None,
            "sig": base64.b64decode(d.signature) if d.signature else None,
            "kid": d.key_id or None,
            "revokes": UUID(d.revokes) if d.revokes else None,
            "cond": d.conditional,
            "cond_on": (
                UUID(d.conditional_on_approval_id) if d.conditional_on_approval_id else None
            ),
        },
    )


# ---------------------------------------------------------------------------
# The rollback action (ADR-48)
# ---------------------------------------------------------------------------
def rollback_of(conn: Connection, parent_id: UUID) -> tuple[UUID, str] | None:
    row = conn.execute(
        text(
            "SELECT action_id, action_digest FROM actions "
            "WHERE parent_action_id = :p AND kind = 'rollback'"
        ),
        {"p": parent_id},
    ).first()
    return None if row is None else (row[0], str(row[1]).strip())


def create_rollback(conn: Connection, parent_id: UUID, now: datetime) -> tuple[UUID, str]:
    """The fix's own undo, as its own action (ADR-48): status `proposed`, its digest set at
    creation, starting from the revision the fix produces and going back to the fix's base. It
    copies the fix's test, predicates, hold and high-impact flag. Idempotent on the parent."""
    found = rollback_of(conn, parent_id)
    if found is not None:
        return found
    fix = load_action(conn, parent_id)
    p = (
        conn.execute(
            text(
                "SELECT incident_id, target_service_id, target_environment_id, recovery_criteria "
                "FROM actions WHERE action_id = :a"
            ),
            {"a": parent_id},
        )
        .mappings()
        .one()
    )
    assert fix is not None
    rollback = ImmutableAction(
        action_id=str(uuid7()),
        incident_id=fix.incident_id,
        diagnosis_id=fix.diagnosis_id,
        kind="rollback",
        patch_sha256=None,
        base_revision=fix.expected_after_revision,
        expected_after_revision=fix.base_revision,
        target=fix.target,
        policy_version=fix.policy_version,
        test_bundle_digest=fix.test_bundle_digest,
        recovery_predicates_digest=fix.recovery_predicates_digest,
        high_impact=fix.high_impact,
        hold_seconds=fix.hold_seconds,
        parent_action_id=fix.action_id,
    )
    dep = fix.target.deployment
    conn.execute(
        text(
            "INSERT INTO actions (action_id, incident_id, diagnosis_id, parent_action_id, kind, "
            "target_service_id, target_environment_id, target_deployment, base_revision, "
            "expected_after_revision, description, expected_effect, risk, undo_plan, "
            "recovery_criteria, recovery_predicates_digest, hold_seconds, high_impact, "
            "policy_version, test_bundle_digest, action_digest) VALUES "
            "(:a, :i, :d, :p, 'rollback', :s, :e, :dep, :base, :after, :desc, :eff, :risk, "
            ":undo, CAST(:crit AS jsonb), :crit_sha, :hold, :hi, :pol, :bundle, :digest)"
        ),
        {
            "a": UUID(rollback.action_id),
            "i": p["incident_id"],
            "d": UUID(fix.diagnosis_id),
            "p": parent_id,
            "s": p["target_service_id"],
            "e": p["target_environment_id"],
            "dep": dep,
            "base": rollback.base_revision,
            "after": rollback.expected_after_revision,
            "desc": f"Roll back action {parent_id} on {dep}: undo to {fix.base_revision[:12]}",
            "eff": f"{dep} runs {fix.base_revision[:12]} again, as before the fix",
            "risk": "The fix did not hold; this puts back what ran before it, failure included",
            "undo": "Apply the fix again as a new action, with its own approval",
            "crit": json.dumps(p["recovery_criteria"], sort_keys=True, separators=(",", ":")),
            "crit_sha": rollback.recovery_predicates_digest,
            "hold": rollback.hold_seconds,
            "hi": rollback.high_impact,
            "pol": rollback.policy_version,
            "bundle": rollback.test_bundle_digest,
            "digest": rollback.digest,
        },
    )
    append_event(
        conn,
        p["incident_id"],
        "aisre.action.proposed.v1",
        REQUESTER,
        ActionProposedData(
            action_id=UUID(rollback.action_id),
            diagnosis_id=UUID(fix.diagnosis_id),
            parent_action_id=parent_id,
            kind=ActionKind.rollback,
            target_service=fix.target.service,
            target_environment=fix.target.environment,
            target_deployment=dep,
        ),
        occurred_at=now,
        source=WORKER_SOURCE,
    )
    return UUID(rollback.action_id), rollback.digest
