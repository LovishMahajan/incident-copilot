"""Artifact 7.4: intake, read and timeline, plus the two probes.

    POST /v1/incidents                         IncidentCreate + Idempotency-Key -> 201 / 200
    GET  /v1/incidents/{id}                    IncidentView, ETag = version
    GET  /v1/incidents/{id}/timeline?after_seq Page[CloudEvent]
    GET  /v1/incidents/{id}/evidence?kind&cursor Page[EvidenceRecord]          (T1.5)
    GET  /v1/incidents/{id}/diagnoses/current  Diagnosis
    GET  /v1/incidents/{id}/actions            list[ActionView]; GET /v1/actions/{id}
    GET  /v1/artifacts/{id}/content            a patch's canonical diff (M2: patch_diff only)
    GET  /v1/incidents/{id}/timeline/stream    SSE (aisre.api.stream)
    GET  /v1/incidents/{id}/timeline/export    signed JSON (aisre.api.export)
    GET  /v1/incidents/{id}/audit/export       signed JSON, from the write-once copy (T1.20)
    POST /v1/incidents/{id}/notes, /cancellation, /severity-confirmation, /closure,
         /reinvestigations                     aisre.api.commands (T1.6)
    GET  /v1/actions/{id}/approval-request, /approvals; POST /approvals, /rejections,
         /revocations                          aisre.api.approvals (T3.2)
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

import asyncio
import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Protocol
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException

from aisre.api import audit, observe
from aisre.api.approvals import Approvals, stop_check
from aisre.api.approvals import register as register_approvals
from aisre.api.auth import Authenticator, Caller, current_caller
from aisre.api.commands import Commands, parse_if_match
from aisre.api.export import ExportSigner, audit_document, timeline_document
from aisre.api.intake import Intake
from aisre.api.permissions import check_service, content_read, requires
from aisre.api.problems import ApiError, field_errors, problem_response
from aisre.api.stream import MEDIA_TYPE, Streams, Timing, starting_point
from aisre.config import Settings
from aisre.contracts import (
    ActionView,
    CancellationCreate,
    ClosureCreate,
    CloudEvent,
    Diagnosis,
    EvidenceKind,
    EvidenceRecord,
    IncidentAccepted,
    IncidentView,
    NoteCreate,
    Page,
    ReinvestigationCreate,
    SeverityConfirmationCreate,
)
from aisre.evidence.snapshots import S3Snapshots, SnapshotFailed
from aisre.record import commands as cmd
from aisre.record import load_incident, make_engine, reads, timeline, uuid7
from aisre.record.audit_copy import WriteOnceStore, store_from_settings
from aisre.reliability import health, telemetry
from aisre.remediation.action import Perm, Signer, development_signer

REQUEST_ID_HEADER = "X-Request-ID"
TIMELINE_PAGE_MAX = 500
EVIDENCE_PAGE_MAX = 200

CallerDep = Annotated[Caller, Depends(current_caller)]


class ArtifactReader(Protocol):
    def get(self, key: str) -> bytes: ...


KeyDep = Annotated[str | None, Header(alias="Idempotency-Key")]
IfMatchDep = Annotated[str | None, Header(alias="If-Match")]


def create_app(
    settings: Settings | None = None,
    engine: Engine | None = None,
    *,
    stream_timing: Timing | None = None,
    audit_store: WriteOnceStore | None = None,
    artifact_store: ArtifactReader | None = None,
) -> FastAPI:
    """The API. Without a token verifier, refuses to build outside development and test
    (aisre.api.auth). The audit copy's store defaults to the AISRE_AUDIT_STORE_* settings."""
    settings = settings or Settings.from_env()
    authenticator = Authenticator(settings)
    signer = ExportSigner.from_settings(settings)
    store = audit_store if audit_store is not None else store_from_settings(settings)
    audit.install_handler()
    copy = audit.install_copy(store)
    db = engine or make_engine(settings.database_url)
    blobs = artifact_store if artifact_store is not None else _artifact_store(settings)

    probes = api_health(settings, db, store)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        telemetry.setup_tracing("aisre-api", settings.otlp_endpoint)
        metrics = telemetry.serve_metrics(settings.metrics_port)  # when set (T1.19)
        stop = asyncio.Event()
        probing = asyncio.create_task(probes.run(stop))  # T4.5: every 15 s
        yield
        stop.set()
        await probing
        if metrics is not None:
            metrics.shutdown()
        if engine is None:
            db.dispose()

    app = FastAPI(title="AI SRE platform API", version="0.1.0", lifespan=lifespan)
    app.state.authenticator = authenticator
    _install_errors(app)
    observe.install(app)

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

    commands = Commands(db)

    @app.post("/v1/incidents/{incident_id}/notes", status_code=201, **requires(Perm.NOTE))
    def add_note(
        request: Request,
        incident_id: UUID,
        body: NoteCreate,
        caller: CallerDep,
        idempotency_key: KeyDep = None,
    ) -> JSONResponse:
        return commands.run(
            request,
            caller,
            incident_id,
            Perm.NOTE,
            idempotency_key,
            body,
            lambda c, inc: cmd.add_note(c, inc, caller.caller_id, body.note),
            status=201,
        )

    @app.post("/v1/incidents/{incident_id}/cancellation", status_code=202, **requires(Perm.CANCEL))
    def cancel(
        request: Request,
        incident_id: UUID,
        body: CancellationCreate,
        caller: CallerDep,
        idempotency_key: KeyDep = None,
    ) -> JSONResponse:
        """A stop (ADR-69): the dispatcher refuses this incident's existing actions, and the
        workflow is signalled through the outbox. Accepted is not stopped: 202."""
        return commands.run(
            request,
            caller,
            incident_id,
            Perm.CANCEL,
            idempotency_key,
            body,
            lambda c, inc: cmd.cancel(c, inc, caller.caller_id, body.reason, stop_check(caller)),
            status=202,
        )

    @app.post(
        "/v1/incidents/{incident_id}/severity-confirmation",
        **requires(Perm.CONFIRM_SEVERITY),
    )
    def confirm_severity(
        request: Request,
        incident_id: UUID,
        body: SeverityConfirmationCreate,
        caller: CallerDep,
        idempotency_key: KeyDep = None,
        if_match: IfMatchDep = None,
    ) -> JSONResponse:
        version = parse_if_match(if_match)
        return commands.run(
            request,
            caller,
            incident_id,
            Perm.CONFIRM_SEVERITY,
            idempotency_key,
            body,
            lambda c, inc: cmd.confirm_severity(c, inc, caller.caller_id, body.severity, version),
            status=200,
            if_match=version,
        )

    @app.post("/v1/incidents/{incident_id}/closure", **requires(Perm.CLOSE))
    def close(
        request: Request,
        incident_id: UUID,
        body: ClosureCreate,
        caller: CallerDep,
        idempotency_key: KeyDep = None,
        if_match: IfMatchDep = None,
    ) -> JSONResponse:
        version = parse_if_match(if_match)
        return commands.run(
            request,
            caller,
            incident_id,
            Perm.CLOSE,
            idempotency_key,
            body,
            lambda c, inc: cmd.close(c, inc, caller.caller_id, body.note, version),
            status=200,
            if_match=version,
        )

    @app.post(
        "/v1/incidents/{incident_id}/reinvestigations",
        status_code=202,
        **requires(Perm.REINVESTIGATE),
    )
    def reinvestigate(
        request: Request,
        incident_id: UUID,
        body: ReinvestigationCreate,
        caller: CallerDep,
        idempotency_key: KeyDep = None,
        if_match: IfMatchDep = None,
    ) -> JSONResponse:
        """ADR-46: only from handed_over. Starts `incident-<id>-r<N>` through the outbox."""
        version = parse_if_match(if_match)
        return commands.run(
            request,
            caller,
            incident_id,
            Perm.REINVESTIGATE,
            idempotency_key,
            body,
            lambda c, inc: cmd.reinvestigate(c, inc, caller.caller_id, body.reason, version),
            status=202,
            if_match=version,
        )

    @app.get(
        "/v1/incidents/{incident_id}/evidence",
        response_model=Page[EvidenceRecord],
        **requires(Perm.READ),
    )
    def read_evidence(
        request: Request,
        incident_id: UUID,
        caller: CallerDep,
        kind: EvidenceKind | None = None,
        cursor: UUID | None = None,
        limit: Annotated[int, Query(ge=1, le=EVIDENCE_PAGE_MAX)] = 100,
    ) -> Page[EvidenceRecord]:
        with db.connect() as conn:
            readable(request, caller, load_incident(conn, incident_id, caller.org_id), incident_id)
            items = reads.evidence_page(
                conn,
                incident_id,
                kind=kind.value if kind else None,
                after=cursor,
                limit=limit,
            )
            shown = reads.restrict_documents(conn, caller.org_id, items, caller.groups)
        content_read(request, caller, incident_id, "evidence")
        last = str(items[-1].evidence_id) if len(items) == limit else None
        return Page[EvidenceRecord](items=shown, next_cursor=last)

    @app.get(
        "/v1/incidents/{incident_id}/diagnoses/current",
        response_model=Diagnosis,
        **requires(Perm.READ),
    )
    def read_diagnosis(request: Request, incident_id: UUID, caller: CallerDep) -> Diagnosis:
        with db.connect() as conn:
            readable(request, caller, load_incident(conn, incident_id, caller.org_id), incident_id)
            diagnosis = reads.current_diagnosis(conn, incident_id)
        if diagnosis is None:
            raise ApiError(404, "no_diagnosis", "No diagnosis yet", f"Incident {incident_id}.")
        content_read(request, caller, incident_id, "diagnosis")
        return diagnosis

    @app.get(
        "/v1/incidents/{incident_id}/actions",
        response_model=list[ActionView],
        **requires(Perm.READ),
    )
    def read_actions(request: Request, incident_id: UUID, caller: CallerDep) -> list[ActionView]:
        with db.connect() as conn:
            readable(request, caller, load_incident(conn, incident_id, caller.org_id), incident_id)
            found = reads.actions(conn, incident_id)
        content_read(request, caller, incident_id, "actions")
        return found

    @app.get("/v1/actions/{action_id}", response_model=ActionView, **requires(Perm.READ))
    def read_action(request: Request, action_id: UUID, caller: CallerDep) -> ActionView:
        missing = ApiError(404, "not_found", "Action not found", f"No action {action_id}.")
        with db.connect() as conn:
            incident_id = reads.incident_of_action(conn, action_id, caller.org_id)
            if incident_id is None:
                raise missing
            view = load_incident(conn, incident_id, caller.org_id)
            if view is None or not check_service(
                request, caller, Perm.READ, view.service, incident_id=incident_id
            ):
                raise missing
            (action,) = reads.actions(conn, incident_id, action_id)
        content_read(request, caller, incident_id, "action")
        return action

    @app.get("/v1/artifacts/{artifact_id}/content", **requires(Perm.READ))
    def read_artifact_content(request: Request, artifact_id: UUID, caller: CallerDep) -> Response:
        """Artifact 7.4, ADR-50 (read incidents, timeline, artifacts). M2 serves one kind, a
        patch's canonical diff (`PatchView.diff_artifact_id`): repository text that passed the
        output checks. Evidence snapshots and model input and output stay unserved until their
        redaction and restricted-document rules are applied to the bytes, so they are 404."""
        missing = ApiError(404, "not_found", "Artifact not found", f"No artifact {artifact_id}.")
        with db.connect() as conn:
            found = reads.artifact(conn, artifact_id, caller.org_id)
            if found is None or found.kind != "patch_diff" or blobs is None:
                raise missing
            view = load_incident(conn, found.incident_id, caller.org_id)
            if view is None or not check_service(
                request, caller, Perm.READ, view.service, incident_id=found.incident_id
            ):
                raise missing
        try:
            body = blobs.get(found.storage_key)
        except SnapshotFailed as e:
            raise ApiError(
                503, "artifact_unavailable", "Artifact unavailable", "The store did not answer."
            ) from e
        if hashlib.sha256(body).hexdigest() != found.content_sha256:
            raise ApiError(
                500, "artifact_changed", "Artifact changed", "The stored bytes fail their hash."
            )
        content_read(request, caller, found.incident_id, "artifact")
        return Response(
            body,
            media_type=found.media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{artifact_id}.json"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    register_approvals(app, Approvals(db, approval_signer(settings)))

    streams = Streams(db, stream_timing)

    @app.get("/v1/incidents/{incident_id}/timeline/stream", **requires(Perm.READ))
    async def stream_timeline(
        request: Request,
        incident_id: UUID,
        caller: CallerDep,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
        after_seq: Annotated[int | None, Query(ge=0)] = None,
    ) -> StreamingResponse:
        """Artifact 7.9: replay after the starting point, then follow, until closed or
        cancelled (aisre.api.stream)."""
        start = starting_point(last_event_id, after_seq)

        def check() -> IncidentView:
            with db.connect() as conn:
                found = load_incident(conn, incident_id, caller.org_id)
            return readable(request, caller, found, incident_id)

        view = await run_in_threadpool(check)
        streams.reserve(caller)
        content_read(request, caller, incident_id, "timeline_stream")
        return StreamingResponse(
            streams.events(request, caller, incident_id, view.service, start),
            media_type=MEDIA_TYPE,
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/v1/incidents/{incident_id}/timeline/export", **requires(Perm.READ))
    def export_timeline(request: Request, incident_id: UUID, caller: CallerDep) -> JSONResponse:
        with db.connect() as conn:
            readable(request, caller, load_incident(conn, incident_id, caller.org_id), incident_id)
            doc = timeline_document(conn, incident_id, caller.org_id, caller.caller_id)
        content_read(request, caller, incident_id, "timeline_export")
        name = f"incident-{incident_id}-timeline.json"
        return JSONResponse(
            signer.sign(doc),
            headers={
                "Content-Disposition": f'attachment; filename="{name}"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/v1/incidents/{incident_id}/audit/export", **requires(Perm.AUDIT_EXPORT))
    def export_audit(request: Request, incident_id: UUID, caller: CallerDep) -> JSONResponse:
        """ADR-50: the full audit export, read back from the write-once copy (T1.20)."""
        with db.connect() as conn:
            view = readable(
                request, caller, load_incident(conn, incident_id, caller.org_id), incident_id
            )
        if not check_service(
            request, caller, Perm.AUDIT_EXPORT, view.service, incident_id=incident_id
        ):
            raise ApiError(403, "forbidden", "Forbidden", "Your roles don't grant audit:export.")
        if store is None or copy is None:
            raise ApiError(
                503, "audit_copy_unavailable", "Audit copy unavailable", "No store configured."
            )
        content_read(request, caller, incident_id, "audit_export")
        copy.flush()  # this process's lines so far, the read just logged included
        doc = audit_document(store, view, caller.caller_id)
        name = f"incident-{incident_id}-audit.json"
        return JSONResponse(
            signer.sign(doc),
            headers={
                "Content-Disposition": f'attachment; filename="{name}"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/livez")
    async def livez() -> dict[str, str]:
        """Only that the event loop answers (12.5): failing means restart the process."""
        return {"status": "alive"}

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        """12.5, T4.5 (WR-22): the database, the schema version, the configuration and, on the
        approval path, the approval signing key. Never Temporal, the model provider or the
        collector. Failing means stop sending traffic; sets aisre_process_ready{process="api"}."""
        report = await probes.ready()
        if not report.ready:
            failed = ", ".join(sorted(report.failing))
            raise ApiError(503, "not_ready", "Not ready", f"Failing checks: {failed}.")
        return {"status": "ready"}

    return app


def approval_path_on(settings: Settings) -> bool:
    """release.remediation_enabled's rule, restated: ST-39 keeps the release module out of the
    API package (tests/unit/test_health.py holds the two equal)."""
    return settings.env in ("development", "test") and settings.release_adapter == "fake_release"


def api_health(settings: Settings, db: Engine, store: WriteOnceStore | None) -> health.Health:
    """The API's readiness checks and its probes: PostgreSQL and the object store (the audit
    copy). It never talks to Temporal (ADR-19), so it doesn't probe it."""
    readiness = {
        "database": health.database(db),
        "schema_version": health.schema_version(db),
        "configuration": health.present("database URL", settings.database_url.get_secret_value()),
    }
    if approval_path_on(settings):  # the API alone signs decisions (ADR-73)
        readiness["approval_signing_key"] = health.present(
            "approval signing key", settings.approval_signing_key
        )
    dependencies = {"postgresql": health.database(db)}
    if store is not None:
        dependencies["object_store"] = health.object_store(store)
    return health.Health("api", readiness, dependencies)


def approval_signer(settings: Settings) -> Signer | None:
    """ADR-73: the development signing key, when set (Settings refuses it outside development
    and test). Without one, the approval writes answer 503."""
    key = settings.approval_signing_key
    return development_signer(key.get_secret_value()) if key is not None else None


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

    @app.exception_handler(OperationalError)
    async def record_unavailable(request: Request, exc: OperationalError) -> Response:
        """12.5: with PostgreSQL down the API answers 503, never acknowledging what it didn't
        store; on-call handles incidents by hand until it returns (T4.2)."""
        err = ApiError(
            503,
            "record_unavailable",
            "Incident Record unavailable",
            "The incident record can't be written now; retry, and page on-call directly.",
            headers={"Retry-After": "30"},
        )
        return problem_response(request, err)

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> Response:
        return problem_response(request, ApiError(500, "internal_error", "Internal error"))


def _artifact_store(settings: Settings) -> ArtifactReader | None:
    """The object store artifacts live in (ADR-57), read-only use here. Unset: none served."""
    s = settings
    if not (
        s.object_store_url
        and s.object_store_bucket
        and s.object_store_access_key
        and s.object_store_secret_key
    ):
        return None
    return S3Snapshots(
        s.object_store_url,
        s.object_store_bucket,
        s.object_store_access_key,
        s.object_store_secret_key,
    )
