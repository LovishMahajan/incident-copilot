"""Artifact 7.4, ADR-49, ADR-50 (T1.3): who is calling, and which roles they hold where.

Two ways in, never both:

**Bearer tokens** (`Settings.token_sign_in`: AISRE_OIDC_ISSUER, _AUDIENCE and _JWKS_URL set).
Every `/v1` call sends `Authorization: Bearer <token>`, verified by aisre.api.tokens against
the issuer's JWK set. The caller is `user:<sub>` in the token's `org_id`. A missing or refused
token is 401 with no data, logged as `aisre.auth.failed` (ST-19). The development header below
is then refused: sending it without a token is a failed sign-in like any other.

**DEVELOPMENT ONLY: the caller header**, when no verifier is configured. Anyone can claim to be
anyone, so `check_sign_in` refuses to build the app this way unless `AISRE_ENV` is development
or test, and Compose publishes the API on 127.0.0.1 only.

    X-Aisre-Dev-Caller: <name>      required; recorded as `dev:<name>`
    X-Aisre-Dev-Org: <uuid>         optional; defaults to DEV_ORG_ID
    X-Aisre-Dev-Groups: <g>,<g>     optional; defaults to "aisre:responder"

**Roles come from groups** (ADR-49), the token's `groups` claim or the header. A group names a
role, and optionally the one service it applies to; without a service it applies to every
service in the caller's organisation:

    aisre:<role>              e.g. aisre:incident_lead, aisre:monitoring
    aisre:<role>:<service>    e.g. aisre:approver:orders-api

`<role>` is one of ADR-50's: viewer, responder, approver, incident_lead, monitoring,
platform_admin, plus security_lead (Artifact 11.7). Other groups are ignored; the platform's
own identities (system:workflow, system:dispatcher) never come from a group. What each role
may do is the matrix in aisre.remediation.action (MATRIX), checked by `Caller.can`.

Sign-in time and the two-factor claim are carried on the Caller for the approval routes,
which require a sign-in within 15 minutes, with two-factor (ADR-49, M3).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import Header, Request

from aisre.api import audit
from aisre.api.problems import ApiError
from aisre.api.tokens import InvalidToken, TokenVerifier, VerifierUnavailable
from aisre.config import Settings
from aisre.remediation.action import MATRIX, Perm, Role

DEV_CALLER_HEADER = "X-Aisre-Dev-Caller"
DEV_ORG_HEADER = "X-Aisre-Dev-Org"
DEV_GROUPS_HEADER = "X-Aisre-Dev-Groups"
DEV_ORG_ID = UUID("00000000-0000-7000-8000-000000000001")
DEV_DEFAULT_GROUPS = "aisre:responder"
ALLOWED_ENVIRONMENTS = frozenset({"development", "test"})
CALLER_NAME = re.compile(r"^[a-z0-9][a-z0-9._@-]{0,62}$")
GROUP = re.compile(r"^aisre:(?P<role>[a-z_]+)(?::(?P<service>[a-z0-9][a-z0-9-]{0,62}))?$")
GROUP_ROLES = frozenset(
    {
        Role.VIEWER,
        Role.RESPONDER,
        Role.APPROVER,
        Role.INCIDENT_LEAD,
        Role.MONITORING,
        Role.PLATFORM_ADMIN,
        Role.SECURITY_LEAD,
    }
)
EVERY_SERVICE = "*"

Grants = Mapping[str, frozenset[Role]]  # service slug, or EVERY_SERVICE -> roles


def roles_from_groups(groups: Iterable[str]) -> dict[str, frozenset[Role]]:
    """The roles a caller's groups give, per service. Unknown groups give nothing."""
    grants: dict[str, set[Role]] = {}
    for g in groups:
        m = GROUP.fullmatch(g)
        if m is None or m["role"] not in {r.value for r in GROUP_ROLES}:
            continue
        grants.setdefault(m["service"] or EVERY_SERVICE, set()).add(Role(m["role"]))
    return {svc: frozenset(roles) for svc, roles in grants.items()}


@dataclass(frozen=True)
class Caller:
    caller_id: str
    org_id: UUID
    grants: Grants = field(default_factory=dict)
    auth_time: datetime | None = None
    mfa: bool = False
    groups: frozenset[str] = frozenset()  # identity groups as sent, for document reads (10.9)

    def roles_on(self, service: str) -> frozenset[Role]:
        return self.grants.get(EVERY_SERVICE, frozenset()) | self.grants.get(service, frozenset())

    def can(self, perm: Perm, service: str) -> bool:
        """ADR-50: some role the caller holds on `service` grants `perm`."""
        return any(perm in MATRIX[r] for r in self.roles_on(service))

    def can_somewhere(self, perm: Perm) -> bool:
        """Some role the caller holds, on any service, grants `perm`."""
        return any(perm in MATRIX[r] for roles in self.grants.values() for r in roles)

    @property
    def is_monitoring(self) -> bool:
        """A monitoring system's identity, not a person's (ADR-58's 422 path)."""
        return any(Role.MONITORING in roles for roles in self.grants.values())


