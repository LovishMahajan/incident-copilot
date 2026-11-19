"""Artifact 7.4, 11.6, 13.5 T3.2, T3.3 (FR-24 to FR-26, FR-49, FR-52, FR-54, ADR-49, ADR-73): the
Approval Service's routes.

    GET  /v1/actions/{id}/approval-request   what the approver reviews (FR-25)    incident:read
    GET  /v1/actions/{id}/approvals          the approval history                 incident:read
    POST /v1/actions/{id}/approvals          approve (+ the conditional rollback)  action:approve
    POST /v1/actions/{id}/rejections         reject, with a reason                 action:reject
    POST /v1/actions/{id}/revocations        withdraw your own approval            approval:revoke_own

Every write takes an `Idempotency-Key` and `If-Match` (the action's version, ADR-64), and runs
in one transaction: the action row locked, the key claimed, `action.record_decision` (the
Layer 11 rules: identity, rights, recent sign-in with two-factor, no self-approval, the exact
digest, the request open, nothing after intent), the signed rows, their events and the
`approval_decided` signal to the workflow through the outbox. Rights come from the caller's
identity groups on the action's service (ADR-73), never from a name.

A refusal writes no decision. Its transaction is rolled back, and `aisre.approval.refused.v1`
is written on its own, with `aisre_approval_refused_total`, whose identity, rights and digest
codes page security (ops/alerts.yml). Without a signing key the writes answer 503: nothing is
approved unsigned.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import Connection, Engine, text

from aisre.api import audit
from aisre.api.auth import Caller, current_caller
from aisre.api.commands import parse_if_match
from aisre.api.intake import check_key
from aisre.api.permissions import check_service, content_read, requires
from aisre.api.problems import ApiError
from aisre.contracts import (
    ActionView,
    ApprovalCreate,
    ApprovalDecisionView,
    ApprovalGrantedData,
    ApprovalRefusedData,
    ApprovalRejectedData,
    ApprovalRevokedData,
    Page,
    RejectionCreate,
    RevocationCreate,
    Strict,
)
from aisre.record import approvals as rec
from aisre.record import idempotency as idem
from aisre.record import load_incident, reads
from aisre.record.incidents import SIGNAL_WORKFLOW, append_event, enqueue, uuid7
from aisre.reliability import telemetry
from aisre.remediation.action import (
    ApprovalError,
    ApprovalPolicy,
    Decision,
    Directory,
    Perm,
    Signer,
    record_decision,
)

SOURCE = "/aisre/api/approvals"
CallerDep = Annotated[Caller, Depends(current_caller)]
KeyDep = Annotated[str | None, Header(alias="Idempotency-Key")]
IfMatchDep = Annotated[str | None, Header(alias="If-Match")]
STATUS = {  # refusal code -> HTTP status (Artifact 7.4)
    "reauth_required": 401,
    "not_found": 404,
    "forbidden": 403,
    "self_approval": 403,
    "not_own_approval": 403,
    "reason_required": 422,
}  # anything else (digest_mismatch, duplicate, action_not_open, execution_started) is 409
EVENT = {
    "approved": "aisre.approval.granted.v1",
    "rejected": "aisre.approval.rejected.v1",
    "revoked": "aisre.approval.revoked.v1",
}


class ApprovalRequestView(Strict):
    """FR-25: everything the approver reviews in one read. The diff is the patch's artifact
    (`GET /v1/artifacts/{id}/content`); evidence is cited by ID, readable through the incident."""

    action: ActionView
    approval_request_id: UUID | None
    approvers_needed: int | None
    requested_at: datetime | None
    escalate_at: datetime | None
    expires_at: datetime | None
    open: bool
    diagnosis_summary: str | None
    diagnosis_evidence_ids: list[UUID]
    diff_artifact_id: UUID | None
    risk: str
    undo_plan: str
    rollback_action_id: UUID | None
    rollback_action_digest: str | None


class Locked(BaseModel):
    incident_id: UUID
    service: str
    status: str
    version: int
    investigation_run: int


def _now() -> datetime:
    return datetime.now(UTC)


def _lock(conn: Connection, action_id: UUID, org_id: UUID) -> Locked | None:
    row = (
        conn.execute(
            text(
                "SELECT a.incident_id, s.slug AS service, a.status, a.version, "
                "i.investigation_run FROM actions a "
                "JOIN incidents i ON i.incident_id = a.incident_id "
                "JOIN services s ON s.service_id = a.target_service_id "
                "WHERE a.action_id = :a AND i.org_id = :o FOR UPDATE OF a"
            ),
            {"a": action_id, "o": org_id},
        )
        .mappings()
        .first()
    )
    return None if row is None else Locked.model_validate(dict(row))


def _view(row: dict[str, Any]) -> ApprovalDecisionView:
    return ApprovalDecisionView(
        approval_id=row["approval_id"],
        action_id=row["action_id"],
        decision=row["decision"],
        decided_by=row["decided_by"],
        decided_by_role=row["decided_by_role"],
        action_digest=str(row["action_digest"]).strip(),
        patch_sha256=str(row["patch_sha256"]).strip() if row["patch_sha256"] else None,
        target_service=row["target_service"],
        target_environment=row["target_environment"],
        decided_at=row["decided_at"],
        valid_until=row["valid_until"],
        reason=row["reason"],
        signing_key_id=row["signing_key_id"],
        revokes_approval_id=row["revokes_approval_id"],
        conditional=row["conditional"],
        conditional_on_approval_id=row["conditional_on_approval_id"],
    )


class Approvals:
    def __init__(
        self,
        db: Engine,
        signer: Signer | None,
        policy: ApprovalPolicy | None = None,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.db, self.signer, self.clock = db, signer, clock
        self.policy = policy or ApprovalPolicy()

    # ------------------------------------------------------------------ reads
    def readable(self, request: Request, conn: Connection, caller: Caller, action_id: UUID) -> Any:
        missing = ApiError(404, "not_found", "Action not found", f"No action {action_id}.")
        incident_id = reads.incident_of_action(conn, action_id, caller.org_id)
        if incident_id is None:
            raise missing
        view = load_incident(conn, incident_id, caller.org_id)
        if view is None or not check_service(
            request, caller, Perm.READ, view.service, incident_id=incident_id
        ):
            raise missing
        return incident_id

    def request_view(
        self, request: Request, caller: Caller, action_id: UUID
    ) -> ApprovalRequestView:
        with self.db.connect() as conn:
            incident_id = self.readable(request, conn, caller, action_id)
            (action,) = reads.actions(conn, incident_id, action_id)
            req = rec.latest_request(conn, action_id)
            diagnosis = reads.current_diagnosis(conn, incident_id)
            rollback = rec.rollback_of(conn, action_id)
        content_read(request, caller, incident_id, "approval_request")
        cited: list[UUID] = []
        if diagnosis is not None:
            found = [e for c in diagnosis.claims for e in c.evidence_ids] + [
                link.evidence_id for h in diagnosis.hypotheses for link in h.evidence
            ]
            cited = list(dict.fromkeys(found))
        return ApprovalRequestView(
            action=action,
            approval_request_id=req.approval_request_id if req else None,
            approvers_needed=req.approvers_needed if req else None,
            requested_at=req.requested_at if req else None,
            escalate_at=req.escalate_at if req else None,
            expires_at=req.expires_at if req else None,
            open=bool(req and req.closed_at is None),
            diagnosis_summary=diagnosis.summary if diagnosis else None,
            diagnosis_evidence_ids=cited,
            diff_artifact_id=action.patch.diff_artifact_id if action.patch else None,
            risk=action.risk,
            undo_plan=action.undo_plan,
            rollback_action_id=rollback[0] if rollback else None,
            rollback_action_digest=rollback[1] if rollback else None,
        )

    def history(
        self, request: Request, caller: Caller, action_id: UUID
    ) -> Page[ApprovalDecisionView]:
        with self.db.connect() as conn:
            incident_id = self.readable(request, conn, caller, action_id)
            rows = rec.decision_rows(conn, action_id)
        content_read(request, caller, incident_id, "approvals")
        return Page[ApprovalDecisionView](items=[_view(r) for r in rows])

    # ----------------------------------------------------------------- writes
    def decide(
        self,
        request: Request,
        caller: Caller,
        action_id: UUID,
        decision: Literal["approved", "rejected", "revoked"],
        body: ApprovalCreate | RejectionCreate | RevocationCreate,
        key: str | None,
        if_match: str | None,
    ) -> JSONResponse:
        key = check_key(key)
        version = parse_if_match(if_match)
        operation = f"{request.method} {request.scope['route'].path}"
        fingerprint = idem.request_fingerprint(
            {"action_id": str(action_id), "if_match": version, "body": body.model_dump(mode="json")}
        )
        if self.signer is None:
            raise ApiError(
                503, "approval_signing_unavailable", "Approvals unavailable", "No signing key."
            )
        refused: tuple[UUID, ApprovalError] | None = None
        with self.db.begin() as conn:
            locked = _lock(conn, action_id, caller.org_id)
            if locked is None or not check_service(
                request, caller, Perm.READ, locked.service, incident_id=None
            ):
                raise ApiError(404, "not_found", "Action not found", f"No action {action_id}.")
            try:
                stored = idem.claim(conn, caller.caller_id, operation, key, fingerprint)
            except idem.KeyReused:
                raise ApiError(
                    409, "idempotency_key_reused", "Idempotency key already used"
                ) from None
            except idem.KeyInProgress:
                raise ApiError(
                    409, "idempotency_key_in_progress", "Request still in progress"
                ) from None
            if stored is None:
                if locked.version != version:
                    raise ApiError(
                        412,
                        "precondition_failed",
                        "Version mismatch",
                        f"The action is at version {locked.version}; reload it and retry.",
                        headers={"ETag": f'"{locked.version}"'},
                    )
                try:
                    stored = self._decide(conn, caller, action_id, locked, decision, body)
                except ApprovalError as e:
                    refused = (locked.incident_id, e)
                    conn.rollback()
                else:
                    idem.complete(
                        conn,
                        caller.caller_id,
                        operation,
                        key,
                        status=stored.status,
                        body=stored.body,
                        resource_id=action_id,
                    )
        if refused is not None:
            self._refused(request, caller, action_id, decision, *refused)
        assert stored is not None
        return JSONResponse(stored.body, status_code=stored.status)

    def _decide(
        self,
        conn: Connection,
        caller: Caller,
        action_id: UUID,
        locked: Locked,
        decision: Literal["approved", "rejected", "revoked"],
        body: ApprovalCreate | RejectionCreate | RevocationCreate,
    ) -> idem.StoredResponse:
        assert self.signer is not None
        now = self.clock()
        action = rec.load_action(conn, action_id)
        assert action is not None
        service = action.target.service
        directory = Directory().add_groups(caller.caller_id, caller.groups, service)
        prior = rec.decisions(conn, action_id)
        req = rec.latest_request(conn, action_id)
        intent = bool(
            conn.execute(
                text("SELECT 1 FROM execution_attempts WHERE action_id = :a LIMIT 1"),
                {"a": action_id},
            ).first()
        )
        rollback_found = rec.rollback_of(conn, action_id)
        rollback = rec.load_action(conn, rollback_found[0]) if rollback_found else None
        rollback_prior = rec.decisions(conn, rollback_found[0]) if rollback_found else []
        rollback_digest = getattr(body, "rollback_action_digest", None)
        events: list[dict[str, Any]] = []
        rows = record_decision(
            action=action,
            subject=caller.caller_id,
            decision=decision,
            auth_time=caller.auth_time or datetime.fromtimestamp(0, UTC),
            mfa=caller.mfa,
            now=now,
            directory=directory,
            policy=self.policy,
            signer=self.signer,
            approval_id=str(uuid7()),
            presented_digest=body.action_digest,
            proposer_subjects={rec.REQUESTER},
            action_status=locked.status,
            request_open=bool(req and req.closed_at is None),
            intent_recorded=intent,
            events=events,
            impact=rec.impact(conn, action_id),
            reason=getattr(body, "reason", None),
            revokes=str(body.approval_id) if isinstance(body, RevocationCreate) else None,
            prior=prior,
            rollback=rollback,
            rollback_digest=rollback_digest,
            rollback_approval_id=str(uuid7()),
            rollback_prior=rollback_prior,
        )
        if req is None:
            raise ApprovalError("action_not_open", "no approval request")
        for d in rows:
            request_id = req.approval_request_id
            if d.action_id != str(action_id):
                rb = rec.latest_request(conn, UUID(d.action_id))
                if rb is None:
                    raise ApprovalError("action_not_open", "the rollback has no request")
                request_id = rb.approval_request_id
            rec.insert_decision(conn, d, request_id)
            self._event(conn, locked.incident_id, d, now, body)
            telemetry.approval_decisions.labels(decision=d.decision).inc()
        main = rows[0]
        if decision == "rejected":
            rec.close_request(conn, req.approval_request_id, "decided", now)
        enqueue(
            conn,
            locked.incident_id,
            SIGNAL_WORKFLOW,
            {
                "signal": "approval_decided",
                "arg": {
                    "decision_id": main.approval_id,
                    "action_id": main.action_id,
                    "decision": main.decision,
                    "action_digest": main.action_digest,
                    "decided_by": main.subject,
                },
            },
            run=locked.investigation_run,
        )
        (row,) = [
            r
            for r in rec.decision_rows(conn, action_id)
            if str(r["approval_id"]) == main.approval_id
        ]
        return idem.StoredResponse(201, _view(row).model_dump(mode="json"))

    def _event(
        self,
        conn: Connection,
        incident_id: UUID,
        d: Decision,
        now: datetime,
        body: BaseModel,
    ) -> None:
        patch = rec.load_action(conn, UUID(d.action_id))
        sha = patch.patch_sha256 if patch else None
        data: Strict
        if d.decision == "approved":
            assert d.valid_until is not None
            data = ApprovalGrantedData(
                approval_id=UUID(d.approval_id),
                action_id=UUID(d.action_id),
                action_digest=d.action_digest,
                patch_sha256=sha,
                decided_by=d.subject,
                valid_until=datetime.fromisoformat(d.valid_until),
                conditional=d.conditional,
            )
        elif d.decision == "rejected":
            data = ApprovalRejectedData(
                approval_id=UUID(d.approval_id),
                action_id=UUID(d.action_id),
                action_digest=d.action_digest,
                decided_by=d.subject,
                reason=d.reason or "",
                request_more_evidence=bool(getattr(body, "request_more_evidence", False)),
            )
        else:
            assert d.revokes is not None
            data = ApprovalRevokedData(
                approval_id=UUID(d.approval_id),
                revokes_approval_id=UUID(d.revokes),
                action_id=UUID(d.action_id),
                action_digest=d.action_digest,
                decided_by=d.subject,
                reason=d.reason or "",
            )
        append_event(
            conn,
            incident_id,
            EVENT[d.decision],
            d.subject,
            data,
            occurred_at=now,
            source=SOURCE,
            causation_id=UUID(d.approval_id),
        )

    def _refused(
        self,
        request: Request,
        caller: Caller,
        action_id: UUID,
        decision: Literal["approved", "rejected", "revoked"],
        incident_id: UUID,
        e: ApprovalError,
    ) -> None:
        """The refusal is recorded on its own: the decision's transaction was rolled back."""
        alert = e.code in _security_codes()
        with self.db.begin() as conn:
            append_event(
                conn,
                incident_id,
                "aisre.approval.refused.v1",
                caller.caller_id,
                ApprovalRefusedData(
                    action_id=action_id, decision=decision, code=e.code, security_alert=alert
                ),
                occurred_at=self.clock(),
                source=SOURCE,
            )
        telemetry.approval_refused.labels(reason=e.code).inc()
        audit.emit(
            "aisre.approval.refused",
            request,
            caller=caller.caller_id,
            action_id=str(action_id),
            decision=decision,
            code=e.code,
            security_alert=alert,
        )
        raise ApiError(
            STATUS.get(e.code, 409), e.code, "Decision refused", str(e).split(": ", 1)[-1] or None
        )


