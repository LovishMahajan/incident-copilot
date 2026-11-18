"""Artifact 13.5 T0.11, offline: the scripted M0 run, and how the worker chooses its activities.

The workflow runs on Temporal's time-skipping test server with aisre.workflow.scripted's
activities and an in-memory stand-in for the three record activities (the real ones are tested
against PostgreSQL in tests/integration/test_workflow_m0.py).
"""

from __future__ import annotations

import os
import uuid

import pytest
from pydantic import ValidationError
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from aisre.config import Settings
from aisre.workflow import activities as act
from aisre.workflow import scripted, worker
from aisre.workflow.workflow import IncidentWorkflow, IncidentWorkflowInput, LeadSignal

TEST_SERVER = os.environ.get("TEMPORAL_TEST_SERVER")  # None: download, as test_workflow.py does


class MemoryRecord:
    def __init__(self) -> None:
        self.status, self.version = "open", 1
        self.transitions: list[tuple[str, str, str]] = []
        self.handovers: list[str] = []

    def activities(self):
        rec = self

        @activity.defn(name="load_incident")
        async def load_incident(inp: act.IncidentIn) -> act.IncidentSnapshot:
            return act.IncidentSnapshot(
                inp.incident_id, "orders-api", "production", rec.version, rec.status
            )

        @activity.defn(name="record_transition")
        async def record_transition(inp: act.TransitionIn) -> act.TransitionOut:
            assert (inp.from_state, inp.expected_version) == (rec.status, rec.version)
            rec.transitions.append((inp.from_state, inp.to_state, inp.reason))
            rec.status, rec.version = inp.to_state, rec.version + 1
            return act.TransitionOut(rec.version, len(rec.transitions) + 1)

        @activity.defn(name="hand_over")
        async def hand_over(inp: act.HandOverIn) -> None:
            rec.handovers.append(inp.reason)

        return [load_incident, record_transition, hand_over]


@pytest.mark.asyncio
async def test_scripted_skills_take_an_incident_to_handed_over():
    rec = MemoryRecord()
    async with await WorkflowEnvironment.start_time_skipping(
        test_server_existing_path=TEST_SERVER, data_converter=act.DATA_CONVERTER
    ) as env:
        async with Worker(
            env.client,
            task_queue=act.INVESTIGATION_QUEUE,
            workflows=[IncidentWorkflow],
            activities=rec.activities() + scripted.SCRIPTED_ACTIVITIES,
        ):
            iid = str(uuid.uuid4())
            handle = await env.client.start_workflow(
                IncidentWorkflow.run,
                IncidentWorkflowInput(iid),
                id=f"incident-{iid}",
                task_queue=act.INVESTIGATION_QUEUE,
            )
            for _ in range(200):
                if rec.handovers:
                    break
                await env.sleep(1)
            await handle.signal(IncidentWorkflow.lead_decision, LeadSignal("close-1", "close", "x"))
            assert await handle.result() == "closed"
    assert [(f, t) for f, t, _ in rec.transitions] == [
        ("open", "investigating"),
        ("investigating", "diagnosed"),
        ("diagnosed", "handed_over"),
    ]
    assert rec.handovers == [f"no fix drafted: {scripted.NOT_DRAFTED}"]


def test_scripted_results_are_deterministic():
    inp = act.SkillIn("i-1", "k")
    assert scripted.scripted_id("i-1", "x") == scripted.scripted_id("i-1", "x")
    assert scripted.scripted_id("i-1", "x") != scripted.scripted_id("i-2", "x")
    assert scripted._hypotheses(inp.incident_id) == scripted._hypotheses(inp.incident_id)


def names(acts) -> list[str]:
    return [worker.activity_name(a) for a in acts]


def test_worker_registers_real_record_activities_and_scripts_only_when_asked():
    engine = object()
    plain = worker.investigation_activities(Settings.from_env({}), engine)  # type: ignore[arg-type]
    with_scripts = worker.investigation_activities(
        Settings.from_env({"AISRE_SCRIPTED_SKILLS": "true"}),
        engine,  # type: ignore[arg-type]
    )
    every = sorted(a.__name__ for a in act.ALL_INVESTIGATION_ACTIVITIES)
    for chosen in (plain, with_scripts):
        assert sorted(names(chosen)) == every  # each name once, nothing for the dispatcher
        assert names(chosen)[:3] == ["load_incident", "record_transition", "hand_over"]
        assert all(getattr(a, "__self__", None) is not None for a in chosen[:3])  # bound: real
    assert act.collect_evidence in plain and act.collect_evidence not in with_scripts
    assert scripted.collect_evidence in with_scripts
    assert not set(names(with_scripts)) & {a.__name__ for a in act.ALL_DISPATCHER_ACTIVITIES}


def test_scripts_are_refused_in_production():
    with pytest.raises(ValidationError, match="never production"):
        Settings.from_env({"AISRE_ENV": "production", "AISRE_SCRIPTED_SKILLS": "true"})
    # And by the worker, should a Settings be built some other way.
    forced = Settings.model_construct(env="production", scripted_skills=True)
    with pytest.raises(RuntimeError, match="production"):
        worker.investigation_activities(forced, object())  # type: ignore[arg-type]


def test_demo_pacing_stays_below_the_heartbeat_timeout():
    assert scripted.scripted_activities(0) == scripted.SCRIPTED_ACTIVITIES
    with pytest.raises(ValueError):
        scripted.scripted_activities(scripted.MAX_STEP_SECONDS + 1)
    with pytest.raises(ValidationError):
        Settings.from_env({"AISRE_SCRIPTED_STEP_SECONDS": "11"})
