"""DEVELOPMENT ONLY: who is calling, until real sign-in exists.

Artifact 7.4 puts an OIDC bearer token on every `/v1` call. M0 has no identity system yet
(its owner is still to be named: "Needs the team"), so the caller names itself in a header:

    X-Aisre-Dev-Caller: <name>      required; recorded as `dev:<name>`
    X-Aisre-Dev-Org: <uuid>         optional; defaults to DEV_ORG_ID

Anyone can claim to be anyone, so this must never face a real network. Idempotency keys are
per caller name, across organisations. `create_app` calls
`require_development` and refuses to build the app unless `AISRE_ENV` is `development` or
`test`; Compose publishes the API on 127.0.0.1 only. M1 replaces this module with token
verification (T1.2), and permissions (`incident:create`, `incident:read`) arrive with it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Header

from aisre.api.problems import ApiError

DEV_CALLER_HEADER = "X-Aisre-Dev-Caller"
DEV_ORG_HEADER = "X-Aisre-Dev-Org"
DEV_ORG_ID = UUID("00000000-0000-7000-8000-000000000001")
ALLOWED_ENVIRONMENTS = frozenset({"development", "test"})
CALLER_NAME = re.compile(r"^[a-z0-9][a-z0-9._@-]{0,62}$")


@dataclass(frozen=True)
class Caller:
    caller_id: str
    org_id: UUID


def require_development(env: str) -> None:
    """Refuse to serve with the development caller header outside development and test."""
    if env not in ALLOWED_ENVIRONMENTS:
        raise RuntimeError(
            f"AISRE_ENV is {env!r}: the M0 API authenticates with a development-only header "
            "and runs only when AISRE_ENV is 'development' or 'test'."
        )


def dev_caller(
    caller: Annotated[str | None, Header(alias=DEV_CALLER_HEADER)] = None,
    org: Annotated[str | None, Header(alias=DEV_ORG_HEADER)] = None,
) -> Caller:
    if caller is None or not CALLER_NAME.fullmatch(caller):
        raise ApiError(
            401,
            "unauthenticated",
            "Caller not identified",
            f"Send {DEV_CALLER_HEADER} with a lower-case name (development only).",
        )
    try:
        org_id = UUID(org) if org else DEV_ORG_ID
    except ValueError:
        raise ApiError(
            401, "unauthenticated", "Caller not identified", f"{DEV_ORG_HEADER} is not a UUID."
        ) from None
    return Caller(f"dev:{caller}", org_id)