def _security_codes() -> set[str]:
    from aisre.remediation.action import SECURITY_ALERT_CODES

    return set(SECURITY_ALERT_CODES)


def register(app: FastAPI, approvals: Approvals) -> None:
    @app.get(
        "/v1/actions/{action_id}/approval-request",
        response_model=ApprovalRequestView,
        **requires(Perm.READ),
    )
    def read_approval_request(
        request: Request, action_id: UUID, caller: CallerDep
    ) -> ApprovalRequestView:
        return approvals.request_view(request, caller, action_id)

    @app.get(
        "/v1/actions/{action_id}/approvals",
        response_model=Page[ApprovalDecisionView],
        **requires(Perm.READ),
    )
    def read_approvals(
        request: Request, action_id: UUID, caller: CallerDep
    ) -> Page[ApprovalDecisionView]:
        return approvals.history(request, caller, action_id)

    @app.post("/v1/actions/{action_id}/approvals", status_code=201, **requires(Perm.APPROVE))
    def approve(
        request: Request,
        action_id: UUID,
        body: ApprovalCreate,
        caller: CallerDep,
        idempotency_key: KeyDep = None,
        if_match: IfMatchDep = None,
    ) -> JSONResponse:
        return approvals.decide(
            request, caller, action_id, "approved", body, idempotency_key, if_match
        )

    @app.post("/v1/actions/{action_id}/rejections", status_code=201, **requires(Perm.REJECT))
    def reject(
        request: Request,
        action_id: UUID,
        body: RejectionCreate,
        caller: CallerDep,
        idempotency_key: KeyDep = None,
        if_match: IfMatchDep = None,
    ) -> JSONResponse:
        return approvals.decide(
            request, caller, action_id, "rejected", body, idempotency_key, if_match
        )

    @app.post("/v1/actions/{action_id}/revocations", status_code=201, **requires(Perm.REVOKE_OWN))
    def revoke(
        request: Request,
        action_id: UUID,
        body: RevocationCreate,
        caller: CallerDep,
        idempotency_key: KeyDep = None,
        if_match: IfMatchDep = None,
    ) -> JSONResponse:
        return approvals.decide(
            request, caller, action_id, "revoked", body, idempotency_key, if_match
        )
