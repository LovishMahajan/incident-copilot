"""Artifact 13.5 T3.10, T3.11 (8.9, WR-29, RB-04; ST-40): the reconciliation jobs against the
Compose stack's Temporal and PostgreSQL.

`reconcile_workflows` compares the record with every running IncidentWorkflow on the stack, so
other incidents may add mismatches; the checks here are on the two incidents the test makes:
one left `investigating` with no workflow, which counts in aisre_workflows_stalled, and one
`handed_over` with a queue row whose run has ended, which is removed. The schedule is the one the
worker creates.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from conftest import add_catalogue
from sqlalchemy import text
from temporalio.client import Client
from temporalio.testing import ActivityEnvironment

from aisre.record import uuid7
from aisre.record.transitions import Transition, record_transition
from aisre.reliability import telemetry
from aisre.workflow import activities as act
from aisre.workflow.reconciliation import ReconciliationActivities

pytestmark = pytest.mark.integration


def move(app_engine, incident_id, *states):
    with app_engine.begin() as conn:
        for to in states:
            status, version = conn.execute(
                text("SELECT status, version FROM incidents WHERE incident_id = :i"),
                {"i": incident_id},
            ).one()
            record_transition(
                conn, Transition(incident_id, str(uuid7()), status, to, version, "test")
            )


def test_t310_a_run_gone_missing_is_counted_and_an_ended_runs_queue_row_removed(
    app_engine, catalogue, quiet, temporal_address
):
    org, service, _ = catalogue
    lost = quiet(org, service)
    move(app_engine, lost, "investigating")  # no workflow will ever move it on
    other = uuid7()  # another organisation: an open incident would take a second alert
    add_catalogue(app_engine, other, service)
    done = quiet(other, service)
    move(app_engine, done, "investigating", "inconclusive", "handed_over")
    with app_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO investigation_queue (incident_id, run_id) VALUES (:i, :r)"),
            {"i": UUID(done), "r": f"incident-{done}"},
        )

    async def go() -> act.ReconciliationOut:
        client = await Client.connect(temporal_address, data_converter=act.DATA_CONVERTER)
        acts = ReconciliationActivities(app_engine, client)
        return await ActivityEnvironment().run(
            acts.reconcile_workflows, act.ReconciliationIn("t310")
        )

    out = asyncio.run(go())
    stalled = telemetry.REGISTRY.get_sample_value(
        "aisre_workflows_stalled", {"state": "investigating"}
    )
    assert out.mismatches >= 1 and stalled is not None and stalled >= 1
    with app_engine.connect() as conn:
        left = conn.execute(
            text("SELECT count(*) FROM investigation_queue WHERE incident_id = :i"),
            {"i": UUID(done)},
        ).scalar_one()
    assert left == 0


def test_t310_the_worker_creates_the_schedule_once(temporal_address):
    async def go() -> tuple[str, str]:
        client = await Client.connect(temporal_address, data_converter=act.DATA_CONVERTER)
        await act.ensure_reconciliation_schedule(client)
        await act.ensure_reconciliation_schedule(client)  # a second start changes nothing
        desc = await client.get_schedule_handle(act.RECONCILIATION_SCHEDULE).describe()
        return desc.schedule.action.workflow, desc.schedule.action.task_queue

    assert asyncio.run(go()) == ("ReconciliationWorkflow", act.INVESTIGATION_QUEUE)
