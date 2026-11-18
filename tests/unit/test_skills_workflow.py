"""Artifact 9.1, 13.5 T1.18, offline: the coordinator drives the real skills in the workflow.

IncidentWorkflow on Temporal's time-skipping test server, with the real hypothesise, diagnose and
review_diagnosis (aisre.agent.skills) over the Model Gateway and the fake provider's scripts, the
reference incident's evidence as the collector gathers it, and in-memory stand-ins for the record
activities. The rest is aisre.workflow.scripted, as in the Compose worker: draft_patch answers
"no allowed fix" until M2. FR-02, FR-11. Against PostgreSQL and the stack: tests/integration.
"""

from __future__ import annotations

import asyncio
import uuid

from monitoring_scenarios import copy_reference
from skill_rig import collected, skills
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from test_scripted import TEST_SERVER, MemoryRecord

from aisre.agent.agent_contracts import SkillResult
from aisre.workflow import activities as act
from aisre.workflow import scripted
from aisre.workflow.workflow import IncidentWorkflow, IncidentWorkflowInput, LeadSignal


async def run_incident(inputs) -> tuple[MemoryRecord, object]:
    rec = MemoryRecord()
    model_skills, store, transport = skills(inputs)
    ids = [str(e.evidence_id) for e in inputs.evidence]

    @activity.defn(name="collect_evidence")
    async def collect_evidence(inp: act.EvidenceIn) -> SkillResult[act.EvidenceOut]:
        gaps = sum(e.kind.value == "gap" for e in inputs.evidence)
        return SkillResult(output=act.EvidenceOut(ids, gaps), usage=scripted.NO_USAGE)

    real = {"collect_evidence", "hypothesise", "diagnose", "review_diagnosis"}
    rest = [a for a in scripted.SCRIPTED_ACTIVITIES if a.__name__ not in real]
    async with await WorkflowEnvironment.start_time_skipping(
        test_server_existing_path=TEST_SERVER, data_converter=act.DATA_CONVERTER
    ) as env:
        async with Worker(
            env.client,
            task_queue=act.INVESTIGATION_QUEUE,
            workflows=[IncidentWorkflow],
            activities=rec.activities() + model_skills.all() + [collect_evidence] + rest,
        ):
            iid = str(inputs.context.incident_id)
            handle = await env.client.start_workflow(
                IncidentWorkflow.run,
                IncidentWorkflowInput(iid),
                id=f"incident-{iid}-{uuid.uuid4()}",
                task_queue=act.INVESTIGATION_QUEUE,
            )
            for _ in range(200):
                if rec.handovers:
                    break
                await env.sleep(1)
            await handle.signal(IncidentWorkflow.lead_decision, LeadSignal("close-1", "close", "x"))
            assert await handle.result() == "closed"
    return rec, store


def test_reference_incident_is_diagnosed_by_the_real_skills_then_handed_over():
    rec, store = asyncio.run(run_incident(collected()))
    assert [(f, t) for f, t, _ in rec.transitions] == [
        ("open", "investigating"),
        ("investigating", "diagnosed"),
        ("diagnosed", "handed_over"),
    ]
    (saved,) = store.diagnoses.values()
    assert saved["draft"].outcome == "established" and len(store.reviews) == 1
    assert rec.handovers == [f"no fix drafted: {scripted.NOT_DRAFTED}"]


def test_honesty_variant_ends_inconclusive_and_is_handed_over(tmp_path):
    root = copy_reference(tmp_path, without=("release.json", "config-diff.txt"))
    rec, store = asyncio.run(run_incident(collected(root)))
    assert [(f, t) for f, t, _ in rec.transitions] == [
        ("open", "investigating"),
        ("investigating", "inconclusive"),
        ("inconclusive", "handed_over"),
    ]
    assert rec.transitions[1][2] == "diagnoser could not establish a cause"
    (saved,) = store.diagnoses.values()
    assert saved["draft"].summary.startswith("Root cause not established")
