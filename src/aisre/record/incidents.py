"""Artifact 7.7, 7.8: intake, read and timeline on the Incident Record.

`create_incident` is the one write path for a new alert (ADR-19). In the caller's transaction
it either opens an incident, with its alert row, its first timeline event and an outbox row
that starts workflow `incident-<id>` (Artifact 7.7), or, when an open incident already holds
the alert's deduplication key, attaches the alert to that incident and returns it (FR-03,
Artifact 7.8). Nothing here commits: the caller owns the transaction, so the API can add the
idempotency key to the same one, and a failure anywhere leaves no part behind.

Timeline events: the application supplies the event ID, type, source, actor, time,
correlation and data; the database assigns `seq` from the incident's counter (gapless, as the
row lock serialises writers), `prev_hash`, `hash` and `recorded_at` (schema.sql section 12).

Intake runs under one transaction-scoped advisory lock. It serialises the dedup check with the
insert, so two copies of an alert arriving together give one incident (the partial unique
index `uq_incidents_open_dedup` stays the final guard), and it makes `display_ref` numbering
safe without a sequence the app role couldn't create.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, func, insert, select, text

from aisre.contracts import (
    AlertAttachedData,
    BudgetView,
    CloudEvent,
    IncidentCreate,
    IncidentCreatedData,
    IncidentStatus,
    IncidentView,
    Strict,
)
from aisre.record.tables import (
    actions,
    diagnoses,
    environments,
    incident_alerts,
    incident_budgets,
    incidents,
    outbox,
    services,
    timeline_cloudevents,
    timeline_events,
)

API_SOURCE = "/aisre/api/intake"
INTAKE_LOCK = 0x41495352_45494E54  # pg_advisory_xact_lock key: "AISREINT"
CLOSED_STATES = (IncidentStatus.closed.value, IncidentStatus.cancelled.value)  # not "open"
FINISHED_ACTIONS = (
    "test_failed",
    "rejected",
    "expired",
    "not_applied",
    "verified",
    "undone",
    "withdrawn",
)
START_WORKFLOW = "start_workflow"
SIGNAL_WORKFLOW = "signal_workflow"


def uuid7() -> UUID:
    """A UUIDv7 (RFC 9562): 48-bit Unix milliseconds, then random bits (ADR-20)."""
    ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")
    value = (ms & (2**48 - 1)) << 80 | rand & ~(0xF << 76 | 0x3 << 62)
    return UUID(int=value | 0x7 << 76 | 0x2 << 62)


def workflow_id(incident_id: UUID, run: int = 1) -> str:
    """Workflow `incident-<id>`, then `incident-<id>-r2` and on (ADR-46)."""
    return f"incident-{incident_id}" if run == 1 else f"incident-{incident_id}-r{run}"


def dedup_key(body: IncidentCreate) -> str:
    """The alert's fingerprint, or service plus alert name (FR-42, ADR-58). Never empty."""
    return body.fingerprint or f"{body.service}:{body.title}"


class UnknownService(LookupError):
    """The service or environment isn't in this organisation's catalogue."""


@dataclass(frozen=True)
class Intake:
    incident_id: UUID
    display_ref: str
    status: IncidentStatus
    deduplicated: bool


