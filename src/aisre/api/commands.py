"""Artifact 7.4, 7.8, ADR-64 (T1.6): the incident's write routes, after intake.

    POST /v1/incidents/{id}/notes                  incident:note             201
    POST /v1/incidents/{id}/cancellation           incident:cancel           202
    POST /v1/incidents/{id}/severity-confirmation  incident:confirm_severity 200  If-Match
    POST /v1/incidents/{id}/closure                incident:close            200  If-Match
    POST /v1/incidents/{id}/reinvestigations       incident:reinvestigate    202  If-Match
    POST /v1/incidents/{id}/incident-review        incident:close            201  (ADR-75)

Every one, in order: the `Idempotency-Key` (400 without one); one transaction that claims
the key, locks the incident, checks the caller on the incident's service, checks `If-Match`
where the route takes it, makes the change (aisre.record.commands) and stores the response.

- A caller who can't read the incident gets the same 404 as for a missing one (ADR-58); one
  who can read it but lacks the route's permission on its service gets 403. Both are logged
  as `aisre.authz.denied`.
- `If-Match` missing is 428, stale is 412 (ADR-64). Cancellation and notes take none.
- A state the change can't apply in is 409. The same key and body replays the stored answer;
  the same key with another body or incident is 409 `idempotency_key_reused` (Artifact 7.8).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import Connection, Engine

from aisre.api.auth import Caller
from aisre.api.intake import check_key
from aisre.api.permissions import check_service
from aisre.api.problems import ApiError
from aisre.record import commands as cmd
from aisre.record import idempotency as idem
from aisre.remediation.action import Perm

Command = Callable[[Connection, cmd.Locked], cmd.Done]


def parse_if_match(value: str | None) -> int:
    """The version in `If-Match`: `"3"`, `W/"3"` or `3`. Missing is 428, unreadable 412."""
    if value is None:
        raise ApiError(
            428,
            "precondition_required",
            "If-Match required",
            "Send the incident's version (its ETag) in If-Match.",
        )
    text = value.strip().removeprefix("W/").strip('"')
    if not text.isdigit():
        raise ApiError(412, "precondition_failed", "Version mismatch", "If-Match is not a version.")
    return int(text)


def not_found(incident_id: UUID) -> ApiError:
    return ApiError(404, "not_found", "Incident not found", f"No incident {incident_id}.")


class Commands:
    def __init__(self, db: Engine) -> None:
        self.db = db

    def run(
        self,
        request: Request,
        caller: Caller,
        incident_id: UUID,
        perm: Perm,
        key: str | None,
        body: BaseModel | None,
        command: Command,
        *,
        status: int,
        if_match: int | None = None,
    ) -> JSONResponse:
        key = check_key(key)
        operation = f"{request.method} {request.scope['route'].path}"
        fingerprint = idem.request_fingerprint(
            {
                "incident_id": str(incident_id),
                "if_match": if_match,
                "body": body.model_dump(mode="json") if body is not None else None,
            }
        )
        with self.db.begin() as conn:
            inc = cmd.lock(conn, incident_id, caller.org_id)
            if inc is None or not check_service(
                request, caller, Perm.READ, inc.service, incident_id=incident_id
            ):
                raise not_found(incident_id)
            if not check_service(request, caller, perm, inc.service, incident_id=incident_id):
                raise ApiError(
                    403, "forbidden", "Forbidden", f"Your roles don't grant {perm.value} here."
                )
            stored = self._claim(conn, caller, operation, key, fingerprint)
            if stored is None:
                try:
                    done = command(conn, inc)
                except cmd.StaleVersion as e:
                    raise ApiError(
                        412,
                        "precondition_failed",
                        "Version mismatch",
                        f"The incident is at version {e.current}; reload it and retry.",
                        headers={"ETag": f'"{e.current}"'},
                    ) from None
                except cmd.Conflict as e:
                    raise ApiError(409, e.code, "Conflict", e.detail) from None
                stored = idem.StoredResponse(status, _body(done))
                idem.complete(
                    conn,
                    caller.caller_id,
                    operation,
                    key,
                    status=stored.status,
                    body=stored.body,
                    resource_id=incident_id,
                )
        return JSONResponse(
            stored.body,
            status_code=stored.status,
            headers={"ETag": f'"{stored.body["version"]}"'},
        )

    @staticmethod
    def _claim(
        conn: Connection, caller: Caller, operation: str, key: str, fingerprint: str
    ) -> idem.StoredResponse | None:
        try:
            return idem.claim(conn, caller.caller_id, operation, key, fingerprint)
        except idem.KeyReused:
            raise ApiError(
                409,
                "idempotency_key_reused",
                "Idempotency key already used",
                "This Idempotency-Key was sent with a different request.",
            ) from None
        except idem.KeyInProgress:
            raise ApiError(
                409,
                "idempotency_key_in_progress",
                "Request still in progress",
                "Retry after a moment.",
                headers={"Retry-After": "1"},
            ) from None


def _body(done: cmd.Done) -> dict[str, Any]:
    return {
        "incident_id": str(done.incident_id),
        "version": done.version,
        "sequence": done.sequence,
        **done.extra,
    }
