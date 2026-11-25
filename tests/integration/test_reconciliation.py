"""Artifact 13.5 T3.10, T3.11 (8.9, WR-29, RB-04; ST-40): the reconciliation jobs against the
Compose stack's Temporal and PostgreSQL, and the release reconciliation against the fake release
process in-process.

`reconcile_workflows` compares the record with every running IncidentWorkflow on the stack, so
other incidents may add mismatches; the checks here are on the two incidents the test makes:
one left `investigating` with no workflow, which counts in aisre_workflows_stalled, and one
`handed_over` with a queue row whose run has ended, which is removed. The schedule is the one the
worker creates.
"""

# ruff: noqa: F811 (pytest fixtures imported, then named as arguments)
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from conftest import add_catalogue
from sqlalchemy import text
from temporalio.client import Client
from temporalio.testing import ActivityEnvironment
from test_approvals import PUBLIC, awaiting  # noqa: F401 (fixtures)
from test_dispatch_ledger import (  # noqa: F401 (fixtures)
    approved,
    client,
    directory,
    dispatcher_engine,
    released,
)
from test_patch_review import drafted, review  # noqa: F401 (fixtures)

from aisre.record import uuid7
from aisre.record.execution import PgTrustedStore
from aisre.record.transitions import Transition, record_transition
from aisre.reliability import telemetry
from aisre.remediation.action import development_verify_keys
from aisre.remediation.dispatcher import Outcome
from aisre.remediation.dispatcher_process import DispatcherActivities
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


def relayed(app_engine, incident_id: str) -> None:
    """Marks a quiet incident's start as the relay leaves a delivered one: one attempt counted."""
    with app_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE outbox SET attempts = 1 WHERE incident_id = :i AND kind = 'start_workflow'"
            ),
            {"i": UUID(incident_id)},
        )


def reconcile(app_engine, temporal_address) -> act.ReconciliationOut:
    async def go() -> act.ReconciliationOut:
        client = await Client.connect(temporal_address, data_converter=act.DATA_CONVERTER)
        acts = ReconciliationActivities(app_engine, client)
        return await ActivityEnvironment().run(
            acts.reconcile_workflows, act.ReconciliationIn("withheld")
        )

    return asyncio.run(go())


def stalled_in(state: str) -> float:
    return telemetry.REGISTRY.get_sample_value("aisre_workflows_stalled", {"state": state}) or 0


def test_withheld_starts_are_not_stalls_and_a_relayed_one_is(
    app_engine, catalogue, quiet, temporal_address
):
    """M3 follow-up: quiet incidents (start withheld, no workflow) no longer raise
    AisreWorkflowStalled; the same incident with its start relayed and no run is one stall more.
    Other incidents on the stack may count too, so the check is the difference."""
    org, service, _ = catalogue
    other = uuid7()
    add_catalogue(app_engine, other, service)
    held = quiet(other, service)
    move(app_engine, held, "investigating")
    reconcile(app_engine, temporal_address)
    before = stalled_in("investigating")
    relayed(app_engine, held)
    reconcile(app_engine, temporal_address)
    assert stalled_in("investigating") == before + 1


def test_t310_a_run_gone_missing_is_counted_and_an_ended_runs_queue_row_removed(
    app_engine, catalogue, quiet, temporal_address
):
    org, service, _ = catalogue
    lost = quiet(org, service)
    relayed(app_engine, lost)  # as if the relay had started it, and the run then vanished
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


def test_t311_st40_a_change_no_attempt_carries_pages_security(
    app_engine,
    dispatcher_engine,
    approved,
    released,
    caplog,
):
    """The dispatcher's daily job, as aisre_dispatcher, over the fake release process: the
    applied fix is matched by its attempt; an out-of-band change is not, and sets
    aisre_release_changes_unmatched with an audit line."""
    service, incident_id, action_id, blobs = approved
    release, url = released
    store = PgTrustedStore(dispatcher_engine, blobs)
    acts = DispatcherActivities(
        dispatcher_engine,
        lambda s: client(url, s),
        store,
        development_verify_keys(PUBLIC),
        directory(service),
    )
    res = asyncio.run(acts.dispatcher().execute(str(action_id)))
    assert res.outcome == Outcome.APPLIED, res.reason
    since = (datetime.now(UTC) - timedelta(hours=25)).isoformat()

    def check() -> act.ReleaseReconciliationOut:
        return asyncio.run(
            ActivityEnvironment().run(
                acts.reconcile_releases, act.ReleaseReconciliationIn("t311", since)
            )
        )

    clean = check()
    assert clean.unmatched == [] and clean.changes >= 1
    assert telemetry.REGISTRY.get_sample_value("aisre_release_changes_unmatched") == 0
    release.out_of_band({"set": {"ORDER_TAX_RATE_BPS": "9.5"}})
    with caplog.at_level("WARNING", logger="aisre.audit"):
        found = check()
    assert len(found.unmatched) == 1 and " manual " in found.unmatched[0]
    assert telemetry.REGISTRY.get_sample_value("aisre_release_changes_unmatched") == 1
    (line,) = [r for r in caplog.records if "aisre.release.unmatched_change" in r.getMessage()]
    assert json.loads(line.getMessage())["deployment"] == "orders-api-production"
