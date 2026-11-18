"""Artifact 7.4, 7.7, 7.8, ADR-58 (T1.2): full intake, `POST /v1/incidents`.

In order, after sign-in and the `incident:create` check (aisre.api.permissions):

1. `Idempotency-Key`, 8 to 128 characters, else 400.
2. The body: a JSON object mapping to `IncidentCreate` (aisre.api.alerts). Anything else is
   422 `validation_failed`, naming each missing or bad field (FR-05, FR-41).
3. The caller's create permission on the alert's service (roles are per service).
4. One transaction: claim the key, then open the incident or attach the alert to an open one
   (deduplication and grouping: aisre.record.incidents, FR-03, FR-42, FR-43). The same key
   and body again replays the stored response; the same key with another body is
   409 `idempotency_key_reused` (FR-04, Artifact 7.8).

A refusal at 2 or 3, or a service or environment not in the organisation's catalogue, is a
rejected alert (ADR-58): stored raw in `dead_letters` with the reason, in its own committed
transaction (if that write fails the answer is 503, so the sender retries rather than the
alert being lost), with an `aisre.intake.dead_lettered` line for the incident lead, and no
incident. It doesn't use up the key, so a corrected alert can be sent under it. The answer
depends on who sent it, never on whether the service exists:

    monitoring caller   422 unknown_service, for an unknown and a forbidden service alike
    person              404 unknown_service, for both, so existence isn't revealed
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from aisre.api import audit
from aisre.api.alerts import to_incident_create
from aisre.api.auth import Caller
from aisre.api.permissions import check_service
from aisre.api.problems import ApiError, field_errors
from aisre.contracts import IncidentAccepted, IncidentCreate
from aisre.record import UnknownService, create_incident
from aisre.record import idempotency as idem
from aisre.record.dead_letters import add_dead_letter, as_stored
from aisre.remediation.action import Perm

CREATE_INCIDENT = "POST /v1/incidents"
SLUG_MAX = 200  # an unverified service or environment name, as stored on a dead letter


def check_key(key: str | None) -> str:
    if key is None:
        raise ApiError(
            400, "idempotency_key_required", "Idempotency-Key required", "Every write needs one."
        )
    if not 8 <= len(key) <= 128:
        raise ApiError(
            400, "idempotency_key_invalid", "Invalid Idempotency-Key", "Use 8 to 128 characters."
        )
    return key


class Intake:
    def __init__(self, db: Engine) -> None:
        self.db = db

    def submit(
        self, request: Request, caller: Caller, raw: bytes, idempotency_key: str | None
    ) -> JSONResponse:
        key = check_key(idempotency_key)
        try:
            parsed: Any = json.loads(raw)
        except ValueError:
            parsed = None
        stored_payload = as_stored(raw, parsed)
        try:
            if not isinstance(parsed, dict):
                raise ValueError("not an object")
            body = to_incident_create(parsed)
        except (ValidationError, ValueError) as e:
            errors = (
                field_errors(e.errors())
                if isinstance(e, ValidationError)
                else [{"loc": ["body"], "msg": "Expected a JSON object", "type": "json"}]
            )
            self._dead_letter(
                request,
                caller,
                "validation_failed",
                stored_payload,
                detail="; ".join(f"{'.'.join(x['loc'])}: {x['msg']}" for x in errors),
                service=parsed.get("service") if isinstance(parsed, dict) else None,
            )
            raise ApiError(422, "validation_failed", "Invalid incident", errors=errors) from None

        if not check_service(request, caller, Perm.CREATE, body.service):
            self._dead_letter(request, caller, "forbidden_service", stored_payload, body=body)
            raise self._unknown_service(caller, body)
        try:
            stored = self._open_or_attach(caller, key, body, stored_payload)
        except UnknownService:
            self._dead_letter(request, caller, "unknown_service", stored_payload, body=body)
            raise self._unknown_service(caller, body) from None
        headers = {"Location": stored.body["links"]["self"]} if stored.status == 201 else None
        return JSONResponse(stored.body, status_code=stored.status, headers=headers)

    def _open_or_attach(
        self, caller: Caller, key: str, body: IncidentCreate, payload: dict[str, Any]
    ) -> idem.StoredResponse:
        fingerprint = idem.request_fingerprint(body.model_dump(mode="json"))
        with self.db.begin() as conn:
            try:
                stored = idem.claim(conn, caller.caller_id, CREATE_INCIDENT, key, fingerprint)
            except idem.KeyReused:
                raise ApiError(
                    409,
                    "idempotency_key_reused",
                    "Idempotency key already used",
                    "This Idempotency-Key was sent with a different request body.",
                ) from None
            except idem.KeyInProgress:
                raise ApiError(
                    409,
                    "idempotency_key_in_progress",
                    "Request still in progress",
                    "Retry after a moment.",
                    headers={"Retry-After": "1"},
                ) from None
            if stored is not None:
                return stored
            made = create_incident(
                conn,
                org_id=caller.org_id,
                caller_id=caller.caller_id,
                body=body,
                raw_payload=payload,
            )
            path = f"/v1/incidents/{made.incident_id}"
            accepted = IncidentAccepted(
                incident_id=made.incident_id,
                display_ref=made.display_ref,
                status=made.status,
                deduplicated=made.deduplicated,
                links={
                    "self": path,
                    "timeline": f"{path}/timeline",
                    "stream": f"{path}/timeline/stream",
                },
            )
            stored = idem.StoredResponse(
                200 if made.deduplicated else 201, accepted.model_dump(mode="json")
            )
            idem.complete(
                conn,
                caller.caller_id,
                CREATE_INCIDENT,
                key,
                status=stored.status,
                body=stored.body,
                resource_id=made.incident_id,
            )
            return stored

    def _dead_letter(
        self,
        request: Request,
        caller: Caller,
        reason: str,
        payload: dict[str, Any],
        *,
        body: IncidentCreate | None = None,
        detail: str | None = None,
        service: Any = None,
    ) -> None:
        service = body.service if body else service
        environment = body.environment if body else None
        if body is not None:
            detail = f"service {body.service!r}, environment {body.environment!r}"
        try:
            with self.db.begin() as conn:
                dead_letter_id = add_dead_letter(
                    conn,
                    org_id=caller.org_id,
                    caller_id=caller.caller_id,
                    reason=reason,
                    raw_payload=payload,
                    detail=detail,
                    service_slug=str(service)[:SLUG_MAX] if service is not None else None,
                    environment_slug=environment,
                )
        except SQLAlchemyError:
            # Not stored, so not refused either: the sender must retry (ADR-58: no alert is
            # ever dropped).
            raise ApiError(
                503, "intake_unavailable", "Intake unavailable", "Retry the alert shortly."
            ) from None
        audit.emit(
            "aisre.intake.dead_lettered",
            request,
            caller=caller.caller_id,
            org_id=str(caller.org_id),
            dead_letter_id=str(dead_letter_id),
            reason=reason,
            notify="incident_lead",
        )

    @staticmethod
    def _unknown_service(caller: Caller, body: IncidentCreate) -> ApiError:
        detail = f"No service {body.service!r} in environment {body.environment!r}."
        status = 422 if caller.is_monitoring else 404
        return ApiError(status, "unknown_service", "Unknown service", detail)
