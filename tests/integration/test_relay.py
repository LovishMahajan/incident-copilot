"""Artifact 13.5 T0.10: the outbox relay against the Compose PostgreSQL and Temporal server.

The relay delivers a start and a signal, and a repeated start row is success, not an error.
The Compose worker runs its own relay on the same table; rows go to whichever claims them
first (SKIP LOCKED), so each test waits for its own rows to be delivered, by either.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import select
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client

from aisre.contracts import IncidentCreate
from aisre.record import SIGNAL_WORKFLOW, START_WORKFLOW, create_incident, enqueue, uuid7
from aisre.record.tables import outbox
from aisre.reliability import telemetry
from aisre.workflow import activities as act
from aisre.workflow.relay import Relay, backoff


@pytest.fixture
def temporal(temporal_address, settings):
    return asyncio.run(
        Client.connect(
            temporal_address,
            namespace=settings.temporal_namespace,
            data_converter=act.DATA_CONVERTER,
        )
    )


def new_incident(engine, org, service) -> UUID:
    body = IncidentCreate(
        service=service,
        environment="production",
        started_at=datetime.now(UTC),
        title="Relay test",
        source="engineer",
    )
    with engine.begin() as conn:
        return create_incident(conn, org_id=org, caller_id="dev:test", body=body).incident_id


def rows(engine, incident_id: UUID):
    with engine.connect() as conn:
        return conn.execute(
            select(outbox).where(outbox.c.incident_id == incident_id).order_by(outbox.c.outbox_id)
        ).all()


async def relay_until_delivered(relay: Relay, engine, incident_id: UUID, tries: int = 40) -> int:
    """Run passes until the incident's rows are delivered; return how many this relay did."""
    ours = 0
    for _ in range(tries):
        ours += await relay.run_once()
        if all(r.delivered_at for r in rows(engine, incident_id)):
            return ours
        await asyncio.sleep(0.25)
    raise AssertionError(f"outbox rows for {incident_id} not delivered")


def test_backoff_doubles_to_a_one_minute_cap():
    assert [backoff(n).total_seconds() for n in (1, 2, 3, 6, 7, 20)] == [1, 2, 4, 32, 60, 60]


def test_relay_starts_the_workflow_and_a_repeat_is_success(app_engine, catalogue, temporal):
    org, service, _ = catalogue
    incident_id = new_incident(app_engine, org, service)
    with app_engine.begin() as conn:  # the same start, sent again
        enqueue(conn, incident_id, START_WORKFLOW, {"incident_id": str(incident_id), "run": 1})
    before = telemetry.REGISTRY.get_sample_value("aisre_event_available_seconds_count") or 0

    async def go() -> int:
        ours = await relay_until_delivered(Relay(app_engine, temporal), app_engine, incident_id)
        desc = await temporal.get_workflow_handle(f"incident-{incident_id}").describe()
        assert desc.workflow_type == "IncidentWorkflow"
        assert desc.task_queue == act.INVESTIGATION_QUEUE
        return ours

    ours = asyncio.run(go())
    delivered = rows(app_engine, incident_id)
    assert len(delivered) == 2
    assert all(r.last_error is None and r.attempts == 1 for r in delivered)
    after = telemetry.REGISTRY.get_sample_value("aisre_event_available_seconds_count") or 0
    assert after - before == ours  # one observation per row this relay delivered


def test_relay_delivers_a_signal(app_engine, catalogue, temporal):
    org, service, _ = catalogue
    incident_id = new_incident(app_engine, org, service)
    request_id = str(uuid7())
    with app_engine.begin() as conn:
        enqueue(
            conn,
            incident_id,
            SIGNAL_WORKFLOW,
            {
                "signal": "cancellation_requested",
                "arg": {"request_id": request_id, "requested_by": "dev:alice", "reason": "test"},
            },
        )

    async def signals() -> list[str]:
        handle = temporal.get_workflow_handle(f"incident-{incident_id}")
        return [
            e.workflow_execution_signaled_event_attributes.signal_name
            async for e in handle.fetch_history_events()
            if e.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED
        ]

    async def go() -> list[str]:
        await relay_until_delivered(Relay(app_engine, temporal), app_engine, incident_id)
        # Temporal buffers a signal that arrives while a workflow task is running and writes
        # it to history only when that task completes. The first task of a new workflow can
        # take a while on a fresh worker, so a delivered signal may not be in history yet.
        for _ in range(40):
            if names := await signals():
                return names
            await asyncio.sleep(0.25)
        return []

    # Exactly one. Delivery is at least once (Artifact 7.7), so a retry after a reply lost on
    # the way back could signal twice, and the workflow would drop the repeat by its
    # request_id; on a local stack none was seen in 50 runs (2026-11-18). A retry
    # after a refused signal (the start not yet delivered by the other relay) sends one.
    names = asyncio.run(go())
    assert names == ["cancellation_requested"], (names, rows(app_engine, incident_id))


def test_a_signal_to_no_workflow_is_delivered_and_dropped(app_engine, catalogue, temporal):
    """T1.6: Temporal's NOT_FOUND means the run has ended (or never ran), so nothing is left to
    steer; the row is delivered, not retried (aisre.workflow.relay)."""
    org, service, _ = catalogue
    incident_id = new_incident(app_engine, org, service)
    with app_engine.begin() as conn:
        enqueue(
            conn,
            incident_id,
            SIGNAL_WORKFLOW,
            {"signal": "lead_decision", "arg": {"request_id": "r1", "kind": "close", "by": "x"}},
            run=2,  # incident-<id>-r2 was never started
        )

    async def go() -> None:
        relay = Relay(app_engine, temporal)
        for _ in range(40):
            await relay.run_once()
            row = [r for r in rows(app_engine, incident_id) if r.kind == SIGNAL_WORKFLOW][0]
            if row.delivered_at is not None:
                return
            await asyncio.sleep(0.25)

    asyncio.run(go())
    dropped = [r for r in rows(app_engine, incident_id) if r.kind == SIGNAL_WORKFLOW][0]
    assert dropped.delivered_at is not None and dropped.last_error is None
