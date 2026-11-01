"""Tests for the incident workflow, run on Temporal's time-skipping test server.

Fake activities stand in for the platform modules; they record every call so each
scenario can check what happened, and what did not.
Run:  python -m pytest -q test_workflow.py
"""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

import activities as act
import os
TEST_SERVER = os.environ.get("TEMPORAL_TEST_SERVER")  # path to temporal-test-server; None = download

from workflow import (ApprovalSignal, CancelSignal, IncidentWorkflow, IncidentWorkflowInput,
                      LeadSignal)


class Fakes:
    """Configurable fake platform. `script` decides each activity's answer."""

    def __init__(self, **script):
        self.script = {"diagnosis": "established", "test": "passed", "dispatch": "applied",
                       "reconcile": "applied", "verify": "recovered", "dispatch_delay": 0, **script}
        self.calls: Counter = Counter()
        self.transitions: list[tuple[str, str, str | None]] = []
        self.version = 1

    def build(self):
        f = self

        @activity.defn(name="load_incident")
        async def load_incident(inp: act.IncidentIn) -> act.IncidentSnapshot:
            f.calls["load_incident"] += 1
            return act.IncidentSnapshot(inp.incident_id, "checkout", "production", f.version, "open")

        @activity.defn(name="record_transition")
        async def record_transition(inp: act.TransitionIn) -> act.TransitionOut:
            f.calls["record_transition"] += 1
            assert inp.expected_version == f.version, "stale version"
            f.version += 1
            f.transitions.append((inp.from_state, inp.to_state, inp.action_to))
            return act.TransitionOut(f.version, len(f.transitions))

        @activity.defn(name="notify")
        async def notify(inp: act.NotifyIn) -> None:
            f.calls["notify:" + inp.kind] += 1

        @activity.defn(name="collect_evidence")
        async def collect_evidence(inp: act.IncidentIn) -> act.EvidenceOut:
            f.calls["collect_evidence"] += 1
            return act.EvidenceOut(["e1", "e2"], 0)

        @activity.defn(name="retrieve_knowledge")
        async def retrieve_knowledge(inp: act.IncidentIn) -> list[str]:
            return ["runbook-1"]

        @activity.defn(name="diagnose")
        async def diagnose(inp: act.IncidentIn) -> act.DiagnosisOut:
            f.calls["diagnose"] += 1
            out = f.script["diagnosis"]
            return act.DiagnosisOut("d1", out, "config_revert" if out == "established" else None,
                                    None if out == "established" else "evidence too thin")

        @activity.defn(name="review_diagnosis")
        async def review_diagnosis(inp: act.IncidentIn) -> act.ReviewOut:
            return act.ReviewOut(True, "d1", "ok")

        @activity.defn(name="draft_patch")
        async def draft_patch(inp: act.IncidentIn) -> act.PatchOut:
            return act.PatchOut("a1", "f" * 64)

        @activity.defn(name="run_sandbox_test")
        async def run_sandbox_test(inp: act.TestIn) -> act.TestOut:
            f.calls["run_sandbox_test"] += 1
            o = f.script["test"]
            return act.TestOut("t1", o != "not_reproduced", o)

        @activity.defn(name="request_approval")
        async def request_approval(inp: act.ApprovalRequestIn) -> act.ApprovalRequestOut:
            f.calls["request_approval"] += 1
            return act.ApprovalRequestOut("r1")

        @activity.defn(name="escalate_approval")
        async def escalate_approval(inp: act.ApprovalRequestIn) -> None:
            f.calls["escalate_approval"] += 1

        @activity.defn(name="close_approval_request")
        async def close_approval_request(inp: act.ApprovalRequestIn) -> None:
            f.calls["close_approval_request"] += 1

        @activity.defn(name="dispatch_change")
        async def dispatch_change(inp: act.DispatchIn) -> act.DispatchOut:
            f.calls["dispatch_undo" if inp.is_undo else "dispatch_change"] += 1
            if f.script["dispatch_delay"]:
                await asyncio.sleep(f.script["dispatch_delay"])
            return act.DispatchOut("x1", "applied" if inp.is_undo else f.script["dispatch"], "rel-1")

        @activity.defn(name="reconcile_execution")
        async def reconcile_execution(inp: act.DispatchIn) -> act.DispatchOut:
            f.calls["reconcile_execution"] += 1
            return act.DispatchOut("x1", f.script["reconcile"], "rel-1")

        @activity.defn(name="verify_recovery")
        async def verify_recovery(inp: act.VerifyIn) -> act.VerifyOut:
            f.calls["verify_recovery"] += 1
            return act.VerifyOut("v1", f.script["verify"])

        @activity.defn(name="hand_over")
        async def hand_over(inp: act.HandOverIn) -> None:
            f.calls["hand_over"] += 1

        inv = [load_incident, record_transition, notify, collect_evidence, retrieve_knowledge, diagnose,
               review_diagnosis, draft_patch, run_sandbox_test, request_approval, escalate_approval,
               close_approval_request, verify_recovery, hand_over]
        return inv, [dispatch_change, reconcile_execution]


