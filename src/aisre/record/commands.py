"""Artifact 7.4, 7.7, ADR-46, ADR-64, ADR-69 (T1.6): a person's writes to an incident.

Notes, cancellation, severity confirmation, closure and re-investigation. Each runs in the
caller's transaction, under the incident's row lock, and never commits; the API adds the
idempotency key to the same transaction. What the workflow must hear goes to the outbox as a
signal or a start (Artifact 7.7): nothing here talks to Temporal.

    note             note_added event. Any state; no version (ADR-64)
    cancel           stop_requests row, cancellation_requested event, cancellation signal.
                     No version: a cancel mustn't lose a race with the workflow (ADR-64). The
                     stop is what the dispatcher refuses on (ADR-69); the signal stops the run
    confirm severity confirmed_severity, version + 1, severity_confirmed event, the queue row's
                     severity, and a lead_decision signal
    close            recovered or handed_over -> closed, version + 1, status_changed and
                     closed events, and a lead_decision signal so the run ends
    incident review  handed_over, recovered or closed; once. An incident_reviews row naming the
                     current diagnosis, and a note_added event saying it was recorded (ADR-75)
    reinvestigate    handed_over only. A reinvestigation_requested event naming run N, a
                     lead_decision signal to the run that handed over, and a start of
                     `incident-<id>-r<N>`. The new run moves the incident handed_over ->
                     investigating itself, once it has a slot, and the database then raises
                     `investigation_run` to N (check_incident_transition, ADR-46)

Versioned changes take the `If-Match` version (ADR-64) and compare it under the lock:
`StaleVersion` if it isn't the current one. A state the change can't apply in is `Conflict`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Column,
    Connection,
    Table,
    Text,
    column,
    func,
    insert,
    select,
    table,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID

from aisre.contracts import (
    CancellationRequestedData,
    IncidentClosedData,
    IncidentReviewCreate,
    IncidentStatus,
    IncidentStatusChangedData,
    NoteAddedData,
    ReinvestigationRequestedData,
    Severity,
    SeverityConfirmedData,
)
from aisre.record.incidents import (
    SIGNAL_WORKFLOW,
    START_WORKFLOW,
    append_event,
    enqueue,
    uuid7,
    workflow_id,
)
from aisre.record.tables import (
    Timestamp,
    incidents,
    investigation_queue,
    metadata,
    services,
    timeline_events,
)
from aisre.record.transitions import STATUS_CHANGED

COMMAND_SOURCE = "/aisre/api/incident-commands"
NOTE_ADDED = "aisre.incident.note_added.v1"
CANCELLATION_REQUESTED = "aisre.incident.cancellation_requested.v1"
SEVERITY_CONFIRMED = "aisre.incident.severity_confirmed.v1"
CLOSED = "aisre.incident.closed.v1"
REINVESTIGATION_REQUESTED = "aisre.incident.reinvestigation_requested.v1"

S = IncidentStatus
TERMINAL = frozenset({S.closed, S.cancelled})
# Cancellable: the six states with a `cancelled` transition (ADR-46), and the protected ones,
# where the cancel waits for the change's outcome (ADR-69). Not handed_over or recovered: a
# person owns those already, and nothing is left to stop.
CANCELLABLE = frozenset(
    {
        S.open,
        S.investigating,
        S.diagnosed,
        S.inconclusive,
        S.testing,
        S.awaiting_approval,
        S.executing,
        S.reconciling,
        S.verifying,
        S.not_recovered,
    }
)
CLOSABLE = frozenset({S.recovered, S.handed_over})

# The incident columns a person's change writes, beyond tables.incidents'.
incident_writes = table(
    "incidents",
    column("incident_id"),
    column("version"),
    column("status"),
    column("confirmed_severity"),
    column("severity_confirmed_by"),
    column("closed_at"),
)
# Append-only (schema.sql); `requested_by` is never a `system:` identity.
stop_requests = Table(
    "stop_requests",
    metadata,
    Column("stop_request_id", PgUUID, primary_key=True),
    Column("incident_id", PgUUID, nullable=False),
    Column("requested_by", Text, nullable=False),
    Column("reason", Text, nullable=False),
    Column("requested_at", Timestamp, nullable=False),
)


class Conflict(Exception):
    """The incident's state doesn't allow this change (409)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code, self.detail = code, detail


class StaleVersion(Exception):
    """`If-Match` isn't the incident's current version (412)."""

    def __init__(self, current: int) -> None:
        super().__init__(f"current version is {current}")
        self.current = current


@dataclass(frozen=True)
class Locked:
    incident_id: UUID
    service: str
    status: IncidentStatus
    version: int
    investigation_run: int


@dataclass(frozen=True)
class Done:
    """What a command wrote: the incident's version after it and its last event's seq."""

    incident_id: UUID
    version: int
    sequence: int
    extra: dict[str, Any]


