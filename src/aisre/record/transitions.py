"""Artifact 7.2, 8.2, 13.5 T0.11: the workflow's writes to the Incident Record.

`snapshot` reads what `load_incident` returns, by incident ID alone: an activity carries no
organisation, and the ID is unique across organisations. `record_transition` applies one
workflow step in the caller's transaction: the incident's status, at most two action status
changes, the investigation_queue row's removal, and one timeline event. `hand_over` writes the
hand-over event. Neither commits.

Idempotency. Each write's key (`transition_id`, `handover_key`) gives its event a stable ID,
uuid5 over the key, checked under the incident's row lock before anything is written. A key
already used returns what that write returned and changes nothing, so a retried activity whose
first attempt committed is a no-op.

Optimistic concurrency (ADR-69). The status update matches `version = expected_version` and
`status = from_state`, and sets `version = expected_version + 1` (t40_version needs a rise of
exactly 1). No row matched means a person changed the incident since the workflow read it:
StaleVersion, and the workflow reloads and writes again. A write that leaves the status as it
is (an action change alone) leaves the version alone too, as t40_version requires, and still
checks it.

The database checks every transition against its tables (check_incident_transition,
check_action_transition); its refusals reach the caller as DBAPIError with their SQLSTATE.

No temporalio here: aisre.api imports aisre.record.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import Connection, Row, delete, select, update

from aisre.contracts import IncidentHandedOverData, IncidentStatus, IncidentStatusChangedData
from aisre.record.incidents import append_event
from aisre.record.tables import (
    actions,
    environments,
    incidents,
    investigation_queue,
    services,
    timeline_events,
)

WORKER_SOURCE = "/aisre/worker/workflow"
WORKER_ACTOR = "system:workflow"
STATUS_CHANGED = "aisre.incident.status_changed.v1"
HANDED_OVER = "aisre.incident.handed_over.v1"
# uuid5 namespace for event IDs derived from a workflow idempotency key. Fixed: changing it
# would make a retried write look new.
EVENT_KEY_NAMESPACE = UUID("6f1c9a52-3e0b-5d7a-9c41-2b8e7d0f4a13")


class StaleVersion(Exception):
    """The incident isn't at the expected version and status: someone changed it."""


class ActionMismatch(Exception):
    """An action named in the transition isn't in its `from` status (or isn't this incident's)."""


class UnknownIncident(LookupError):
    """No incident has this ID."""


@dataclass(frozen=True)
class Snapshot:
    """The record's part of activities.IncidentSnapshot."""

    incident_id: UUID
    service: str
    environment: str
    version: int
    status: str


@dataclass(frozen=True)
class Transition:
    """activities.TransitionIn's fields (that module imports temporalio, this one can't)."""

    incident_id: str
    transition_id: str
    from_state: str
    to_state: str
    expected_version: int
    reason: str
    action_id: str | None = None
    action_from: str | None = None
    action_to: str | None = None
    second_action_id: str | None = None
    second_action_from: str | None = None
    second_action_to: str | None = None


def event_id_for(key: str) -> UUID:
    return uuid5(EVENT_KEY_NAMESPACE, key)


def snapshot(conn: Connection, incident_id: UUID) -> Snapshot | None:
    row = conn.execute(
        select(
            incidents.c.incident_id,
            services.c.slug.label("service"),
            environments.c.slug.label("environment"),
            incidents.c.version,
            incidents.c.status,
        )
        .join(services, services.c.service_id == incidents.c.service_id)
        .join(environments, environments.c.environment_id == incidents.c.environment_id)
        .where(incidents.c.incident_id == incident_id)
    ).first()
    if row is None:
        return None
    return Snapshot(row.incident_id, row.service, row.environment, row.version, row.status)


