"""T4.2 (ADR-46; NFR-12, NFR-41): the whole-run limit on a real Temporal server, made short.

The limit is 24 hours (workflow.WORKFLOW_LIMIT); a run may be given a shorter one through
IncidentWorkflowInput.limit_seconds, which only tests set (the relay never does). Here it is
15 s, and evidence collection never finishes on its own: it heartbeats until cancelled. The
limit cancels it, the incident goes `investigating` -> `inconclusive` -> `handed_over` with the
limit's reason, and no activity is left running (NFR-12: nothing held after the stop).

Its own worker, in the integration namespace, with the real record activities and the scripts;
the incident is withheld from the Compose relay, as in test_workflow_m0.
"""

# ruff: noqa: F811 (pytest fixtures imported, then named as arguments)
from __future__ import annotations

import asyncio
import contextlib
import time

from temporalio import activity
from temporalio.client import Client
from temporalio.service import RPCError
from temporalio.worker import Worker
from test_workflow_m0 import (  # noqa: F401 (fixture)
    quiet_incident,
    status_path,
    test_namespace,
    timeline_rows,
    wait_until,
)

from aisre.agent.agent_contracts import SkillResult
from aisre.workflow import activities as act
from aisre.workflow import scripted
from aisre.workflow.record_activities import RecordActivities
from aisre.workflow.workflow import LIMIT_REASON, IncidentWorkflow, IncidentWorkflowInput

LIMIT_S = 15


def test_a_short_limit_cancels_the_running_step_and_hands_over(
    app_engine, catalogue, temporal_address, test_namespace
):
    org, service, _ = catalogue
    iid = quiet_incident(app_engine, org, service)
    real = RecordActivities(app_engine)
    running = {"started": 0, "cancelled": 0}

    @activity.defn(name="collect_evidence")
    async def collect_evidence(inp: act.EvidenceIn) -> SkillResult[act.EvidenceOut]:
        running["started"] += 1
        try:
            while True:  # a source that never answers; heartbeats so a cancel reaches it
                activity.heartbeat()
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            running["cancelled"] += 1
            raise

    scripts = [a for a in scripted.SCRIPTED_ACTIVITIES if a is not scripted.collect_evidence]

    async def go() -> float:
        client = await Client.connect(
            temporal_address, namespace=test_namespace, data_converter=act.DATA_CONVERTER
        )
        async with Worker(
            client,
            task_queue=act.INVESTIGATION_QUEUE,
            workflows=[IncidentWorkflow],
            activities=[
                real.load_incident,
                real.record_transition,
                real.hand_over,
                collect_evidence,
            ]
            + scripts,
        ):
            t0 = time.monotonic()
            handle = await client.start_workflow(
                IncidentWorkflow.run,
                IncidentWorkflowInput(str(iid), limit_seconds=LIMIT_S),
                id=f"incident-{iid}",
                task_queue=act.INVESTIGATION_QUEUE,
            )
            try:
                await wait_until(
                    lambda: any(
                        r.type == "aisre.incident.handed_over.v1"
                        for r in timeline_rows(app_engine, iid)
                    ),
                    f"incident {iid} to be handed over at the limit",
                )
                return time.monotonic() - t0
            finally:
                with contextlib.suppress(RPCError):  # it may have ended at the limit already
                    await handle.terminate("integration test done")

    took = asyncio.run(go())
    rows = timeline_rows(app_engine, iid)
    assert status_path(rows) == [
        ("open", "investigating"),
        ("investigating", "inconclusive"),
        ("inconclusive", "handed_over"),
    ]
    assert rows[-1].data["reason"] == LIMIT_REASON
    assert running == {"started": 1, "cancelled": 1}  # NFR-12: the step was stopped, not left
    assert LIMIT_S <= took < LIMIT_S + 30