def lock(conn: Connection, incident_id: UUID, org_id: UUID) -> Locked | None:
    """The incident, locked FOR UPDATE, or None if it isn't this organisation's."""
    row = conn.execute(
        select(
            incidents.c.incident_id,
            services.c.slug,
            incidents.c.status,
            incidents.c.version,
            incidents.c.investigation_run,
        )
        .join(services, services.c.service_id == incidents.c.service_id)
        .where(incidents.c.incident_id == incident_id, incidents.c.org_id == org_id)
        .with_for_update(of=incidents)
    ).first()
    if row is None:
        return None
    return Locked(row[0], row[1], S(row[2]), row[3], row[4])


def add_note(conn: Connection, inc: Locked, caller_id: str, note: str) -> Done:
    seq = _event(conn, inc, NOTE_ADDED, caller_id, NoteAddedData(note=note))
    return Done(inc.incident_id, inc.version, seq, {})


def cancel(
    conn: Connection,
    inc: Locked,
    caller_id: str,
    reason: str,
    check: Callable[[Connection, Locked], None] | None = None,
) -> Done:
    """A person's stop (ADR-69), and the signal that stops the run. `check` is the Approval
    Service's `action.request_stop` over the incident's open actions (T3.5)."""
    if inc.status not in CANCELLABLE:
        raise Conflict(
            "not_cancellable", f"The incident is {inc.status.value}; nothing is left to cancel."
        )
    if check is not None:
        check(conn, inc)
    stop_request_id = uuid7()
    conn.execute(
        insert(stop_requests).values(
            stop_request_id=stop_request_id,
            incident_id=inc.incident_id,
            requested_by=caller_id,
            reason=reason,
        )
    )
    seq = _event(
        conn,
        inc,
        CANCELLATION_REQUESTED,
        caller_id,
        CancellationRequestedData(reason=reason),
        causation_id=stop_request_id,
    )
    _signal(
        conn,
        inc,
        "cancellation_requested",
        {"request_id": str(stop_request_id), "requested_by": caller_id, "reason": reason},
    )
    return Done(inc.incident_id, inc.version, seq, {"stop_request_id": str(stop_request_id)})


def confirm_severity(
    conn: Connection, inc: Locked, caller_id: str, severity: Severity, if_match: int
) -> Done:
    _check_version(inc, if_match)
    if inc.status in TERMINAL:
        raise Conflict("incident_finished", f"The incident is {inc.status.value}.")
    version = _bump(conn, inc, confirmed_severity=severity.value, severity_confirmed_by=caller_id)
    conn.execute(
        update(investigation_queue)
        .where(investigation_queue.c.incident_id == inc.incident_id)
        .values(severity=severity.value)
    )
    seq = _event(
        conn,
        inc,
        SEVERITY_CONFIRMED,
        caller_id,
        SeverityConfirmedData(confirmed_severity=severity, severity_confirmed_by=caller_id),
    )
    _lead_signal(conn, inc, "confirm_severity", caller_id, severity.value)
    return Done(inc.incident_id, version, seq, {})


def close(conn: Connection, inc: Locked, caller_id: str, note: str | None, if_match: int) -> Done:
    _check_version(inc, if_match)
    if inc.status not in CLOSABLE:
        raise Conflict(
            "not_closable",
            f"The incident is {inc.status.value}; only recovered or handed_over closes.",
        )
    closed_at = datetime.now(UTC)
    version = _bump(conn, inc, status=S.closed.value, closed_at=closed_at)
    _event(
        conn,
        inc,
        STATUS_CHANGED,
        caller_id,
        IncidentStatusChangedData(from_status=inc.status, to_status=S.closed, reason=note),
    )
    seq = _event(
        conn,
        inc,
        CLOSED,
        caller_id,
        IncidentClosedData(from_status=inc.status, closed_at=closed_at, note=note),
    )
    _lead_signal(conn, inc, "close", caller_id)
    return Done(inc.incident_id, version, seq, {})


REVIEWABLE = frozenset({S.handed_over, S.recovered, S.closed})


