"""Artifact 7.4, ADR-50 (T1.4): the permission matrix, enforced at every route.

Each `/v1` route is declared with `**requires(perm)`. That does two things:

- A dependency refuses, before the route runs, a caller none of whose roles grants `perm` on
  any service: 403 `forbidden`, and an `aisre.authz.denied` line (ST-20). Monitoring reading
  a timeline stops here.
- The OpenAPI document names the permission (`x-aisre-permission`), and a test checks every
  `/v1` route has one, so a new route can't ship without it.

Roles are per service (aisre.api.auth), so a caller who passes the first check may still lack
the permission on the service of the incident in hand. The route then calls `check_service`
once it knows the service: for an incident, the answer is the same 404 as for one that
doesn't exist, so existence isn't revealed (ADR-58, ST-22), and the denial is logged with
the incident's ID. Successful reads of incident content are logged with `content_read`.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Request

from aisre.api import audit
from aisre.api.auth import Caller, current_caller
from aisre.api.problems import ApiError
from aisre.remediation.action import Perm

PERMISSION_KEY = "x-aisre-permission"


def denied(
    request: Request,
    caller: Caller,
    perm: Perm,
    *,
    service: str | None = None,
    incident_id: UUID | None = None,
) -> None:
    audit.emit(
        "aisre.authz.denied",
        request,
        caller=caller.caller_id,
        org_id=str(caller.org_id),
        permission=perm.value,
        service=service,
        incident_id=str(incident_id) if incident_id else None,
    )


class Needs:
    """The dependency `requires` installs: some role, on some service, grants `perm`."""

    def __init__(self, perm: Perm) -> None:
        self.perm = perm

    def __call__(
        self, request: Request, caller: Annotated[Caller, Depends(current_caller)]
    ) -> None:
        if not caller.can_somewhere(self.perm):
            denied(request, caller, self.perm)
            raise ApiError(
                403, "forbidden", "Forbidden", f"Your roles don't grant {self.perm.value}."
            )


def requires(perm: Perm) -> dict[str, Any]:
    """Keyword arguments for a route decorator: the check, and the OpenAPI annotation."""
    return {"dependencies": [Depends(Needs(perm))], "openapi_extra": {PERMISSION_KEY: perm.value}}


def check_service(
    request: Request,
    caller: Caller,
    perm: Perm,
    service: str,
    *,
    incident_id: UUID | None = None,
) -> bool:
    """True if the caller holds `perm` on `service`; otherwise logs the denial, False."""
    if caller.can(perm, service):
        return True
    denied(request, caller, perm, service=service, incident_id=incident_id)
    return False


def content_read(request: Request, caller: Caller, incident_id: UUID, content: str) -> None:
    """Log a read of an incident's content (ST-22: every content read logged)."""
    audit.emit(
        "aisre.content.read",
        request,
        caller=caller.caller_id,
        org_id=str(caller.org_id),
        incident_id=str(incident_id),
        content=content,
    )