async def run_case(fakes: Fakes, driver):
    async with await WorkflowEnvironment.start_time_skipping(test_server_existing_path=TEST_SERVER) as env:
        inv, disp = fakes.build()
        async with Worker(env.client, task_queue=act.INVESTIGATION_QUEUE,
                          workflows=[IncidentWorkflow], activities=inv), \
                   Worker(env.client, task_queue=act.DISPATCHER_QUEUE, activities=disp):
            iid = str(uuid.uuid4())
            handle = await env.client.start_workflow(
                IncidentWorkflow.run, IncidentWorkflowInput(iid),
                id=f"incident-{iid}", task_queue=act.INVESTIGATION_QUEUE)
            await driver(env, handle)
            return await handle.result()


async def wait_state(handle, state, tries=200):
    for _ in range(tries):
        if (await handle.query(IncidentWorkflow.current_state))["incident"] == state:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"never reached {state}")


def approve(handle, aid="ap-1", decision="approved"):
    return handle.signal(IncidentWorkflow.approval_decided,
                         ApprovalSignal(aid, "a1", decision, "f" * 64, "priya"))


def close(handle):
    return handle.signal(IncidentWorkflow.lead_decision, LeadSignal("close-1", "close", "lead"))


@pytest.mark.asyncio
async def test_happy_path_reaches_closed():
    f = Fakes()

    async def drive(env, h):
        await wait_state(h, "awaiting_approval")
        await approve(h)
        await approve(h)                     # duplicate delivery from the outbox is ignored
        await wait_state(h, "recovered")
        await close(h)

    assert await run_case(f, drive) == "closed"
    assert f.calls["dispatch_change"] == 1
    assert [t[1] for t in f.transitions] == ["investigating", "diagnosed", "testing", "awaiting_approval",
                                            "executing", "executing", "verifying", "recovered", "closed"]


@pytest.mark.asyncio
async def test_inconclusive_hands_over_without_patch():
    f = Fakes(diagnosis="inconclusive")

    async def drive(env, h):
        await wait_state(h, "handed_over")
        await close(h)

    assert await run_case(f, drive) == "closed"
    assert f.calls["run_sandbox_test"] == 0 and f.calls["dispatch_change"] == 0


@pytest.mark.asyncio
async def test_not_reproduced_never_requests_approval():
    f = Fakes(test="not_reproduced")

    async def drive(env, h):
        await wait_state(h, "handed_over")
        await close(h)

    await run_case(f, drive)
    assert f.calls["request_approval"] == 0


@pytest.mark.asyncio
async def test_unanswered_approval_escalates_then_expires():
    f = Fakes()

    async def drive(env, h):
        await wait_state(h, "awaiting_approval")
        await env.sleep(61 * 60)             # time-skipping: no real hour passes
        await wait_state(h, "handed_over")
        await close(h)

    await run_case(f, drive)
    assert f.calls["escalate_approval"] == 1 and f.calls["close_approval_request"] == 1
    assert f.calls["dispatch_change"] == 0
    assert ("awaiting_approval", "handed_over", "expired") in f.transitions


@pytest.mark.asyncio
async def test_rejection_hands_over():
    f = Fakes()

    async def drive(env, h):
        await wait_state(h, "awaiting_approval")
        await approve(h, decision="rejected")
        await wait_state(h, "handed_over")
        await close(h)

    await run_case(f, drive)
    assert f.calls["dispatch_change"] == 0