def record_review(
    conn: Connection, inc: Locked, caller_id: str, review: IncidentReviewCreate
) -> Done:
    """ADR-75: the incident review's findings, once, against the incident's current diagnosis.
    Confirming or refuting a proposed cause needs one to have been proposed, and is required
    when one was."""
    if inc.status not in REVIEWABLE:
        raise Conflict(
            "not_reviewable",
            f"The incident is {inc.status.value}; a review is recorded once a person owns it.",
        )
    diagnosis = conn.execute(
        text(
            "SELECT diagnosis_id, COALESCE(final_outcome, outcome)::text FROM diagnoses "
            "WHERE incident_id = :i ORDER BY revision DESC LIMIT 1"
        ),
        {"i": inc.incident_id},
    ).first()
    named = diagnosis is not None and diagnosis[1] == "established"
    if named != (review.proposed_cause_confirmed is not None):
        raise Conflict(
            "proposed_cause",
            "Give proposed_cause_confirmed exactly when the diagnosis named a cause"
            f" (it {'did' if named else 'did not'}).",
        )
    written = conn.execute(
        text(
            "INSERT INTO incident_reviews (incident_id, diagnosis_id, cause, cause_category, "
            "cause_identified_at, proposed_cause_confirmed, should_have_handed_over, "
            "engineer_hours, fix_reversed, reviewed_by) VALUES (:i, :d, :cause, "
            "CAST(:category AS hypothesis_category), :at, :confirmed, :handover, :hours, "
            ":reversed, :by) ON CONFLICT (incident_id) DO NOTHING RETURNING incident_id"
        ),
        {
            "i": inc.incident_id,
            "d": diagnosis[0] if diagnosis else None,
            "cause": review.cause,
            "category": review.cause_category.value,
            "at": review.cause_identified_at,
            "confirmed": review.proposed_cause_confirmed,
            "handover": review.should_have_handed_over,
            "hours": review.engineer_hours,
            "reversed": review.fix_reversed,
            "by": caller_id,
        },
    ).first()
    if written is None:
        raise Conflict("already_reviewed", "This incident's review is already recorded.")
    verdict = {
        None: "no cause was proposed",
        True: "the proposed cause is confirmed",
        False: "the proposed cause is not confirmed",
    }[review.proposed_cause_confirmed]
    note = f"Incident review recorded: {verdict}."
    seq = _event(conn, inc, NOTE_ADDED, caller_id, NoteAddedData(note=note))
    return Done(inc.incident_id, inc.version, seq, {})


def reinvestigate(
    conn: Connection, inc: Locked, caller_id: str, reason: str, if_match: int
) -> Done:
    _check_version(inc, if_match)
    if inc.status != S.handed_over:
        raise Conflict(
            "not_handed_over",
            f"The incident is {inc.status.value}; only a handed-over incident is re-investigated.",
        )
    run = inc.investigation_run + 1
    already = conn.execute(
        select(func.count())
        .select_from(timeline_events)
        .where(
            timeline_events.c.incident_id == inc.incident_id,
            timeline_events.c.type == REINVESTIGATION_REQUESTED,
            timeline_events.c.data["investigation_run"].as_integer() == run,
        )
    ).scalar_one()
    if already:
        raise Conflict("reinvestigation_requested", f"Run {run} has already been requested.")
    seq = _event(
        conn,
        inc,
        REINVESTIGATION_REQUESTED,
        caller_id,
        ReinvestigationRequestedData(investigation_run=run, reason=reason),
    )
    _lead_signal(conn, inc, "reinvestigate", caller_id)
    enqueue(
        conn,
        inc.incident_id,
        START_WORKFLOW,
        {"incident_id": str(inc.incident_id), "run": run},
        run=run,
    )
    return Done(
        inc.incident_id,
        inc.version,
        seq,
        {"investigation_run": run, "workflow_id": workflow_id(inc.incident_id, run)},
    )


# ---------------------------------------------------------------------------
def _check_version(inc: Locked, if_match: int) -> None:
    if if_match != inc.version:
        raise StaleVersion(inc.version)


def _bump(conn: Connection, inc: Locked, **values: Any) -> int:
    i = incident_writes
    version = conn.execute(
        update(i)
        .where(i.c.incident_id == inc.incident_id, i.c.version == inc.version)
        .values(version=inc.version + 1, **values)
        .returning(i.c.version)
    ).scalar_one()
    return int(version)


def _event(
    conn: Connection,
    inc: Locked,
    event_type: str,
    caller_id: str,
    data: Any,
    *,
    causation_id: UUID | None = None,
) -> int:
    return append_event(
        conn,
        inc.incident_id,
        event_type,
        caller_id,
        data,
        occurred_at=datetime.now(UTC),
        source=COMMAND_SOURCE,
        causation_id=causation_id,
    )


def _signal(conn: Connection, inc: Locked, name: str, arg: dict[str, Any]) -> None:
    """To the incident's current run, `incident-<id>` or `-r<N>`."""
    enqueue(
        conn,
        inc.incident_id,
        SIGNAL_WORKFLOW,
        {"signal": name, "arg": arg},
        run=inc.investigation_run,
    )


def _lead_signal(
    conn: Connection, inc: Locked, kind: str, caller_id: str, value: str | None = None
) -> None:
    arg: dict[str, Any] = {"request_id": str(uuid7()), "kind": kind, "by": caller_id}
    if value is not None:
        arg["value"] = value
    _signal(conn, inc, "lead_decision", arg)