def record_transition(conn: Connection, t: Transition) -> tuple[int, int]:
    """Apply one workflow step; return (the incident's version after it, the event's seq)."""
    incident_id = UUID(t.incident_id)
    current = _lock(conn, incident_id)
    event_id = event_id_for(t.transition_id)
    done = _seq_of(conn, event_id)
    if done is not None:
        return current.version, done  # applied before: nothing more to do

    if t.to_state != t.from_state:
        version = conn.execute(
            update(incidents)
            .where(
                incidents.c.incident_id == incident_id,
                incidents.c.version == t.expected_version,
                incidents.c.status == t.from_state,
            )
            .values(status=t.to_state, version=t.expected_version + 1)
            .returning(incidents.c.version)
        ).scalar()
    elif (current.version, current.status) == (t.expected_version, t.from_state):
        version = current.version
    else:
        version = None
    if version is None:
        raise StaleVersion(
            f"incident {incident_id} is {current.status} at version {current.version}, "
            f"not {t.from_state} at {t.expected_version}"
        )

    for action_id, a_from, a_to in (
        (t.action_id, t.action_from, t.action_to),
        (t.second_action_id, t.second_action_from, t.second_action_to),
    ):
        if action_id and a_to:
            _move_action(conn, incident_id, UUID(action_id), a_from, a_to)

    investigating, open_, cancelled = (
        IncidentStatus.investigating.value,
        IncidentStatus.open.value,
        IncidentStatus.cancelled.value,
    )
    leaves_investigating = t.from_state == investigating and t.to_state != investigating
    if leaves_investigating or (t.from_state, t.to_state) == (open_, cancelled):
        conn.execute(
            delete(investigation_queue).where(investigation_queue.c.incident_id == incident_id)
        )

    seq = append_event(
        conn,
        incident_id,
        STATUS_CHANGED,
        WORKER_ACTOR,
        IncidentStatusChangedData(
            from_status=IncidentStatus(t.from_state),
            to_status=IncidentStatus(t.to_state),
            reason=t.reason,
        ),
        occurred_at=datetime.now(UTC),
        source=WORKER_SOURCE,
        event_id=event_id,
    )
    return version, seq


def hand_over(conn: Connection, incident_id: UUID, reason: str, handover_key: str) -> int:
    """Write aisre.incident.handed_over.v1 once per key; return its seq.

    `from_status` is the state the incident was handed over from, read from the transition
    into `handed_over` that record_transition wrote before this."""
    current = _lock(conn, incident_id)
    event_id = event_id_for(handover_key)
    done = _seq_of(conn, event_id)
    if done is not None:
        return done
    came_from = conn.execute(
        select(timeline_events.c.data["from_status"].astext)
        .where(
            timeline_events.c.incident_id == incident_id,
            timeline_events.c.type == STATUS_CHANGED,
            timeline_events.c.data["to_status"].astext == IncidentStatus.handed_over.value,
        )
        .order_by(timeline_events.c.seq.desc())
        .limit(1)
    ).scalar()
    return append_event(
        conn,
        incident_id,
        HANDED_OVER,
        WORKER_ACTOR,
        IncidentHandedOverData(
            from_status=IncidentStatus(came_from or current.status), reason=reason
        ),
        occurred_at=datetime.now(UTC),
        source=WORKER_SOURCE,
        event_id=event_id,
    )


def _lock(conn: Connection, incident_id: UUID) -> Row[Any, Any]:
    row = conn.execute(
        select(incidents.c.version, incidents.c.status)
        .where(incidents.c.incident_id == incident_id)
        .with_for_update()
    ).first()
    if row is None:
        raise UnknownIncident(str(incident_id))
    return row


def _seq_of(conn: Connection, event_id: UUID) -> int | None:
    seq = conn.execute(
        select(timeline_events.c.seq).where(timeline_events.c.event_id == event_id)
    ).scalar()
    return None if seq is None else int(seq)


def _move_action(
    conn: Connection, incident_id: UUID, action_id: UUID, a_from: str | None, a_to: str
) -> None:
    moved = conn.execute(
        update(actions)
        .where(
            actions.c.action_id == action_id,
            actions.c.incident_id == incident_id,
            actions.c.status == a_from,
        )
        .values(status=a_to, version=actions.c.version + 1)
    ).rowcount
    if moved != 1:
        raise ActionMismatch(f"action {action_id} is not {a_from} on incident {incident_id}")