def check_sign_in(settings: Settings) -> None:
    """Refuse to serve with the development caller header outside development and test."""
    if not settings.token_sign_in and settings.env not in ALLOWED_ENVIRONMENTS:
        raise RuntimeError(
            f"AISRE_ENV is {settings.env!r} and no token verifier is configured: the "
            "development-only caller header runs only when AISRE_ENV is 'development' or "
            "'test'. Set AISRE_OIDC_ISSUER, AISRE_OIDC_AUDIENCE and AISRE_OIDC_JWKS_URL."
        )


def require_development(env: str) -> None:
    """For development-only tools (aisre.api.dev_seed): refuse outside development and test."""
    if env not in ALLOWED_ENVIRONMENTS:
        raise RuntimeError(f"AISRE_ENV is {env!r}: this is development-only.")


def _refuse(request: Request, reason: str, detail: str) -> ApiError:
    audit.emit("aisre.auth.failed", request, reason=reason)
    return ApiError(
        401,
        "unauthenticated",
        "Not signed in",
        detail,
        headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
    )


class Authenticator:
    """Names the caller of every `/v1` route; `current_caller` is its FastAPI dependency."""

    def __init__(self, settings: Settings, verifier: TokenVerifier | None = None) -> None:
        check_sign_in(settings)
        if verifier is None and settings.token_sign_in:
            assert settings.oidc_issuer and settings.oidc_audience and settings.oidc_jwks_url
            verifier = TokenVerifier(
                settings.oidc_issuer, settings.oidc_audience, settings.oidc_jwks_url
            )
        self.verifier = verifier

    def caller(
        self,
        request: Request,
        authorization: str | None,
        dev_caller: str | None,
        dev_org: str | None,
        dev_groups: str | None,
    ) -> Caller:
        if self.verifier is not None:
            return self._bearer(request, authorization, dev_header=dev_caller is not None)
        return self._dev(request, dev_caller, dev_org, dev_groups)

    def _bearer(self, request: Request, authorization: str | None, *, dev_header: bool) -> Caller:
        assert self.verifier is not None
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            reason = "dev_header_refused" if dev_header and not authorization else "missing_token"
            raise _refuse(request, reason, "Send Authorization: Bearer <token>.")
        try:
            claims = self.verifier.verify(token.strip())
        except InvalidToken as e:
            raise _refuse(request, e.reason, "The token was refused.") from None
        except VerifierUnavailable:
            audit.emit("aisre.auth.failed", request, reason="verifier_unavailable")
            raise ApiError(
                503, "sign_in_unavailable", "Sign-in unavailable", "Try again shortly."
            ) from None
        return Caller(
            f"user:{claims.subject}",
            claims.org_id,
            roles_from_groups(claims.groups),
            claims.auth_time,
            claims.mfa,
            frozenset(claims.groups),
        )

    def _dev(
        self, request: Request, caller: str | None, org: str | None, groups: str | None
    ) -> Caller:
        if caller is None or not CALLER_NAME.fullmatch(caller):
            raise _refuse(
                request,
                "missing_dev_caller",
                f"Send {DEV_CALLER_HEADER} with a lower-case name (development only).",
            )
        try:
            org_id = UUID(org) if org else DEV_ORG_ID
        except ValueError:
            raise _refuse(request, "bad_dev_org", f"{DEV_ORG_HEADER} is not a UUID.") from None
        names = [
            n.strip() for n in (groups if groups is not None else DEV_DEFAULT_GROUPS).split(",")
        ]
        return Caller(
            f"dev:{caller}",
            org_id,
            roles_from_groups(names),
            groups=frozenset(n for n in names if n),
        )


def current_caller(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    dev_caller: Annotated[str | None, Header(alias=DEV_CALLER_HEADER)] = None,
    dev_org: Annotated[str | None, Header(alias=DEV_ORG_HEADER)] = None,
    dev_groups: Annotated[str | None, Header(alias=DEV_GROUPS_HEADER)] = None,
) -> Caller:
    """The FastAPI dependency: the app's Authenticator (`app.state.authenticator`)."""
    authenticator: Authenticator = request.app.state.authenticator
    return authenticator.caller(request, authorization, dev_caller, dev_org, dev_groups)