def create_incident(
    conn: Connection,
    *,
    org_id: UUID,
    caller_id: str,
    body: IncidentCreate,
    raw_payload: dict[str, Any] | None = None,
) -> Intake:
    """Open an incident for `body`, or attach it to the open one with the same dedup key.

    `raw_payload` is the alert as received, stored on its `incident_alerts` row; it defaults
    to `body`. Raises UnknownService. Runs in the caller's transaction and doesn't commit.
    """
    conn.execute(select(func.pg_advisory_xact_lock(INTAKE_LOCK)))
    service_id, environment_id = _catalogue_ids(conn, org_id, body)
    key = dedup_key(body)
    payload = raw_payload if raw_payload is not None else body.model_dump(mode="json")
    now = datetime.now(UTC)

    existing = conn.execute(
        select(incidents.c.incident_id, incidents.c.display_ref, incidents.c.status)
        .where(
            incidents.c.org_id == org_id,
            incidents.c.dedup_key == key,
            incidents.c.status.not_in(CLOSED_STATES),
        )
        .with_for_update()
    ).first()
    if existing is not None:
        alert_id = _insert_alert(conn, existing.incident_id, body, key, payload, now)
        _append_event(
            conn,
            existing.incident_id,
            "aisre.incident.alert_attached.v1",
            caller_id,
            AlertAttachedData(
                alert_id=alert_id,
                source_alert_id=body.source_alert_id,
                fingerprint=key,
                received_at=now,
            ),
            occurred_at=now,
        )
        return Intake(
            existing.incident_id, existing.display_ref, IncidentStatus(existing.status), True
        )

    incident_id = uuid7()
    display_ref = _next_display_ref(conn, now)
    conn.execute(
        insert(incidents).values(
            incident_id=incident_id,
            org_id=org_id,
            display_ref=display_ref,
            service_id=service_id,
            environment_id=environment_id,
            dedup_key=key,
            title=body.title,
            source=body.source,
            started_at=body.started_at,
            created_by=caller_id,
        )
    )
    _append_event(
        conn,
        incident_id,
        "aisre.incident.created.v1",
        caller_id,
        IncidentCreatedData(
            incident_id=incident_id,
            display_ref=display_ref,
            service=body.service,
            environment=body.environment,
            title=body.title,
            source=body.source,
            started_at=body.started_at,
        ),
        occurred_at=now,
    )
    _insert_alert(conn, incident_id, body, key, payload, now)
    enqueue(conn, incident_id, START_WORKFLOW, {"incident_id": str(incident_id), "run": 1})
    return Intake(incident_id, display_ref, IncidentStatus.open, False)


def enqueue(
    conn: Connection,
    incident_id: UUID,
    kind: str,
    payload: dict[str, Any],
    *,
    run: int = 1,
) -> UUID:
    """An outbox row for the relay (Artifact 7.7), in the caller's transaction.

    `start_workflow` carries IncidentWorkflowInput's fields; `signal_workflow` carries
    {"signal": <signal name>, "arg": <the signal's fields>}.
    """
    message_id = uuid7()
    conn.execute(
        insert(outbox).values(
            message_id=message_id,
            incident_id=incident_id,
            kind=kind,
            workflow_id=workflow_id(incident_id, run),
            payload=payload,
        )
    )
    return message_id


def load_incident(conn: Connection, incident_id: UUID, org_id: UUID) -> IncidentView | None:
    """The incident as IncidentView, or None if it isn't there or belongs to another org."""
    i = incidents
    row = conn.execute(
        select(
            i,
            services.c.slug.label("service"),
            environments.c.slug.label("environment"),
            incident_budgets.c.max_calls,
            incident_budgets.c.used_calls,
            incident_budgets.c.max_cost_micros,
            incident_budgets.c.spent_cost_micros,
            incident_budgets.c.reserved_cost_micros,
        )
        .join(services, services.c.service_id == i.c.service_id)
        .join(environments, environments.c.environment_id == i.c.environment_id)
        .outerjoin(incident_budgets, incident_budgets.c.incident_id == i.c.incident_id)
        .where(i.c.incident_id == incident_id, i.c.org_id == org_id)
    ).first()
    if row is None:
        return None
    diagnosis_id = conn.execute(
        select(diagnoses.c.diagnosis_id)
        .where(diagnoses.c.incident_id == incident_id)
        .order_by(diagnoses.c.revision.desc())
        .limit(1)
    ).scalar()
    open_actions = conn.execute(
        select(actions.c.action_id)
        .where(actions.c.incident_id == incident_id, actions.c.status.not_in(FINISHED_ACTIONS))
        .order_by(actions.c.created_at)
    ).scalars()
    return IncidentView(
        incident_id=row.incident_id,
        display_ref=row.display_ref,
        service=row.service,
        environment=row.environment,
        title=row.title,
        status=IncidentStatus(row.status),
        proposed_severity=row.proposed_severity,
        confirmed_severity=row.confirmed_severity,
        started_at=row.started_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        version=row.version,
        last_event_seq=row.last_event_seq,
        current_diagnosis_id=diagnosis_id,
        open_action_ids=list(open_actions),
        # No budget row until the gateway allocates one (M1): show an empty budget.
        budget=BudgetView(
            max_calls=row.max_calls or 0,
            used_calls=row.used_calls or 0,
            max_cost_micros=row.max_cost_micros or 0,
            spent_cost_micros=row.spent_cost_micros or 0,
            reserved_cost_micros=row.reserved_cost_micros or 0,
        ),
    )


