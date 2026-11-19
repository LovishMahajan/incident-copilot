"""Artifact 8.2, 8.6, 13.5 T3.2, T3.6 (FR-25, FR-49, NFR-34, ADR-48, ADR-49): the workflow's
approval activities, `request_approval`, `escalate_approval` and `close_approval_request`.

Bound to the worker's engine (`aisre_app`). Each runs in one transaction under the incident's
row lock, and is idempotent: a request already open is returned as it is, a closed one keeps the
reason it has (write-once).

- `request_approval`, for a fix: the digest sealed when testing ended, `approvers_needed` derived
  by ADR-49's rule from trusted facts (never the action's flag alone), the request (escalates at
  15 minutes, expires at 60, NFR-34) and `aisre.approval.requested.v1`. It also creates the fix's
  rollback action, `proposed`, with its own open request, so an approver can sign the
  conditional approval of the rollback with the fix (ADR-48). For a rollback: a conditional
  approval already signed is used, else its request is replaced by a fresh one.
- `escalate_approval`: `aisre.approval.escalated.v1`.
- `close_approval_request`: `expired`, `withdrawn` or `voided`, with `aisre.approval.expired.v1`
  or `aisre.approval.voided.v1`. A void (ST-01, ST-02, ST-13) also counts in
  `aisre_approval_refused_total{reason="voided"}`, which pages security (ops/alerts.yml), and
  the workflow's hand-over names security and the approvers as its audience.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TypeVar
from uuid import UUID

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError
from temporalio import activity

from aisre.contracts import (
    ApprovalEscalatedData,
    ApprovalExpiredData,
    ApprovalRequestedData,
    ApprovalVoidedData,
)
from aisre.record import approvals as rec
from aisre.record.incidents import append_event
from aisre.reliability import telemetry
from aisre.remediation.action import ApprovalPolicy
from aisre.workflow import activities as act

R = TypeVar("R")
ACTOR = rec.REQUESTER
SOURCE = rec.WORKER_SOURCE


class ApprovalActivities:
    def __init__(
        self,
        engine: Engine,
        policy: ApprovalPolicy | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.engine, self.clock = engine, clock
        self.policy = policy or ApprovalPolicy()

    def all(self) -> list[Callable[..., Any]]:
        return [self.request_approval, self.escalate_approval, self.close_approval_request]

    async def _tx(self, fn: Callable[[Connection], R]) -> R:
        def run() -> R:
            try:
                with self.engine.begin() as conn:
                    return fn(conn)
            except DBAPIError as e:
                sqlstate = getattr(e.orig, "sqlstate", None) or ""
                raise act.db_error(sqlstate, str(e.orig)) from e

        return await asyncio.to_thread(run)

    @staticmethod
    def _incident(conn: Connection, action_id: UUID) -> UUID:
        incident_id = conn.execute(
            text("SELECT incident_id FROM actions WHERE action_id = :a"), {"a": action_id}
        ).scalar_one()
        conn.execute(
            text("SELECT 1 FROM incidents WHERE incident_id = :i FOR UPDATE"), {"i": incident_id}
        )
        return UUID(str(incident_id))

    def _needed(self, conn: Connection, action_id: UUID) -> int:
        action = rec.load_action(conn, action_id)
        assert action is not None
        return self.policy.approvers_needed(action, rec.impact(conn, action_id))

    def _open(self, conn: Connection, incident_id: UUID, action_id: UUID) -> rec.Request:
        """The action's open request, or a new one with its event."""
        found = rec.latest_request(conn, action_id)
        if found is not None and found.closed_at is None:
            return found
        action = rec.load_action(conn, action_id)
        assert action is not None
        digest = conn.execute(
            text("SELECT action_digest FROM actions WHERE action_id = :a"), {"a": action_id}
        ).scalar_one()
        now = self.clock()
        req = rec.open_request(
            conn,
            action_id,
            patch_sha256=action.patch_sha256,
            approvers_needed=self._needed(conn, action_id),
            now=now,
            escalate_after=self.policy.escalates,
            expires_after=self.policy.request_expires,
        )
        append_event(
            conn,
            incident_id,
            "aisre.approval.requested.v1",
            ACTOR,
            ApprovalRequestedData(
                approval_request_id=req.approval_request_id,
                action_id=action_id,
                action_digest=str(digest).strip(),
                patch_sha256=action.patch_sha256,
                requested_by=ACTOR,
                escalate_at=req.escalate_at,
                expires_at=req.expires_at,
                approvers_needed=req.approvers_needed,
            ),
            occurred_at=now,
            source=SOURCE,
        )
        return req

    @activity.defn(name="request_approval")
    async def request_approval(self, inp: act.ApprovalRequestIn) -> act.ApprovalRequestOut:
        action_id = UUID(inp.action_id)

        def run(conn: Connection) -> act.ApprovalRequestOut:
            incident_id = self._incident(conn, action_id)
            kind, digest = conn.execute(
                text("SELECT kind, action_digest FROM actions WHERE action_id = :a"),
                {"a": action_id},
            ).one()
            if kind != "rollback":
                req = self._open(conn, incident_id, action_id)
                rollback_id, rollback_digest = rec.create_rollback(conn, action_id, self.clock())
                self._open(conn, incident_id, rollback_id)  # for a conditional approval
                return act.ApprovalRequestOut(
                    str(req.approval_request_id),
                    req.approvers_needed,
                    str(rollback_id),
                    rollback_digest,
                    action_digest=str(digest).strip(),
                )
            signed = conn.execute(
                text(
                    "SELECT 1 FROM approval_decisions d WHERE d.action_id = :a AND d.conditional "
                    "AND NOT EXISTS (SELECT 1 FROM approval_decisions x "
                    "WHERE x.revokes_approval_id = d.approval_id)"
                ),
                {"a": action_id},
            ).first()
            found = rec.latest_request(conn, action_id)
            if signed is None and found is not None and found.closed_at is None:
                # Opened with the fix for a conditional row nobody signed: start the wait now.
                rec.close_request(conn, found.approval_request_id, "withdrawn", self.clock())
            req = self._open(conn, incident_id, action_id)
            return act.ApprovalRequestOut(
                str(req.approval_request_id),
                req.approvers_needed,
                conditional_approval=signed is not None,
                action_digest=str(digest).strip(),
            )

        return await self._tx(run)

    @activity.defn(name="escalate_approval")
    async def escalate_approval(self, inp: act.ApprovalRequestIn) -> None:
        action_id = UUID(inp.action_id)

        def run(conn: Connection) -> None:
            incident_id = self._incident(conn, action_id)
            req = rec.latest_request(conn, action_id)
            if req is None or req.closed_at is not None:
                return
            done = conn.execute(
                text(
                    "SELECT 1 FROM timeline_events WHERE incident_id = :i "
                    "AND type = 'aisre.approval.escalated.v1' "
                    "AND data->>'approval_request_id' = :r"
                ),
                {"i": incident_id, "r": str(req.approval_request_id)},
            ).first()
            if done is None:
                append_event(
                    conn,
                    incident_id,
                    "aisre.approval.escalated.v1",
                    ACTOR,
                    ApprovalEscalatedData(
                        approval_request_id=req.approval_request_id,
                        action_id=action_id,
                        escalate_at=req.escalate_at,
                    ),
                    occurred_at=self.clock(),
                    source=SOURCE,
                )

        await self._tx(run)

    @activity.defn(name="close_approval_request")
    async def close_approval_request(self, inp: act.ApprovalRequestIn) -> None:
        action_id = UUID(inp.action_id)
        reason = inp.close_reason or "withdrawn"

        def run(conn: Connection) -> bool:
            incident_id = self._incident(conn, action_id)
            req = rec.latest_request(conn, action_id)
            if req is None:
                return False
            now = self.clock()
            closed = rec.close_request(conn, req.approval_request_id, reason, now)
            if not closed and reason != act.VOIDED:
                return False
            if reason == "expired":
                append_event(
                    conn,
                    incident_id,
                    "aisre.approval.expired.v1",
                    ACTOR,
                    ApprovalExpiredData(
                        approval_request_id=req.approval_request_id,
                        action_id=action_id,
                        expires_at=req.expires_at,
                    ),
                    occurred_at=now,
                    source=SOURCE,
                )
            elif reason == act.VOIDED:
                # A request already closed by the approval itself is still voided on the record,
                # once.
                if conn.execute(
                    text(
                        "SELECT 1 FROM timeline_events WHERE incident_id = :i "
                        "AND type = 'aisre.approval.voided.v1' "
                        "AND data->>'approval_request_id' = :r"
                    ),
                    {"i": incident_id, "r": str(req.approval_request_id)},
                ).first():
                    return False
                append_event(
                    conn,
                    incident_id,
                    "aisre.approval.voided.v1",
                    ACTOR,
                    ApprovalVoidedData(
                        approval_request_id=req.approval_request_id,
                        action_id=action_id,
                        reason=inp.close_detail or "the approved action no longer matches",
                    ),
                    occurred_at=now,
                    source=SOURCE,
                )
                return True
            return False

        voided = await self._tx(run)
        if voided:
            telemetry.approval_refused.labels(reason=act.VOIDED).inc()
