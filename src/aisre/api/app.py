"""Artifact 7.4: the M0 routes. Thin intake, read and timeline, plus the two probes.

    POST /v1/incidents                         IncidentCreate + Idempotency-Key -> 201 / 200
    GET  /v1/incidents/{id}                    IncidentView, ETag = version
    GET  /v1/incidents/{id}/timeline?after_seq Page[CloudEvent]
    GET  /livez, /readyz

Intake is one transaction (Artifact 7.7): the idempotency key, then the incident, its first
event, the alert and the outbox row that starts the workflow; or, for a duplicate alert, the
alert attached to the open incident (200, `deduplicated: true`). The relay in the worker
delivers the outbox row; this process never talks to Temporal (ADR-19).

Every `/v1` route names its caller through aisre.api.auth: a verified bearer token, or the
development-only header when no token verifier is configured (T1.3).

Routes are plain `def`: the Incident Record is synchronous, so FastAPI runs them on its thread
pool (see aisre.record). Errors are RFC 9457 problem details (aisre.api.problems).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any
from uuid import UUID

from fastapi import Body, Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import Engine, text
from starlette.exceptions import HTTPException

from aisre.api import audit
from aisre.api.alerts import to_incident_create
from aisre.api.auth import Authenticator, Caller, current_caller
from aisre.api.problems import ApiError, field_errors, problem_response
from aisre.config import Settings
from aisre.contracts import CloudEvent, IncidentAccepted, IncidentView, Page
from aisre.record import (
    UnknownService,
    create_incident,
    load_incident,
    make_engine,
    timeline,
    uuid7,
)
from aisre.record import idempotency as idem

CREATE_INCIDENT = "POST /v1/incidents"
REQUEST_ID_HEADER = "X-Request-ID"
TIMELINE_PAGE_MAX = 500

CallerDep = Annotated[Caller, Depends(current_caller)]


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    """The API. Without a token verifier, refuses to build outside development and test
    (aisre.api.auth)."""
    settings = settings or Settings.from_env()
    authenticator = Authenticator(settings)
    audit.install_handler()
    db = engine or make_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if engine is None:
            db.dispose()

    app = FastAPI(title="AI SRE platform API", version="0.1.0", lifespan=lifespan)
    app.state.authenticator = authenticator
    _install_errors(app)

    @app.post("/v1/incidents", status_code=201, response_model=IncidentAccepted)
    def submit_incident(
        caller: CallerDep,
        payload: Annotated[dict[str, Any], Body()],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> JSONResponse:
        key = _check_key(idempotency_key)
        try:
            body = to_incident_create(payload)
        except ValidationError as e:
            raise ApiError(
                422, "validation_failed", "Invalid incident", errors=field_errors(e.errors())
            ) from None
        fingerprint = idem.request_fingerprint(body.model_dump(mode="json"))
        with db.begin() as conn:
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
            if stored is None:
                try:
                    made = create_incident(
                        conn,
                        org_id=caller.org_id,
                        caller_id=caller.caller_id,
                        body=body,
                        raw_payload=payload,
                    )
                except UnknownService:
                    # One answer for unknown and forbidden, so existence isn't revealed
                    # (Artifact 7.4). Dead-lettering arrives with full intake (M1).
                    raise ApiError(
                        404,
                        "unknown_service",
                        "Unknown service",
                        f"No service {body.service!r} in environment {body.environment!r}.",
                    ) from None
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
        headers = {"Location": stored.body["links"]["self"]} if stored.status == 201 else None
        return JSONResponse(stored.body, status_code=stored.status, headers=headers)

    @app.get("/v1/incidents/{incident_id}", response_model=IncidentView)
    def read_incident(incident_id: UUID, caller: CallerDep, response: Response) -> IncidentView:
        with db.connect() as conn:
            view = load_incident(conn, incident_id, caller.org_id)
        if view is None:
            raise _not_found(incident_id)
        response.headers["ETag"] = f'"{view.version}"'
        return view

    @app.get("/v1/incidents/{incident_id}/timeline", response_model=Page[CloudEvent])
    def read_timeline(
        incident_id: UUID,
        caller: CallerDep,
        after_seq: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=TIMELINE_PAGE_MAX)] = 100,
    ) -> Page[CloudEvent]:
        with db.connect() as conn:
            if load_incident(conn, incident_id, caller.org_id) is None:
                raise _not_found(incident_id)
            items = timeline(conn, incident_id, caller.org_id, after_seq=after_seq, limit=limit)
        last = items[-1].sequence if len(items) == limit else None
        return Page[CloudEvent](items=items, next_after_seq=last)

    @app.get("/livez")
    def livez() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/readyz")
    def readyz() -> dict[str, str]:
        """Ready when the Incident Record answers. Dependency probes are T4.5's (WR-22)."""
        try:
            with db.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception:
            raise ApiError(503, "not_ready", "Not ready", "The database is unreachable.") from None
        return {"status": "ready"}

    return app


def _check_key(key: str | None) -> str:
    if key is None:
        raise ApiError(
            400, "idempotency_key_required", "Idempotency-Key required", "Every write needs one."
        )
    if not 8 <= len(key) <= 128:
        raise ApiError(
            400, "idempotency_key_invalid", "Invalid Idempotency-Key", "Use 8 to 128 characters."
        )
    return key


def _not_found(incident_id: UUID) -> ApiError:
    return ApiError(404, "not_found", "Incident not found", f"No incident {incident_id}.")


def _install_errors(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_id(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        given = request.headers.get(REQUEST_ID_HEADER, "")
        request.state.request_id = given if 0 < len(given) <= 128 else str(uuid7())
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request.state.request_id
        return response

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError) -> Response:
        return problem_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> Response:
        err = ApiError(
            422, "validation_failed", "Invalid request", errors=field_errors(exc.errors())
        )
        return problem_response(request, err)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> Response:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return problem_response(request, ApiError(exc.status_code, code, str(exc.detail)))

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> Response:
        return problem_response(request, ApiError(500, "internal_error", "Internal error"))