def timeline(
    conn: Connection, incident_id: UUID, org_id: UUID, *, after_seq: int = 0, limit: int = 100
) -> list[CloudEvent]:
    """Events with `sequence > after_seq`, in order, as CloudEvents (Q7.30)."""
    e = timeline_cloudevents
    rows = conn.execute(
        select(e)
        .where(e.c.incidentid == incident_id, e.c.orgid == org_id, e.c.sequence > after_seq)
        .order_by(e.c.sequence)
        .limit(limit)
    ).mappings()
    return [CloudEvent.model_validate(dict(r)) for r in rows]


# ---------------------------------------------------------------------------
# The individual writes. Separate functions so a test can fail one of them.
# ---------------------------------------------------------------------------
def _catalogue_ids(conn: Connection, org_id: UUID, body: IncidentCreate) -> tuple[UUID, UUID]:
    service_id = conn.execute(
        select(services.c.service_id).where(
            services.c.org_id == org_id, services.c.slug == body.service
        )
    ).scalar()
    environment_id = conn.execute(
        select(environments.c.environment_id).where(
            environments.c.org_id == org_id, environments.c.slug == body.environment
        )
    ).scalar()
    if service_id is None or environment_id is None:
        raise UnknownService(f"{body.service}/{body.environment}")
    return service_id, environment_id


def _next_display_ref(conn: Connection, now: datetime) -> str:
    """INC-<year>-<6 digits>, numbered within the year. Needs the intake lock."""
    prefix = f"INC-{now.year}-"
    last = conn.execute(
        text(
            "SELECT max(substring(display_ref FROM :start)::integer) FROM incidents "
            "WHERE display_ref LIKE :pattern"
        ),
        {"start": len(prefix) + 1, "pattern": prefix + "%"},
    ).scalar()
    return f"{prefix}{(last or 0) + 1:06d}"


def _insert_alert(
    conn: Connection,
    incident_id: UUID,
    body: IncidentCreate,
    fingerprint: str,
    payload: dict[str, Any],
    received_at: datetime,
) -> UUID:
    # The application's clock, not now(): two alerts in one transaction stay distinct
    # under UNIQUE (incident_id, source_alert_id, received_at).
    alert_id = uuid7()
    conn.execute(
        insert(incident_alerts).values(
            alert_id=alert_id,
            incident_id=incident_id,
            source_alert_id=body.source_alert_id,
            fingerprint=fingerprint,
            received_at=received_at,
            payload=payload,
        )
    )
    return alert_id


def _append_event(
    conn: Connection,
    incident_id: UUID,
    event_type: str,
    actor: str,
    data: Strict,
    *,
    occurred_at: datetime,
    source: str = API_SOURCE,
    causation_id: UUID | None = None,
) -> int:
    """Insert one timeline event and return the `seq` the database gave it."""
    seq = conn.execute(
        insert(timeline_events)
        .values(
            event_id=uuid7(),
            incident_id=incident_id,
            type=event_type,
            source=source,
            actor=actor,
            occurred_at=occurred_at,
            causation_id=causation_id,
            correlation_id=incident_id,
            data=data.model_dump(mode="json"),
        )
        .returning(timeline_events.c.seq)
    ).scalar_one()
    return int(seq)
