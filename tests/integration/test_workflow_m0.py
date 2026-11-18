"""Artifact 13.5 T0.11: the M0 workflow on the live stack, with the real record activities.

1. An alert posted through the API reaches `handed_over`, run by the Compose worker (which
   needs `AISRE_SCRIPTED_SKILLS=true`, as in .env.example), and its timeline lists every step
   in order, with the evidence the real collector stores when it is configured and, with the
   provider configured (T1.18), the hypotheses and reviewed diagnosis of the real skills.
2. A stale version is reloaded and retried; when it stays stale, the run hands over (ADR-69).
3. A repeated transition_id or handover_key changes nothing.

Tests 2 and 3 need a person changing the incident between the workflow's read and its write.
They run their own worker, with the real record activities and the scripts, in their own
Temporal namespace on the same server, so the Compose worker never picks up their tasks; their
incidents' start rows are marked delivered in the intake transaction, so the Compose relay
never starts them either.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from google.protobuf.duration_pb2 import Duration
from sqlalchemy import func, select, update
from temporalio import activity
from temporalio.api.workflowservice.v1 import RegisterNamespaceRequest
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker

from aisre.api.app import create_app
from aisre.api.auth import DEV_CALLER_HEADER, DEV_ORG_HEADER
from aisre.contracts import IncidentCreate
from aisre.record import create_incident, uuid7
from aisre.record.tables import incidents, outbox, timeline_events
from aisre.workflow import activities as act
from aisre.workflow import scripted
from aisre.workflow.record_activities import RecordActivities
from aisre.workflow.workflow import STALE_RETRIES, IncidentWorkflow, IncidentWorkflowInput

ALERT = json.loads(
    (Path(__file__).parents[1] / "fixtures/reference-incident/alert.json").read_text()
)
TEST_NAMESPACE = "aisre-integration"
WAIT_S = 90
HANDED_OVER_PATH = [
    "aisre.incident.created.v1",
    "aisre.incident.status_changed.v1",
    "aisre.incident.status_changed.v1",
    "aisre.incident.status_changed.v1",
    "aisre.incident.handed_over.v1",
]


@pytest.fixture(scope="session")
def test_namespace(temporal_address) -> str:
    async def register() -> None:
        client = await Client.connect(temporal_address)
        try:
            await client.workflow_service.register_namespace(
                RegisterNamespaceRequest(
                    namespace=TEST_NAMESPACE,
                    workflow_execution_retention_period=Duration(seconds=86400),
                )
            )
        except RPCError as e:
            if e.status != RPCStatusCode.ALREADY_EXISTS:
                raise

    asyncio.run(register())
    return TEST_NAMESPACE


def timeline_rows(engine, incident_id: UUID):
    with engine.connect() as conn:
        return conn.execute(
            select(timeline_events.c.seq, timeline_events.c.type, timeline_events.c.data)
            .where(timeline_events.c.incident_id == incident_id)
            .order_by(timeline_events.c.seq)
        ).all()


def status_path(rows) -> list[tuple[str, str]]:
    return [
        (r.data["from_status"], r.data["to_status"])
        for r in rows
        if r.type == "aisre.incident.status_changed.v1"
    ]


def incident_row(engine, incident_id: UUID):
    with engine.connect() as conn:
        return conn.execute(
            select(incidents.c.status, incidents.c.version).where(
                incidents.c.incident_id == incident_id
            )
        ).one()


def quiet_incident(engine, org, service) -> UUID:
    """An open incident whose start row is already marked delivered: no relay starts it."""
    body = IncidentCreate(
        service=service,
        environment="production",
        started_at=datetime.now(UTC),
        title="M0 workflow test",
        source="engineer",
    )
    with engine.begin() as conn:
        iid = create_incident(conn, org_id=org, caller_id="dev:test", body=body).incident_id
        conn.execute(
            update(outbox).where(outbox.c.incident_id == iid).values(delivered_at=func.now())
        )
    return iid


async def wait_until(check: Callable[[], bool], what: str) -> None:
    for _ in range(WAIT_S * 4):
        if await asyncio.to_thread(check):
            return
        await asyncio.sleep(0.25)
    raise AssertionError(f"timed out waiting for {what}")


# ---------------------------------------------------------------------------
# 1. Through the API and the Compose worker
# ---------------------------------------------------------------------------
def test_an_alert_posted_through_the_api_reaches_handed_over(
    app_engine, settings, add_service, temporal_address
):
    org = uuid7()
    add_service(org, "orders-api")
    headers = {
        DEV_CALLER_HEADER: "monitoring",
        DEV_ORG_HEADER: str(org),
        "Idempotency-Key": f"m0-run-{org.hex[-12:]}",
    }
    with TestClient(create_app(settings, app_engine)) as api:
        made = api.post("/v1/incidents", json=ALERT, headers=headers)
        assert made.status_code == 201, made.text
        iid = UUID(made.json()["incident_id"])

        def handed_over() -> bool:
            rows = timeline_rows(app_engine, iid)
            return bool(rows) and rows[-1].type == "aisre.incident.handed_over.v1"

        try:
            asyncio.run(wait_until(handed_over, f"incident {iid} to reach handed_over"))
        except AssertionError as e:
            raise AssertionError(
                f"{e}. Is the Compose worker running with AISRE_SCRIPTED_SKILLS=true, or with "
                "the provider settings of .env.example? "
                f"Timeline so far: {[r.type for r in timeline_rows(app_engine, iid)]}"
            ) from None
        page = api.get(made.json()["links"]["timeline"], headers=headers).json()
        view = api.get(made.json()["links"]["self"], headers=headers).json()

    # With AISRE_EVIDENCE_SOURCES set (compose.yaml), the real collector and retriever add one
    # evidence event per record, all while investigating; the status path is the same.
    every = page["items"]
    assert [e["sequence"] for e in every] == list(range(1, len(every) + 1))
    types = [e["type"] for e in every]
    evidence = [i for i, t in enumerate(types) if t.startswith("aisre.evidence.")]
    if evidence:
        assert types.index("aisre.incident.status_changed.v1") < evidence[0]
        assert evidence[-1] < len(types) - 3  # before diagnosed
    # With the provider configured (T1.18), the real skills add their hypotheses and the
    # reviewed diagnosis, also while investigating.
    reasoning = [
        i for i, t in enumerate(types) if t.startswith(("aisre.hypothesis.", "aisre.diagnosis."))
    ]
    if reasoning:
        assert types[reasoning[-1]] == "aisre.diagnosis.reviewed.v1"
        assert reasoning[-1] < len(types) - 3  # before diagnosed
    items = [
        e
        for e in every
        if not e["type"].startswith(("aisre.evidence.", "aisre.hypothesis.", "aisre.diagnosis."))
    ]
    assert [e["type"] for e in items] == HANDED_OVER_PATH
    page = {"items": items}
    changes = [(e["data"]["from_status"], e["data"]["to_status"]) for e in page["items"][1:4]]
    assert changes == [
        ("open", "investigating"),
        ("investigating", "diagnosed"),
        ("diagnosed", "handed_over"),
    ]
    assert {e["source"] for e in page["items"][1:]} == {"/aisre/worker/workflow"}
    assert page["items"][-1]["data"] == {
        "from_status": "diagnosed",
        "reason": f"no fix drafted: {scripted.NOT_DRAFTED}",
    }
    assert view["status"] == "handed_over" and view["version"] == 4

    async def close_out() -> None:
        client = await Client.connect(temporal_address, namespace=settings.temporal_namespace)
        handle = client.get_workflow_handle(f"incident-{iid}")
        desc = await handle.describe()
        assert desc.workflow_type == "IncidentWorkflow"
        assert desc.status == WorkflowExecutionStatus.RUNNING  # waits for a person to close it
        await handle.terminate("integration test done")

    asyncio.run(close_out())


# ---------------------------------------------------------------------------
# 2. Stale versions, on a worker of the test's own
# ---------------------------------------------------------------------------
def bump_version(engine, incident_id: str, n: int) -> None:
    """What a person does: change the incident (here its proposed severity), so version + 1."""
    with engine.begin() as conn:
        conn.execute(
            update(incidents)
            .where(incidents.c.incident_id == UUID(incident_id))
            .values(
                proposed_severity="sev2" if n % 2 else "sev3",
                version=incidents.c.version + 1,
            )
        )


def run_with_people_changing_it(
    engine, address: str, namespace: str, incident_id: UUID, into: str, times: int
) -> int:
    """Run the workflow to handed_over, changing the incident `times` times just before each
    write into `into`. Returns how many changes were made."""
    real = RecordActivities(engine)
    bumps = 0

    @activity.defn(name="record_transition")
    async def record_transition(inp: act.TransitionIn) -> act.TransitionOut:
        nonlocal bumps
        if inp.to_state == into and bumps < times:
            bumps += 1
            await asyncio.to_thread(bump_version, engine, inp.incident_id, bumps)
        return await real.record_transition(inp)

    async def go() -> None:
        client = await Client.connect(
            address, namespace=namespace, data_converter=act.DATA_CONVERTER
        )
        async with Worker(
            client,
            task_queue=act.INVESTIGATION_QUEUE,
            workflows=[IncidentWorkflow],
            activities=[real.load_incident, record_transition, real.hand_over]
            + scripted.SCRIPTED_ACTIVITIES,
        ):
            handle = await client.start_workflow(
                IncidentWorkflow.run,
                IncidentWorkflowInput(str(incident_id)),
                id=f"incident-{incident_id}",
                task_queue=act.INVESTIGATION_QUEUE,
            )
            try:
                await wait_until(
                    lambda: any(
                        r.type == "aisre.incident.handed_over.v1"
                        for r in timeline_rows(engine, incident_id)
                    ),
                    f"incident {incident_id} to be handed over",
                )
            finally:
                await handle.terminate("integration test done")

    asyncio.run(go())
    return bumps


def test_a_stale_version_is_reloaded_and_written_again(
    app_engine, catalogue, temporal_address, test_namespace
):
    org, service, _ = catalogue
    iid = quiet_incident(app_engine, org, service)
    bumps = run_with_people_changing_it(
        app_engine, temporal_address, test_namespace, iid, "diagnosed", times=1
    )
    rows = timeline_rows(app_engine, iid)
    assert bumps == 1
    assert status_path(rows) == [
        ("open", "investigating"),
        ("investigating", "diagnosed"),
        ("diagnosed", "handed_over"),
    ]
    assert incident_row(app_engine, iid) == ("handed_over", 1 + 3 + 1)  # 3 steps, 1 person


def test_a_version_that_stays_stale_hands_over(
    app_engine, catalogue, temporal_address, test_namespace
):
    org, service, _ = catalogue
    iid = quiet_incident(app_engine, org, service)
    bumps = run_with_people_changing_it(
        app_engine, temporal_address, test_namespace, iid, "diagnosed", times=100
    )
    rows = timeline_rows(app_engine, iid)
    assert bumps == STALE_RETRIES + 1  # the first write and its 5 reloads, all stale
    assert status_path(rows) == [
        ("open", "investigating"),
        ("investigating", "inconclusive"),
        ("inconclusive", "handed_over"),
    ]
    reason = "incident record kept changing: diagnosed not recorded"
    assert rows[-1].type == "aisre.incident.handed_over.v1"
    assert rows[-1].data == {"from_status": "inconclusive", "reason": reason}
    assert incident_row(app_engine, iid) == ("handed_over", 1 + 3 + bumps)


# ---------------------------------------------------------------------------
# 3. Idempotency of the record activities, called directly
# ---------------------------------------------------------------------------
def test_a_repeated_transition_id_is_a_no_op(app_engine, catalogue):
    org, service, _ = catalogue
    iid = quiet_incident(app_engine, org, service)
    record = RecordActivities(app_engine)
    step = act.TransitionIn(str(iid), f"{iid}:t1", "open", "investigating", 1, "slot granted")

    first = asyncio.run(record.record_transition(step))
    again = asyncio.run(record.record_transition(step))
    assert first == again == act.TransitionOut(new_version=2, event_seq=2)
    assert incident_row(app_engine, iid) == ("investigating", 2)
    assert status_path(timeline_rows(app_engine, iid)) == [("open", "investigating")]

    # A new step written with the old version is stale, and writes nothing.
    stale = act.TransitionIn(str(iid), f"{iid}:t2", "investigating", "diagnosed", 1, "late")
    with pytest.raises(act.StaleVersion):
        asyncio.run(record.record_transition(stale))
    assert incident_row(app_engine, iid) == ("investigating", 2)
    assert len(timeline_rows(app_engine, iid)) == 2

    # The hand-over event is written once per key.
    to_ho = act.TransitionIn(str(iid), f"{iid}:t3", "investigating", "handed_over", 2, "stop")
    asyncio.run(record.record_transition(to_ho))
    hand = act.HandOverIn(str(iid), "stop", f"{iid}:h4")
    asyncio.run(record.hand_over(hand))
    asyncio.run(record.hand_over(hand))
    rows = timeline_rows(app_engine, iid)
    assert [r.type for r in rows].count("aisre.incident.handed_over.v1") == 1
    assert rows[-1].data == {"from_status": "investigating", "reason": "stop"}
    with app_engine.connect() as conn:  # the chain is whole: no seq taken by a repeat
        assert conn.execute(select(func.verify_timeline_chain(iid))).all() == []


def test_load_incident_reads_by_id_alone(app_engine, catalogue):
    org, service, _ = catalogue
    iid = quiet_incident(app_engine, org, service)
    record = RecordActivities(app_engine)
    snap = asyncio.run(record.load_incident(act.IncidentIn(str(iid), "k")))
    assert snap == act.IncidentSnapshot(str(iid), service, "production", 1, "open", False)
    assert asyncio.run(record.load_incident(act.IncidentIn(str(uuid7()), "k"))) is None


def test_an_illegal_transition_is_refused_not_retried(app_engine, catalogue):
    org, service, _ = catalogue
    iid = quiet_incident(app_engine, org, service)
    record = RecordActivities(app_engine)
    illegal = act.TransitionIn(str(iid), f"{iid}:t1", "open", "diagnosed", 1, "skip ahead")
    with pytest.raises(act.InvalidTransition):
        asyncio.run(record.record_transition(illegal))
    assert incident_row(app_engine, iid) == ("open", 1)