@pytest.mark.asyncio
async def test_unknown_outcome_is_reconciled_not_retried():
    f = Fakes(dispatch="unknown", reconcile="applied")

    async def drive(env, h):
        await wait_state(h, "awaiting_approval")
        await approve(h)
        await wait_state(h, "recovered")
        await close(h)

    await run_case(f, drive)
    assert f.calls["dispatch_change"] == 1 and f.calls["reconcile_execution"] == 1
    assert ("executing", "reconciling", "outcome_unknown") in f.transitions


@pytest.mark.asyncio
async def test_still_unknown_after_reconciliation_hands_over():
    f = Fakes(dispatch="unknown", reconcile="unknown")

    async def drive(env, h):
        await wait_state(h, "awaiting_approval")
        await approve(h)
        await wait_state(h, "handed_over")
        await close(h)

    await run_case(f, drive)
    assert f.calls["dispatch_change"] == 1 and f.calls["verify_recovery"] == 0


@pytest.mark.asyncio
async def test_failed_verification_undoes_and_hands_over():
    f = Fakes(verify="not_recovered")

    async def drive(env, h):
        await wait_state(h, "awaiting_approval")
        await approve(h)
        await wait_state(h, "handed_over")
        await close(h)

    await run_case(f, drive)
    assert f.calls["dispatch_undo"] == 1
    assert ("not_recovered", "handed_over", "undone") in f.transitions


@pytest.mark.asyncio
async def test_cancel_while_awaiting_approval_withdraws_action():
    f = Fakes()

    async def drive(env, h):
        await wait_state(h, "awaiting_approval")
        await h.signal(IncidentWorkflow.cancellation_requested, CancelSignal("c-1", "sam", "false alarm"))

    assert await run_case(f, drive) == "cancelled"
    assert f.calls["dispatch_change"] == 0 and f.calls["close_approval_request"] == 1
    assert ("awaiting_approval", "cancelled", "withdrawn") in f.transitions


@pytest.mark.asyncio
async def test_cancel_during_write_lets_write_finish_and_does_not_pretend_reversal():
    f = Fakes(dispatch_delay=2)

    async def drive(env, h):
        await wait_state(h, "awaiting_approval")
        await approve(h)
        await wait_state(h, "executing")
        await h.signal(IncidentWorkflow.cancellation_requested, CancelSignal("c-2", "sam", "stop"))
        await wait_state(h, "recovered")
        await close(h)

    result = await run_case(f, drive)
    assert result == "closed"                # not "cancelled": the change happened and was verified
    assert f.calls["dispatch_change"] == 1


@pytest.mark.asyncio
async def test_temporal_cancel_during_write_waits_for_outcome():
    f = Fakes(dispatch_delay=2)

    async def drive(env, h):
        await wait_state(h, "awaiting_approval")
        await approve(h)
        await wait_state(h, "executing")
        await h.cancel()
        await wait_state(h, "recovered")
        await close(h)

    try:
        result = await run_case(f, drive)
    except WorkflowFailureError:
        result = "failed"
    assert f.calls["dispatch_change"] == 1
    assert result in ("closed", "recovered")


@pytest.mark.asyncio
async def test_completed_history_replays_deterministically():
    """Replay strategy (8.8): every recorded history must replay against the current code.
    In CI, the same check runs over histories exported from production before each deploy."""
    from temporalio.worker import Replayer

    f = Fakes()
    async with await WorkflowEnvironment.start_time_skipping(test_server_existing_path=TEST_SERVER) as env:
        inv, disp = f.build()
        async with Worker(env.client, task_queue=act.INVESTIGATION_QUEUE,
                          workflows=[IncidentWorkflow], activities=inv), \
                   Worker(env.client, task_queue=act.DISPATCHER_QUEUE, activities=disp):
            iid = str(uuid.uuid4())
            h = await env.client.start_workflow(IncidentWorkflow.run, IncidentWorkflowInput(iid),
                                                id=f"incident-{iid}", task_queue=act.INVESTIGATION_QUEUE)
            await wait_state(h, "awaiting_approval")
            await approve(h)
            await wait_state(h, "recovered")
            await close(h)
            assert await h.result() == "closed"
            history = await h.fetch_history()
    await Replayer(workflows=[IncidentWorkflow]).replay_workflow(history)
