"""Artifact 7.4: intake, read and timeline, plus the two probes.

    POST /v1/incidents                         IncidentCreate + Idempotency-Key -> 201 / 200
    GET  /v1/incidents/{id}                    IncidentView, ETag = version
    GET  /v1/incidents/{id}/timeline?after_seq Page[CloudEvent]
    GET  /livez, /readyz

Intake (aisre.api.intake) is one transaction (Artifact 7.7): the idempotency key, then the
incident, its first event, the alert and the outbox row that starts the workflow; or, for a
duplicate or grouped alert, the alert attached to the open incident (200, `deduplicated:
true`). A rejected alert is dead-lettered (ADR-58). The relay in the worker delivers the
outbox row; this process never talks to Temporal (ADR-19).

Every `/v1` route names its caller through aisre.api.auth: a verified bearer token, or the
development-only header when no token verifier is configured (T1.3). Each declares its
ADR-50 permission with `**requires(...)`, and checks it again on the incident's own service
(aisre.api.permissions, T1.4).

Routes are plain `def`: the Incident Record is synchronous, so FastAPI runs them on its thread
pool (see aisre.record). Intake reads its body itself, so it is `async` and hands the
record's work to the same pool. Errors are RFC 9457 problem details (aisre.api.problems).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import Engine, text
from starlette.exceptions import HTTPException

from aisre.api import audit
from aisre.api.auth import Authenticator, Caller, current_caller
from aisre.api.intake import Intake
from aisre.api.permissions import check_service, content_read, requires
from aisre.api.problems import ApiError, field_errors, problem_response
from aisre.config import Settings
from aisre.contracts import CloudEvent, IncidentAccepted, IncidentView, Page
from aisre.record import load_incident, make_engine, timeline, uuid7
from aisre.remediation.action import Perm

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

    intake = Intake(db)

    @app.post(
        "/v1/incidents", status_code=201, response_model=IncidentAccepted, **requires(Perm.CREATE)
    )
    async def submit_incident(
        request: Request,
        caller: CallerDep,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> JSONResponse:
        """Body: an IncidentCreate, or the reference alert's envelope (aisre.api.alerts). Read
        raw, after sign-in, so even a body that isn't JSON is dead-lettered (aisre.api.intake)."""
        raw = await request.body()
        return await run_in_threadpool(intake.submit, request, caller, raw, idempotency_key)

    def readable(
        request: Request, caller: Caller, view: IncidentView | None, incident_id: UUID
    ) -> IncidentView:
        """The incident, if the caller may read it: else the same 404 as a missing one."""
        if view is None or not check_service(
            request, caller, Perm.READ, view.service, incident_id=incident_id
        ):
            raise _not_found(incident_id)
        return view

    @app.get("/v1/incidents/{incident_id}", response_model=IncidentView, **requires(Perm.READ))
    def read_incident(
        request: Request, incident_id: UUID, caller: CallerDep, response: Response
    ) -> IncidentView:
        with db.connect() as conn:
            view = readable(
                request, caller, load_incident(conn, incident_id, caller.org_id), incident_id
            )
        content_read(request, caller, incident_id, "incident")
        response.headers["ETag"] = f'"{view.version}"'
        return view

    @app.get(
        "/v1/incidents/{incident_id}/timeline",
        response_model=Page[CloudEvent],
        **requires(Perm.READ),
    )
    def read_timeline(
        request: Request,
        incident_id: UUID,
        caller: CallerDep,
        after_seq: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=TIMELINE_PAGE_MAX)] = 100,
    ) -> Page[CloudEvent]:
        with db.connect() as conn:
            readable(request, caller, load_incident(conn, incident_id, caller.org_id), incident_id)
            items = timeline(conn, incident_id, caller.org_id, after_seq=after_seq, limit=limit)
        content_read(request, caller, incident_id, "timeline")
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
