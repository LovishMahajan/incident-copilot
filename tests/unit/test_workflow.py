"""Tests for the incident workflow, run on Temporal's time-skipping test server.

Fake activities stand in for the platform modules; they record every call so each
scenario can check what happened, and what did not. The coordinator is the real Layer 9 one.
A few tests shorten a limit: those run without the sandbox, so the patch reaches the workflow.
Run:  uv run pytest -q tests/unit/test_workflow.py
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import re
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from aisre.workflow import activities as act

TEST_SERVER = os.environ.get(
    "TEMPORAL_TEST_SERVER"
)  # path to temporal-test-server; None = download

from aisre.agent.agent_contracts import (
    Claim,
    Confidence,
    CriticVerdict,
    DiagnosisDraft,
    DiagnosisResult,
    HypothesesOut,
    HypothesisDraft,
    KnowledgeOut,
    ReviewResult,
    SkillFailure,
    SkillResult,
    SkillUsage,
    evidence_request,
)
from aisre.agent.coordinator import Limits, Step
from aisre.agent.tool_registry import ToolGate
from aisre.remediation.dispatcher import DispatchResult, Outcome, ReleaseInProgress
from aisre.workflow import workflow as wf
from aisre.workflow.states import (
    ACTION_TRANSITIONS,
    INCIDENT_TRANSITIONS,
    ActionState,
    InvalidTransition,
    check_action,
)
from aisre.workflow.workflow import (
    ApprovalSignal,
    CancelSignal,
    IncidentWorkflow,
    IncidentWorkflowInput,
    LeadSignal,
    ReconciliationWorkflow,
)

EVIDENCE = uuid.UUID("11111111-1111-4111-8111-111111111111")
FIX_DIGEST = "d" * 64
ROLLBACK_DIGEST = "e" * 64
LOG_REQUEST = evidence_request(
    tool="get_logs", reason="check the error", params={"service": "checkout"}
)


def hypothesis(category="dependency", requests=()):
    return HypothesisDraft(
        statement="the cause",
        category=category,
        claims=[Claim(text="seen in logs", evidence_ids=[EVIDENCE])],
        confirm_test="confirm",
        refute_test="refute",
        evidence_needed=list(requests),
    )


HYPOTHESES = [hypothesis("deployment_config", [LOG_REQUEST]), hypothesis()]


def diagnosis(outcome, n):
    established = outcome == "established"
    draft = DiagnosisDraft(
        outcome=outcome,
        leading_index=0 if established else None,
        hypotheses=[hypothesis(), hypothesis()],
        summary="what happened",
        proposed_action="config_revert" if established else None,
    )
    return DiagnosisResult(
        diagnosis_id=uuid.uuid4(),
        draft=draft,
        fingerprint=f"{n:064x}",
        supported_ratio=0.5 + n / 10,
    )


def review(verdict):
    if verdict == "invalid":
        return ReviewResult()
    return ReviewResult(
        verdict=CriticVerdict(
            verdict=verdict,
            confidence_cap=Confidence.high,
            evidence_requests=[LOG_REQUEST] if verdict == "revise" else [],
        ),
        confidence=Confidence.high,
    )


class Fakes:
    """Configurable fake platform. `script` decides each activity's answer.

    raises: activity names, or notify:<kind>, that fail with no retry. skill_failures: activity
    name to (code, detail). delays: real seconds. sleeps: virtual seconds, once. test: one outcome, or
    a list with one per patch candidate. severity_confirmed_before: a person changes the incident, so
    its version moves on, just before the transition to that state."""

    def __init__(self, **script):
        self.script = {
            "diagnosis": "established",
            "critic": ["accept"],
            "test": "passed",
            "dispatch": "applied",
            "reconcile": "applied",
            "verify": "recovered",
            "rollback_dispatch": "applied",
            "rollback_reconcile": "applied",
            "rollback_verify": "verified",
            "conditional": True,
            "approvers_needed": 1,
            "refusal_reason": None,
            "no_intent": False,
            "status": "open",
            "remediation": True,
            "sandbox_available": True,
            "evidence_only": False,
            "hypotheses": HYPOTHESES,
            "usage": {},
            "skill_failures": {},
            "raises": set(),
            "delays": {},
            "sleeps": {},
            "blocks": set(),
            "lost_once": set(),
            "db_down_attempts": 0,
            "reconcile_in_progress": 0,
            "severity_confirmed_before": None,
            "void_approval": False,
            "watch": 0,
            "watch_flags": [],
            **script,
        }
        self.calls: Counter = Counter()
        self.steps: list[str] = []
        self.inputs: dict[str, list] = {}
        self.transitions: list[tuple[str, str, str | None]] = []
        self.records: list[act.TransitionIn] = []
        self.handovers: list[str] = []
        self.events: list[str] = []
        self.versions: Counter = Counter()  # per incident, on top of the starting version
        self.env = None
        self.queue: list[act.QueueRow] = []
        self.grants: list[str] = []

    @property
    def second(self):
        return [
            (r.second_action_id, r.second_action_from, r.second_action_to)
            for r in self.records
            if r.second_action_to
        ]

    async def enter(self, name, inp=None):
        """Common to every fake: count, remember, then fail, wait or block as scripted."""
        self.calls[name] += 1
        self.steps.append(name)
        self.inputs.setdefault(name, []).append(inp)
        if name in self.script["raises"]:
            raise ApplicationError(f"{name} exhausted", non_retryable=True)
        if name in self.script["lost_once"] and self.calls[name] == 1:
            raise ConnectionError("worker lost")
        if name in self.script["delays"]:
            await asyncio.sleep(self.script["delays"][name])
        if name in self.script["sleeps"]:
            await self.env.sleep(self.script["sleeps"].pop(name))
        if name in self.script["blocks"]:
            try:
                while True:
                    activity.heartbeat()
                    await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                await asyncio.sleep(0.3)  # destroying the sandbox takes a moment
                self.events.append(f"{name}:stopped")
                raise

    def skill(self, name, output, inp):
        failure = self.script["skill_failures"].get(name)
        usage = self.script["usage"].get(name, SkillUsage(model_calls=1))
        if failure:
            return SkillResult(
                failure=SkillFailure(skill=name, code=failure[0], detail=failure[1]), usage=usage
            )
        return SkillResult(output=output, usage=usage)

    def build(self):
        f = self

        @activity.defn(name="load_incident")
        async def load_incident(inp: act.IncidentIn) -> act.IncidentSnapshot:
            await f.enter("load_incident", inp)
            return act.IncidentSnapshot(
                inp.incident_id,
                "checkout",
                "production",
                1 + f.versions[inp.incident_id],
                f.script["status"],
                remediation_enabled=f.script["remediation"],
            )

        @activity.defn(name="acquire_investigation_slot")
        async def acquire_investigation_slot(inp: act.SlotIn) -> None:
            await f.enter("acquire_investigation_slot", inp)
            if "no_slot" in f.script["raises"]:
                raise act.NoSlot("queued")
            if f.queue:
                if not act.may_grant(inp.incident_id, f.queue):
                    raise act.NoSlot("queued")
                f.queue = [
                    dataclasses.replace(r, granted=r.granted or r.incident_id == inp.incident_id)
                    for r in f.queue
                ]
                f.grants.append(inp.incident_id)

        @activity.defn(name="read_spend_state")
        async def read_spend_state(inp: act.IncidentIn) -> act.SpendOut:
            await f.enter("read_spend_state", inp)
            return act.SpendOut(f.script["evidence_only"])

        @activity.defn(name="record_transition")
        async def record_transition(inp: act.TransitionIn) -> act.TransitionOut:
            f.calls["record_transition"] += 1
            f.calls["attempt:" + inp.to_state] += 1
            if (
                inp.to_state == "investigating"
                and f.calls["attempt:investigating"] <= f.script["db_down_attempts"]
            ):
                raise ConnectionError("database down")
            if f.script.get("refuse_transition_to") == inp.to_state:
                raise InvalidTransition("a conflicting writer got there first")
            await asyncio.sleep(f.script["delays"].get("record:" + inp.to_state, 0))
            if f.script["severity_confirmed_before"] == inp.to_state:
                f.script["severity_confirmed_before"] = None
                f.versions[inp.incident_id] += 1  # the API's write, in its own transaction
            if inp.expected_version != 1 + f.versions[inp.incident_id] or inp.to_state in (
                f.script.get("always_stale") or ()
            ):
                f.calls["stale"] += 1
                raise act.StaleVersion(f"expected {inp.expected_version}")
            f.versions[inp.incident_id] += 1
            f.transitions.append((inp.from_state, inp.to_state, inp.action_to))
            f.records.append(inp)
            if inp.to_state == "cancelled":
                f.events.append("cancelled")
            return act.TransitionOut(1 + f.versions[inp.incident_id], len(f.transitions))

        @activity.defn(name="notify")
        async def notify(inp: act.NotifyIn) -> None:
            await f.enter("notify:" + inp.kind, inp)

        @activity.defn(name="record_notification_failed")
        async def record_notification_failed(inp: act.NotifyIn) -> None:
            await f.enter("notification_failed", inp)

        @activity.defn(name="collect_evidence")
        async def collect_evidence(inp: act.EvidenceIn) -> SkillResult:
            await f.enter("collect_evidence", inp)
            n = f.calls["collect_evidence"]
            return f.skill("collect_evidence", act.EvidenceOut([f"e{n}a", f"e{n}b"], 0), inp)

        @activity.defn(name="retrieve_knowledge")
        async def retrieve_knowledge(inp: act.SkillIn) -> SkillResult:
            await f.enter("retrieve_knowledge", inp)
            return f.skill(
                "retrieve_knowledge",
                KnowledgeOut(evidence_ids=["k1"], gap_count=0, index_revision="r"),
                inp,
            )

        @activity.defn(name="hypothesise")
        async def hypothesise(inp: act.SkillIn) -> SkillResult:
            await f.enter("hypothesise", inp)
            return f.skill("hypothesise", HypothesesOut(hypotheses=f.script["hypotheses"]), inp)

        @activity.defn(name="diagnose")
        async def diagnose(inp: act.SkillIn) -> SkillResult:
            await f.enter("diagnose", inp)
            return f.skill("diagnose", diagnosis(f.script["diagnosis"], f.calls["diagnose"]), inp)

        @activity.defn(name="review_diagnosis")
        async def review_diagnosis(inp: act.SkillIn) -> SkillResult:
            await f.enter("review_diagnosis", inp)
            verdicts = f.script["critic"]
            return f.skill(
                "critique",
                review(verdicts[min(f.calls["review_diagnosis"], len(verdicts)) - 1]),
                inp,
            )

        @activity.defn(name="draft_patch")
        async def draft_patch(inp: act.SkillIn) -> SkillResult:
            await f.enter("draft_patch", inp)
            return f.skill(
                "draft_patch",
                act.PatchOut(
                    f"a{f.calls['draft_patch']}",
                    "f" * 64,
                    "config_revert",
                    900,
                    f.script["sandbox_available"],
                ),
                inp,
            )

        @activity.defn(name="run_sandbox_test")
        async def run_sandbox_test(inp: act.TestIn) -> act.TestOut:
            await f.enter("run_sandbox_test", inp)
            o = f.script["test"]
            if isinstance(o, list):
                o = o[min(f.calls["run_sandbox_test"], len(o)) - 1]
            return act.TestOut("t1", o != "not_reproduced", o, FIX_DIGEST, "development-runc")

        @activity.defn(name="open_draft_pull_request")
        async def open_draft_pull_request(inp: act.ReviewIn) -> act.ReviewOut:
            await f.enter("open_draft_pull_request", inp)
            return act.ReviewOut(f"orders-api#{f.calls['open_draft_pull_request']}", "u")

        @activity.defn(name="request_approval")
        async def request_approval(inp: act.ApprovalRequestIn) -> act.ApprovalRequestOut:
            await f.enter("request_approval", inp)
            if inp.action_id == "r1":
                return act.ApprovalRequestOut("r2", 1, conditional_approval=f.script["conditional"])
            return act.ApprovalRequestOut("r1", f.script["approvers_needed"], "r1", ROLLBACK_DIGEST)

        @activity.defn(name="escalate_approval")
        async def escalate_approval(inp: act.ApprovalRequestIn) -> None:
            await f.enter("escalate_approval", inp)

        @activity.defn(name="close_approval_request")
        async def close_approval_request(inp: act.ApprovalRequestIn) -> None:
            await f.enter("close_approval_request", inp)

        @activity.defn(name="dispatch_change")
        async def dispatch_change(inp: act.DispatchIn) -> act.DispatchOut:
            await f.enter("dispatch_change", inp)
            f.calls["dispatch:" + inp.action_id] += 1
            outcome = f.script["rollback_dispatch" if inp.action_id == "r1" else "dispatch"]
            return act.DispatchOut(
                "x1",
                outcome,
                "rel-1",
                f.script["refusal_reason"],
                outcome != "refused",
                void_approval=outcome == "refused" and f.script["void_approval"],
            )

        @activity.defn(name="reconcile_execution")
        async def reconcile_execution(inp: act.DispatchIn) -> act.DispatchOut:
            await f.enter("reconcile_execution", inp)
            if f.calls["reconcile_execution"] <= f.script["reconcile_in_progress"]:
                raise ReleaseInProgress(inp.action_id)
            if f.script["no_intent"]:
                return act.DispatchOut("", "not_applied", intent_recorded=False)
            return act.DispatchOut(
                "x1",
                f.script["rollback_reconcile" if inp.action_id == "r1" else "reconcile"],
                "rel-1",
            )

        @activity.defn(name="verify_recovery")
        async def verify_recovery(inp: act.VerifyIn) -> act.VerifyOut:
            await f.enter("verify_recovery", inp)
            verdict = f.script["rollback_verify" if inp.action_id == "r1" else "verify"]
            result = "verified" if verdict == "recovered" else verdict
            watch = f.script["watch"] if result == "verified" else 0
            return act.VerifyOut("v1", result, "2026-11-19T12:11:00Z", watch)

        @activity.defn(name="watch_after_change")
        async def watch_after_change(inp: act.WatchIn) -> act.WatchOut:
            await f.enter("watch_after_change", inp)
            pending = [x for x in f.script["watch_flags"] if x not in inp.flagged]
            if pending:
                return act.WatchOut(
                    [act.WatchFlagOut(pending[0], "alert_breach", pending[0])], False
                )
            return act.WatchOut([], True)

        @activity.defn(name="hand_over")
        async def hand_over(inp: act.HandOverIn) -> None:
            await f.enter("hand_over", inp)
            f.handovers.append(inp.reason)

        inv = [
            load_incident,
            acquire_investigation_slot,
            read_spend_state,
            record_transition,
            notify,
            record_notification_failed,
            collect_evidence,
            retrieve_knowledge,
            hypothesise,
            diagnose,
            review_diagnosis,
            draft_patch,
            run_sandbox_test,
            open_draft_pull_request,
            request_approval,
            escalate_approval,
            close_approval_request,
            verify_recovery,
            watch_after_change,
            hand_over,
        ]
        sandbox = next(a for a in inv if a is run_sandbox_test)
        return (
            [a for a in inv if a is not sandbox],
            [dispatch_change, reconcile_execution],
            [sandbox],
        )


def start_env():
    return WorkflowEnvironment.start_time_skipping(
        test_server_existing_path=TEST_SERVER, data_converter=act.DATA_CONVERTER
    )


def workers(env, fakes, unsandboxed=False):
    inv, disp, tests = fakes.build()
    fast_heartbeats = {
        "max_heartbeat_throttle_interval": timedelta(seconds=1),
        "default_heartbeat_throttle_interval": timedelta(seconds=1),
    }
    runner = {"workflow_runner": UnsandboxedWorkflowRunner()} if unsandboxed else {}
    return (
        Worker(
            env.client,
            task_queue=act.INVESTIGATION_QUEUE,
            workflows=[IncidentWorkflow],
            activities=inv,
            **fast_heartbeats,
            **runner,
        ),
        Worker(env.client, task_queue=act.DISPATCHER_QUEUE, activities=disp),
        Worker(env.client, task_queue=act.TEST_RUNNER_QUEUE, activities=tests, **fast_heartbeats),
    )


async def run_case(fakes: Fakes, driver, run=1, unsandboxed=False):
    async with await start_env() as env:
        fakes.env = env
        inv_worker, disp_worker, test_worker = workers(env, fakes, unsandboxed)
        async with inv_worker, disp_worker, test_worker:
            iid = str(uuid.uuid4())
            fakes.incident_id = iid
            handle = await env.client.start_workflow(
                IncidentWorkflow.run,
                IncidentWorkflowInput(iid, run),
                id=f"incident-{iid}" + (f"-r{run}" if run > 1 else ""),
                task_queue=act.INVESTIGATION_QUEUE,
            )
            await driver(env, handle)
            return await handle.result()


async def wait_state(handle, state, tries=200):
    for _ in range(tries):
        if (await handle.query(IncidentWorkflow.current_state))["incident"] == state:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"never reached {state}")


async def no_driver(env, h):
    return None


def approve(handle, did="ap-1", decision="approved", by="priya", action="a1", digest=FIX_DIGEST):
    return handle.signal(
        IncidentWorkflow.approval_decided, ApprovalSignal(did, action, decision, digest, by)
    )


def rollback_decision(handle, decision="approved"):
    return approve(handle, "ap-r", decision, "lee", "r1", ROLLBACK_DIGEST)


def close(handle):
    return handle.signal(IncidentWorkflow.lead_decision, LeadSignal("close-1", "close", "lead"))


def cancel(handle, rid="c-1"):
    return handle.signal(
        IncidentWorkflow.cancellation_requested, CancelSignal(rid, "sam", "false alarm")
    )


async def to_approval(h):
    await wait_state(h, "awaiting_approval")
    await approve(h)


async def hand_over_then_close(env, h):
    await wait_state(h, "handed_over")
    await close(h)


async def approve_then_close(env, h):
    await to_approval(h)
    await hand_over_then_close(env, h)


# ---------------------------------------------------------------------------
# State tables (ADR-46)
# ---------------------------------------------------------------------------
def schema_pairs(table):
    sql = (Path(__file__).parents[2] / "db/schema.sql").read_text()
    block = re.search(rf"INSERT INTO {table} .*?;", sql, re.S).group(0)
    return set(re.findall(r"\('(\w+)', '(\w+)'\)", block))


def state_pairs(table):
    return {(a.value, b.value) for a, targets in table.items() for b in targets}


def test_transition_tables_match_schema_exactly():
    assert state_pairs(INCIDENT_TRANSITIONS) == schema_pairs("incident_transitions")
    assert state_pairs(ACTION_TRANSITIONS) == schema_pairs("action_transitions")
    assert (
        len(state_pairs(INCIDENT_TRANSITIONS)) == 30 and len(state_pairs(ACTION_TRANSITIONS)) == 20
    )


def test_only_a_rollback_action_skips_testing():
    check_action(ActionState.PROPOSED, ActionState.AWAITING_APPROVAL, "rollback")
    with pytest.raises(InvalidTransition):
        check_action(ActionState.PROPOSED, ActionState.AWAITING_APPROVAL, "config_revert")


# ---------------------------------------------------------------------------
# The coordinator loop (ADR-52)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_each_coordinator_step_reaches_its_activity_and_a_revision_round_works():
    f = Fakes(critic=["revise", "accept"])

    async def drive(env, h):
        await wait_state(h, "awaiting_approval")
        await cancel(h)

    assert await run_case(f, drive) == "cancelled"
    skills = [
        s
        for s in f.steps
        if s
        in (
            "collect_evidence",
            "retrieve_knowledge",
            "hypothesise",
            "diagnose",
            "review_diagnosis",
            "draft_patch",
        )
    ]
    assert skills == [
        "collect_evidence",
        "retrieve_knowledge",
        "hypothesise",
        "collect_evidence",
        "diagnose",
        "review_diagnosis",
        "collect_evidence",
        "diagnose",
        "review_diagnosis",
        "draft_patch",
    ]
    baseline, targeted, revision = f.inputs["collect_evidence"]
    assert baseline.mode == "baseline" and not baseline.requests
    assert targeted.mode == "targeted" and targeted.requests[0]["tool"] == "get_logs"
    assert revision.mode == "targeted" and revision.requests[0]["reason"] == "check the error"
    # What Layer 8 keeps for itself reaches the next skill, and evidence IDs accumulate.
    first, second = f.inputs["diagnose"]
    assert first.hypotheses.hypotheses[0].statement == "the cause" and first.critic is None
    assert second.critic.verdict == "revise" and second.hypotheses is not None
    assert set(first.evidence_ids) < set(second.evidence_ids)
    # Every step has its own key under the run.
    keys = [i.idempotency_key for n in ("collect_evidence", "diagnose") for i in f.inputs[n]]
    assert len(set(keys)) == 5 and all(k.startswith(f"{f.incident_id}:s") for k in keys)


@pytest.mark.asyncio
async def test_critic_sees_the_diagnosis_but_not_the_hypotheses():
    f = Fakes(critic=["escalate"])
    await run_case(f, hand_over_then_close)
    seen = f.inputs["review_diagnosis"][0]
    assert (
        seen.diagnosis.outcome == "established" and seen.hypotheses is None and seen.critic is None
    )


@pytest.mark.asyncio
async def test_invalid_critic_output_runs_the_critique_again():
    f = Fakes(critic=["invalid", "accept"])

    async def drive(env, h):
        await wait_state(h, "awaiting_approval")
        await cancel(h)

    await run_case(f, drive)
    assert f.calls["review_diagnosis"] == 2 and f.calls["diagnose"] == 1


@pytest.mark.asyncio
async def test_summary_goes_out_after_first_hypotheses_marked_unreviewed():
    f = Fakes(diagnosis="inconclusive")
    await run_case(f, hand_over_then_close)
    assert (
        f.steps.index("hypothesise") < f.steps.index("notify:summary") < f.steps.index("diagnose")
    )
    assert f.inputs["notify:summary"][0].detail == "not yet reviewed"


@pytest.mark.parametrize("name", ["hypothesise", "diagnose", "review_diagnosis", "draft_patch"])
@pytest.mark.asyncio
async def test_model_step_reruns_once_for_a_lost_worker(name):
    f = Fakes(lost_once={name}, diagnosis="established")

    async def drive(env, h):
        await wait_state(h, "awaiting_approval")
        await cancel(h)

    await run_case(f, drive)
    assert f.calls[name] == 2


def test_model_steps_use_the_8_5_policy():
    assert wf.MODEL_RETRY.maximum_attempts == 2
    assert all(
        wf.STEP_PLANS[s].retry is wf.MODEL_RETRY
        for s in (Step.HYPOTHESISE, Step.DIAGNOSE, Step.CRITIQUE)
    )
    assert all(
        wf.STEP_PLANS[s].timeout_s == 300 for s in (Step.HYPOTHESISE, Step.DIAGNOSE, Step.CRITIQUE)
    )


@pytest.mark.asyncio
async def test_time_stop_goes_through_inconclusive(monkeypatch):
    monkeypatch.setattr(wf, "LIMITS", Limits(investigation=timedelta(0)))
    f = Fakes()
    assert await run_case(f, hand_over_then_close, unsandboxed=True) == "closed"
    assert [t[:2] for t in f.transitions] == [
        ("open", "investigating"),
        ("investigating", "inconclusive"),
        ("inconclusive", "handed_over"),
    ]
    assert (
        f.records[1].reason == "investigation time limit reached"
        and f.calls["collect_evidence"] == 0
    )


@pytest.mark.asyncio
async def test_budget_stop_goes_through_inconclusive():
    f = Fakes(
        usage={"hypothesise": SkillUsage(model_calls=1, cost_micros=Limits().max_cost_micros)}
    )
    await run_case(f, hand_over_then_close)
    assert ("investigating", "inconclusive", None) in f.transitions and f.calls["diagnose"] == 0
    assert f.records[1].reason == "incident spend limit reached"


@pytest.mark.parametrize(
    "code", ["budget_refused", "provider_unavailable", "invalid_output", "deadline_passed"]
)
@pytest.mark.asyncio
async def test_typed_skill_failure_goes_through_inconclusive(code):
    f = Fakes(skill_failures={"diagnose": (code, "it failed")})
    await run_case(f, hand_over_then_close)
    assert ("investigating", "inconclusive", None) in f.transitions
    assert f.records[1].reason == f"diagnose failed ({code}): it failed"
    assert f.calls["review_diagnosis"] == 0 and f.calls["draft_patch"] == 0


@pytest.mark.asyncio
async def test_only_a_security_stop_goes_direct():
    f = Fakes(skill_failures={"hypothesise": ("security_risk", "credential in evidence")})
    await run_case(f, hand_over_then_close)
    assert [t[:2] for t in f.transitions] == [
        ("open", "investigating"),
        ("investigating", "handed_over"),
    ]


@pytest.mark.asyncio
async def test_step_with_no_result_stops_as_deadline_passed(monkeypatch):
    plan = wf.STEP_PLANS[Step.RETRIEVE_KNOWLEDGE]
    monkeypatch.setitem(wf.STEP_PLANS, Step.RETRIEVE_KNOWLEDGE, plan._replace(timeout_s=1))
    f = Fakes(blocks={"retrieve_knowledge"})
    await run_case(f, hand_over_then_close, unsandboxed=True)
    assert "retrieve_knowledge failed (deadline_passed)" in f.records[1].reason
    assert ("investigating", "inconclusive", None) in f.transitions


@pytest.mark.asyncio
async def test_new_investigation_past_daily_limit_is_evidence_only():
    f = Fakes(evidence_only=True)
    await run_case(f, hand_over_then_close)
    assert (
        f.calls["collect_evidence"] == 1
        and f.calls["retrieve_knowledge"] == 1
        and f.calls["hypothesise"] == 0
    )
    assert f.records[1].reason == "evidence-only: daily spend limit reached"
    assert f.steps.index("read_spend_state") < f.steps.index("collect_evidence")


@pytest.mark.asyncio
async def test_unreadable_spend_ledger_fails_closed():
    f = Fakes(raises={"read_spend_state"})
    await run_case(f, hand_over_then_close)
    assert f.calls["hypothesise"] == 0


# ---------------------------------------------------------------------------
# Happy path and hand-overs
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_happy_path_reaches_closed():
    f = Fakes()

    async def drive(env, h):
        await wait_state(h, "awaiting_approval")
        await approve(h)
        await approve(h)  # duplicate delivery from the outbox is ignored
        await wait_state(h, "recovered")
        await close(h)

    assert await run_case(f, drive) == "closed"
    assert f.calls["dispatch_change"] == 1
    # `closed` is the API's write, and the dispatcher's action changes aren't here (C18, ADR-51).
    assert [t[1] for t in f.transitions] == [
        "investigating",
        "diagnosed",
        "testing",
        "awaiting_approval",
        "executing",
        "verifying",
        "recovered",
    ]
    assert ("verifying", "recovered", "verified") in f.transitions
    assert f.inputs["verify_recovery"][0].hold_seconds == 900  # the action's, not a constant


@pytest.mark.asyncio
async def test_inconclusive_hands_over_without_patch():
    f = Fakes(diagnosis="inconclusive")

    async def drive(env, h):
        await wait_state(h, "handed_over")
        await close(h)

    assert await run_case(f, drive) == "closed"
    assert f.calls["run_sandbox_test"] == 0 and f.calls["dispatch_change"] == 0
    assert ("inconclusive", "handed_over", None) in f.transitions


@pytest.mark.asyncio
async def test_not_reproduced_never_requests_approval():
    f = Fakes(test="not_reproduced")

    async def drive(env, h):
        await wait_state(h, "handed_over")
        await close(h)

    await run_case(f, drive)
    assert f.calls["request_approval"] == 0
    assert ("testing", "handed_over", "test_failed") in f.transitions


@pytest.mark.asyncio
async def test_release_a_hands_over_the_tested_recommendation():
    f = Fakes(remediation=False)
    await run_case(f, hand_over_then_close)
    assert ("testing", "handed_over", "withdrawn") in f.transitions
    assert f.records[-1].reason == (
        "release_a_recommendation: fix passed its sandbox test (development-runc);"
        " draft pull request orders-api#1"
    )
    assert f.calls["request_approval"] == 0 and f.calls["dispatch_change"] == 0
    (review,) = f.inputs["open_draft_pull_request"]
    assert (review.action_id, review.test_run_id, review.review_key) == ("a1", "t1", "a1:review:1")
    # The pull request comes after the test, and before the hand-over.
    order = [
        s for s in f.steps if s in ("run_sandbox_test", "open_draft_pull_request", "hand_over")
    ]
    assert order == ["run_sandbox_test", "open_draft_pull_request", "hand_over"]


@pytest.mark.asyncio
async def test_release_a_hands_over_even_when_the_pull_request_cannot_be_opened():
    f = Fakes(remediation=False, raises={"open_draft_pull_request"})
    await run_case(f, hand_over_then_close)
    assert ("testing", "handed_over", "withdrawn") in f.transitions
    assert f.records[-1].reason.endswith(
        "draft pull request could not be opened; the patch is on the action"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome", ["failed", "not_reproduced", "inconclusive", "timeout", "crashed"]
)
async def test_release_a_opens_no_pull_request_unless_the_fix_passed(outcome):
    f = Fakes(remediation=False, test=outcome)
    await run_case(f, hand_over_then_close)
    assert f.calls["open_draft_pull_request"] == 0
    assert ("testing", "handed_over", "test_failed") in f.transitions


@pytest.mark.asyncio
async def test_release_a_up_to_three_candidates_one_at_a_time_then_the_pull_request():
    f = Fakes(remediation=False, test=["failed", "failed", "passed"])
    await run_case(f, hand_over_then_close)
    assert f.calls["draft_patch"] == 3 and f.calls["run_sandbox_test"] == 3
    assert [i.candidate_no for i in f.inputs["draft_patch"]] == [1, 2, 3]
    assert [(i.action_id) for i in f.inputs["run_sandbox_test"]] == ["a1", "a2", "a3"]
    assert [s for s in f.steps if s in ("draft_patch", "run_sandbox_test")] == [
        "draft_patch", "run_sandbox_test"] * 3  # fmt: skip
    assert f.inputs["open_draft_pull_request"][0].action_id == "a3"
    assert ("testing", "handed_over", "withdrawn") in f.transitions


@pytest.mark.asyncio
async def test_not_reproduced_hands_over_saying_the_failure_did_not_reproduce():
    f = Fakes(remediation=False, test="not_reproduced")
    await run_case(f, hand_over_then_close)
    assert f.calls["draft_patch"] == 1  # not a failed fix: no second candidate
    assert f.records[-1].reason.startswith(
        "fix not tested: the failure did not reproduce at the patch's base revision"
        " (development-runc)"
    )


@pytest.mark.asyncio
async def test_release_a_without_a_sandbox_hands_over_the_draft():
    f = Fakes(remediation=False, sandbox_available=False)
    await run_case(f, hand_over_then_close)
    assert ("diagnosed", "handed_over", "withdrawn") in f.transitions and f.calls[
        "run_sandbox_test"
    ] == 0


@pytest.mark.asyncio
async def test_no_allowed_fix_hands_over_with_no_action():
    f = Fakes(skill_failures={"draft_patch": ("no_allowed_fix", "nothing allowed fits")})
    await run_case(f, hand_over_then_close)
    assert (
        f.transitions[-1] == ("diagnosed", "handed_over", None) and f.calls["run_sandbox_test"] == 0
    )


@pytest.mark.asyncio
async def test_reinvestigation_run_starts_from_handed_over_with_its_own_keys():
    f = Fakes(status="handed_over", diagnosis="inconclusive")
    await run_case(f, hand_over_then_close, run=2)
    assert f.transitions[0][:2] == ("handed_over", "investigating")
    run = f"{f.incident_id}-r2"
    assert f.records[0].transition_id == f"{run}:t1"
    assert f.inputs["load_incident"][0].idempotency_key == f"{run}:load"
    assert f.inputs["acquire_investigation_slot"][0].idempotency_key == f"{run}:slot"
    assert f.inputs["hand_over"][0].handover_key.startswith(f"{run}:h")
    assert f.inputs["notify:opened"][0].notification_id.startswith(f"{run}:n")


@pytest.mark.asyncio
async def test_reinvestigate_signal_ends_the_run_without_a_write():
    f = Fakes(diagnosis="inconclusive")

    async def drive(env, h):
        await wait_state(h, "handed_over")
        await h.signal(IncidentWorkflow.lead_decision, LeadSignal("re-1", "reinvestigate", "lead"))

    assert await run_case(f, drive) == "handed_over"
    assert f.transitions[-1][:2] == ("inconclusive", "handed_over")


@pytest.mark.parametrize("status", ["closed", "cancelled", "investigating"])
@pytest.mark.asyncio
async def test_run_refuses_to_start_from_any_other_state(status):
    f = Fakes(status=status)
    assert await run_case(f, no_driver) == status
    assert not f.transitions


# ---------------------------------------------------------------------------
# Approval (ADR-49)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_unanswered_approval_escalates_then_expires():
    f = Fakes()

    async def drive(env, h):
        await wait_state(h, "awaiting_approval")
        await env.sleep(61 * 60)  # time-skipping: no real hour passes
        await wait_state(h, "handed_over")
        await close(h)

    await run_case(f, drive)
    assert f.calls["escalate_approval"] == 1 and f.calls["close_approval_request"] == 1
    assert f.inputs["close_approval_request"][0].close_reason == "expired"
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
    assert f.second == [("r1", "proposed", "withdrawn")]  # no `failed` verdict, so no rollback


async def still_waiting(h):
    await asyncio.sleep(0.3)
    assert (await h.query(IncidentWorkflow.current_state))["incident"] == "awaiting_approval"


@pytest.mark.asyncio
async def test_second_approver_is_waited_for_and_must_be_another_person():
    f = Fakes(approvers_needed=2)

    async def drive(env, h):
        await wait_state(h, "awaiting_approval")
        await approve(h, "d1", by="priya")
        await approve(h, "d2", by="priya")  # the same person twice counts once
        await still_waiting(h)
        await approve(h, "d3", by="lee")
        await wait_state(h, "recovered")
        await close(h)

    await run_case(f, drive)
    assert f.calls["dispatch_change"] == 1


@pytest.mark.asyncio
async def test_first_approval_ages_out_and_the_wait_continues():
    f = Fakes(approvers_needed=2)

    async def drive(env, h):
        await wait_state(h, "awaiting_approval")
        await approve(h, "d1", by="priya")
        await env.sleep(16 * 60)
        await approve(h, "d2", by="lee")
        await still_waiting(h)  # priya's approval is 16 minutes old
        await approve(h, "d3", by="sam")
        await wait_state(h, "recovered")
        await close(h)

    await run_case(f, drive)
    assert f.calls["dispatch_change"] == 1


@pytest.mark.asyncio
async def test_rejection_after_one_approval_wins():
    f = Fakes(approvers_needed=2)

    async def drive(env, h):
        await wait_state(h, "awaiting_approval")
        await approve(h, "d1", by="priya")
        await approve(h, "d2", "rejected", by="lee")
        await hand_over_then_close(env, h)

    await run_case(f, drive)
    assert ("awaiting_approval", "handed_over", "rejected") in f.transitions and f.calls[
        "dispatch_change"
    ] == 0


@pytest.mark.asyncio
async def test_revocation_removes_that_persons_approval():
    f = Fakes(approvers_needed=2)

    async def drive(env, h):
        await wait_state(h, "awaiting_approval")
        await approve(h, "d1", by="priya")
        await approve(h, "d2", "revoked", by="priya")
        await approve(h, "d3", by="lee")
        await still_waiting(h)
        await approve(h, "d4", by="sam")
        await wait_state(h, "recovered")
        await close(h)

    await run_case(f, drive)


@pytest.mark.asyncio
async def test_approval_for_a_different_digest_or_action_is_ignored():
    f = Fakes()

    async def drive(env, h):
        await wait_state(h, "awaiting_approval")
        await approve(h, "d1", digest="0" * 64)
        await approve(h, "d2", action="other")
        await still_waiting(h)
        await approve(h, "d3")
        await wait_state(h, "recovered")
        await close(h)

    await run_case(f, drive)


@pytest.mark.asyncio
async def test_late_rejection_withdraws_the_approved_action():
    f = Fakes(delays={"record:executing": 0.5})

    async def drive(env, h):
        await to_approval(h)
        while not f.calls["attempt:executing"]:
            await asyncio.sleep(0.02)
        await approve(h, "d2", "rejected", by="lee")
        await hand_over_then_close(env, h)

    await run_case(f, drive)
    assert ("executing", "handed_over", "withdrawn") in f.transitions and f.calls[
        "dispatch_change"
    ] == 0


# ---------------------------------------------------------------------------
# Execution (ADR-47, ADR-51)
# ---------------------------------------------------------------------------
def test_dispatch_input_is_only_the_action_id():
    assert [x.name for x in dataclasses.fields(act.DispatchIn)] == ["action_id"]


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
    assert (
        "executing",
        "reconciling",
        None,
    ) in f.transitions  # the dispatcher writes outcome_unknown
    assert ("reconciling", "verifying", None) in f.transitions


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
    assert "do not retry" in f.handovers[0]


@pytest.mark.parametrize(
    "reason,action",
    [
        ("approval expired: past valid_until", "expired"),
        ("approval pending: revoked", "withdrawn"),
        ("target runs r9, action was approved for r8", "withdrawn"),
    ],
)
@pytest.mark.asyncio
async def test_refused_dispatch_maps_to_the_action_state(reason, action):
    f = Fakes(dispatch="refused", refusal_reason=reason)
    await run_case(f, approve_then_close)
    assert ("executing", "handed_over", action) in f.transitions
    assert f.calls["reconcile_execution"] == 0 and reason in f.handovers[0]


@pytest.mark.asyncio
async def test_dispatcher_answering_not_applied_hands_over_without_an_action_write():
    f = Fakes(dispatch="not_applied")
    await run_case(f, approve_then_close)
    assert ("executing", "handed_over", None) in f.transitions


@pytest.mark.asyncio
async def test_dispatcher_activity_failure_reconciles():
    f = Fakes(raises={"dispatch_change"})

    async def drive(env, h):
        await to_approval(h)
        await wait_state(h, "recovered")
        await close(h)

    await run_case(f, drive)
    assert ("executing", "reconciling", None) in f.transitions and (
        "verifying",
        "recovered",
        "verified",
    ) in f.transitions


@pytest.mark.asyncio
async def test_reconcile_finding_no_intent_withdraws_the_action():
    f = Fakes(raises={"dispatch_change"}, no_intent=True)
    await run_case(f, approve_then_close)
    assert ("reconciling", "handed_over", "withdrawn") in f.transitions and f.calls[
        "verify_recovery"
    ] == 0


@pytest.mark.asyncio
async def test_reconcile_failure_is_treated_as_still_unknown():
    f = Fakes(dispatch="unknown", raises={"reconcile_execution"})
    assert await run_case(f, approve_then_close) == "closed"
    assert ("reconciling", "handed_over", None) in f.transitions and "do not retry" in f.handovers[
        0
    ]


@pytest.mark.asyncio
async def test_release_in_progress_is_retried_by_read_retry():
    f = Fakes(dispatch="unknown", reconcile_in_progress=2)

    async def drive(env, h):
        await to_approval(h)
        await wait_state(h, "reconciling")
        await env.sleep(30)  # READ_RETRY's backoff is the wait between checks
        await wait_state(h, "recovered")
        await close(h)

    await run_case(f, drive)
    assert f.calls["reconcile_execution"] == 3


# ---------------------------------------------------------------------------
# Verification and rollback (ADR-48, ADR-66)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_inconclusive_verification_hands_over_without_rollback():
    f = Fakes(verify="inconclusive")
    await run_case(f, approve_then_close)
    assert ("verifying", "handed_over", None) in f.transitions
    assert f.calls["dispatch:r1"] == 0 and ("r1", "proposed", "withdrawn") in f.second


@pytest.mark.asyncio
async def test_verify_activity_failure_hands_over_without_rollback():
    f = Fakes(raises={"verify_recovery"})
    await run_case(f, approve_then_close)
    assert ("verifying", "handed_over", None) in f.transitions and f.calls["dispatch:r1"] == 0


@pytest.mark.asyncio
async def test_verified_fix_withdraws_its_unused_rollback():
    f = Fakes()

    async def drive(env, h):
        await to_approval(h)
        await wait_state(h, "recovered")
        await close(h)

    await run_case(f, drive)
    assert f.second == [("r1", "proposed", "withdrawn")]


@pytest.mark.asyncio
async def test_failed_verification_rolls_back_as_its_own_action_and_hands_over():
    f = Fakes(verify="failed")
    await run_case(f, approve_then_close)
    assert f.calls["dispatch:a1"] == 1 and f.calls["dispatch:r1"] == 1  # each action its own key
    assert f.second == [
        ("r1", "proposed", "awaiting_approval"),
        ("r1", "awaiting_approval", "approved"),
        ("r1", "executed", "verified"),
    ]
    assert ("not_recovered", "not_recovered", "undone") in f.transitions  # the parent
    assert [i.action_id for i in f.inputs["verify_recovery"]] == ["a1", "r1"]
    assert "verifying" not in [
        t[1] for t in f.transitions[f.transitions.index(("verifying", "not_recovered", None)) :]
    ]
    assert f.transitions[-1][:2] == ("not_recovered", "handed_over")
    assert "check verified" in f.handovers[0]
    assert f.inputs["notify:handed_over"][0].audience == wf.ROLLBACK_AUDIENCE


@pytest.mark.asyncio
async def test_t317_the_watch_after_a_verified_fix_flags_to_people_and_rolls_nothing_back():
    """11.12: each flag is a notification to on-call and the incident lead, with its detail,
    linked to the fix; the action stays verified, and no rollback is asked for or dispatched."""
    f = Fakes(watch=120, watch_flags=["alert_breach:http_5xx_rate", "new_error_group:x"])

    async def driver(env, h):
        await to_approval(h)
        await wait_state(h, "recovered")
        for _ in range(200):
            if f.calls["watch_after_change"] == 3:
                break
            await asyncio.sleep(0.05)
        await close(h)

    assert await run_case(f, driver) == "closed"
    flags = [i for i in f.inputs["notify:watch_flagged"]]
    assert [n.detail for n in flags] == ["alert_breach:http_5xx_rate", "new_error_group:x"]
    assert all(n.audience == ["on_call", "incident_lead"] for n in flags)
    watched = f.inputs["watch_after_change"]
    assert [w.action_id for w in watched] == ["a1"] * 3 and watched[-1].flagged == [
        "alert_breach:http_5xx_rate",
        "new_error_group:x",
    ]
    assert watched[0].start == "2026-11-19T12:11:00Z" and watched[0].end.startswith(
        "2026-11-19T12:13:00"
    )
    assert f.calls["dispatch:r1"] == 0 and f.calls["request_approval"] == 1
    assert ("verifying", "recovered", "verified") in f.transitions
    assert f.transitions[-1][1] == "recovered"  # the watch writes no state


@pytest.mark.asyncio
async def test_t317_no_watch_without_a_verified_fix_and_a_failed_watch_ends_quietly():
    f = Fakes(verify="failed", watch=120)
    assert await run_case(f, approve_then_close) == "closed"
    assert f.calls["watch_after_change"] == 0
    f = Fakes(watch=120, raises={"watch_after_change"})

    async def driver(env, h):
        await to_approval(h)
        await wait_state(h, "recovered")
        await close(h)

    assert await run_case(f, driver) == "closed" and f.calls["watch_after_change"] == 1


@pytest.mark.asyncio
async def test_t317_closing_the_incident_ends_a_running_watch():
    """The incident lead accepted recovery: the watch stops, so no closed incident keeps a live
    run (8.9)."""
    f = Fakes(watch=3600, blocks={"watch_after_change"})

    async def driver(env, h):
        await to_approval(h)
        await wait_state(h, "recovered")
        for _ in range(200):
            if f.calls["watch_after_change"]:
                break
            await asyncio.sleep(0.05)
        await close(h)

    assert await run_case(f, driver) == "closed"
    assert "watch_after_change:stopped" in f.events


@pytest.mark.asyncio
async def test_rollback_check_triggers_nothing():
    f = Fakes(verify="failed", rollback_verify="failed")
    await run_case(f, approve_then_close)
    assert f.calls["dispatch:r1"] == 1 and "check failed" in f.handovers[0]
    assert ("r1", "executed", "verified") not in f.second


@pytest.mark.asyncio
async def test_rollback_without_conditional_approval_takes_a_fresh_one():
    f = Fakes(verify="failed", conditional=False)

    async def drive(env, h):
        await to_approval(h)
        await wait_state(h, "not_recovered")
        await asyncio.sleep(0.2)
        assert f.calls["dispatch:r1"] == 0
        await rollback_decision(h)
        await hand_over_then_close(env, h)

    await run_case(f, drive)
    assert f.calls["dispatch:r1"] == 1 and f.calls["notify:approval_needed"] == 2


@pytest.mark.asyncio
async def test_fresh_rollback_approval_escalates_then_expires():
    f = Fakes(verify="failed", conditional=False)

    async def drive(env, h):
        await to_approval(h)
        await wait_state(h, "not_recovered")
        await env.sleep(61 * 60)
        await hand_over_then_close(env, h)

    await run_case(f, drive)
    assert (
        f.calls["dispatch:r1"] == 0
        and f.calls["escalate_approval"] == 1
        and f.calls["close_approval_request"] == 1
    )
    assert ("r1", "awaiting_approval", "expired") in f.second


@pytest.mark.asyncio
async def test_rejected_rollback_hands_over():
    f = Fakes(verify="failed", conditional=False)

    async def drive(env, h):
        await to_approval(h)
        await wait_state(h, "not_recovered")
        await rollback_decision(h, "rejected")
        await hand_over_then_close(env, h)

    await run_case(f, drive)
    assert f.calls["dispatch:r1"] == 0 and ("r1", "awaiting_approval", "rejected") in f.second


@pytest.mark.asyncio
async def test_unknown_rollback_goes_through_reconciling_and_never_verifying():
    f = Fakes(verify="failed", rollback_dispatch="unknown", rollback_reconcile="unknown")
    await run_case(f, approve_then_close)
    assert ("not_recovered", "reconciling", None) in f.transitions
    assert f.transitions[-1][:2] == ("reconciling", "handed_over")
    assert ("reconciling", "verifying", None) not in f.transitions
    assert [i.action_id for i in f.inputs["verify_recovery"]] == ["a1"]


@pytest.mark.asyncio
async def test_rollback_found_applied_is_checked_while_reconciling():
    f = Fakes(verify="failed", rollback_dispatch="unknown", rollback_reconcile="applied")
    await run_case(f, approve_then_close)
    assert ("reconciling", "reconciling", "undone") in f.transitions
    assert ("reconciling", "verifying", None) not in f.transitions
    assert f.inputs["verify_recovery"][1].action_id == "r1" and f.transitions[-1][:2] == (
        "reconciling",
        "handed_over",
    )


@pytest.mark.asyncio
async def test_refused_rollback_hands_over_with_its_own_state():
    f = Fakes(
        verify="failed",
        rollback_dispatch="refused",
        refusal_reason="approval expired: past valid_until",
    )
    await run_case(f, approve_then_close)
    assert ("r1", "approved", "expired") in f.second and f.calls["verify_recovery"] == 1


@pytest.mark.parametrize("via_signal", [True, False])
@pytest.mark.asyncio
async def test_cancel_during_rollback_wait_never_skips_the_rollback(via_signal):
    """ADR-69. Until then a cancel here withdrew the rollback, and the failed fix stayed in production."""
    f = Fakes(verify="failed", conditional=False)

    async def drive(env, h):
        await to_approval(h)
        await wait_state(h, "not_recovered")
        while not f.calls["notify:approval_needed"] == 2:
            await asyncio.sleep(0.05)
        await (cancel(h) if via_signal else h.cancel())
        await asyncio.sleep(0.5)
        assert (await h.query(IncidentWorkflow.current_state))[
            "incident"
        ] == "not_recovered"  # still waiting
        await rollback_decision(h)
        await hand_over_then_close(env, h)

    assert await run_case(f, drive) == "closed"  # never `cancelled`: the fix was applied
    assert ("r1", "awaiting_approval", "withdrawn") not in f.second
    assert ("r1", "awaiting_approval", "approved") in f.second and f.calls["dispatch:r1"] == 1
    assert ("not_recovered", "not_recovered", "undone") in f.transitions


@pytest.mark.asyncio
async def test_cancel_while_the_fix_is_verified_takes_effect_after_the_rollback():
    f = Fakes(verify="failed", conditional=False, delays={"verify_recovery": 1})

    async def drive(env, h):
        await to_approval(h)
        await wait_state(h, "verifying")
        await cancel(h)
        await wait_state(h, "not_recovered")
        await rollback_decision(h)
        await hand_over_then_close(env, h)

    assert await run_case(f, drive) == "closed"
    assert f.calls["dispatch:r1"] == 1 and "cancelled" not in [t[1] for t in f.transitions]


@pytest.mark.asyncio
async def test_a_cancelled_rollback_wait_still_expires_on_its_own_clock():
    f = Fakes(verify="failed", conditional=False)

    async def drive(env, h):
        await to_approval(h)
        await wait_state(h, "not_recovered")
        await cancel(h)
        await env.sleep(61 * 60)
        await hand_over_then_close(env, h)

    await run_case(f, drive)
    assert ("r1", "awaiting_approval", "expired") in f.second and f.calls["dispatch:r1"] == 0
    assert f.inputs["close_approval_request"][-1].close_reason == "expired"


# ---------------------------------------------------------------------------
# ADR-69: the incident version, patch candidates, the stop, the voided approval
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_severity_confirmation_mid_run_is_reloaded_not_fatal():
    """The API confirms severity in its own transaction, which moves the incident's version on."""
    f = Fakes(severity_confirmed_before="diagnosed")

    async def drive(env, h):
        await to_approval(h)
        await wait_state(h, "recovered")
        await close(h)

    assert await run_case(f, drive) == "closed"
    assert (
        f.calls["stale"] == 1
        and f.calls["load_incident"] == 2
        and f.calls["attempt:diagnosed"] == 2
    )
    diagnosed = [r for r in f.records if r.to_state == "diagnosed"]
    assert (
        len(diagnosed) == 1 and diagnosed[0].expected_version == 3
    )  # open 1, investigating 2, the person's 3


@pytest.mark.asyncio
async def test_a_record_that_stays_stale_is_handed_over_not_retried_forever():
    f = Fakes(always_stale=("diagnosed",))
    assert await run_case(f, hand_over_then_close) == "closed"
    assert f.calls["attempt:diagnosed"] == wf.STALE_RETRIES + 1
    assert [t[:2] for t in f.transitions][-2:] == [
        ("investigating", "inconclusive"),
        ("inconclusive", "handed_over"),
    ]
    assert f.handovers == ["incident record kept changing: diagnosed not recorded"]


@pytest.mark.asyncio
async def test_a_hand_over_that_stays_stale_fails_the_run():
    f = Fakes(always_stale=("diagnosed", "inconclusive"))
    with pytest.raises(WorkflowFailureError):
        await run_case(f, no_driver)


def test_a_stale_version_is_its_own_non_retryable_error():
    assert "StaleVersion" in wf.DB_RETRY.non_retryable_error_types and not issubclass(
        act.StaleVersion, InvalidTransition
    )


@pytest.mark.asyncio
async def test_a_failed_test_drafts_the_next_candidate_as_its_own_action():
    f = Fakes(test=["failed", "passed"])

    async def drive(env, h):
        await wait_state(h, "awaiting_approval")
        await approve(h, action="a2")
        await wait_state(h, "recovered")
        await close(h)

    assert await run_case(f, drive) == "closed"
    assert [i.candidate_no for i in f.inputs["draft_patch"]] == [1, 2]
    assert [i.action_id for i in f.inputs["run_sandbox_test"]] == ["a1", "a2"]
    actions = [(r.action_id, r.action_from, r.action_to) for r in f.records if r.action_to]
    assert actions[:4] == [
        ("a1", "proposed", "testing"),
        ("a1", "testing", "test_failed"),
        ("a2", "proposed", "testing"),
        ("a2", "testing", "awaiting_approval"),
    ]
    assert [t[:2] for t in f.transitions].count(
        ("diagnosed", "testing")
    ) == 1  # the incident tests once
    assert (
        f.inputs["request_approval"][0].action_id == "a2"
        and f.calls["dispatch:a2"] == 1
        and f.calls["dispatch:a1"] == 0
    )


@pytest.mark.asyncio
async def test_three_failed_candidates_end_the_attempt():
    f = Fakes(test="failed")
    assert await run_case(f, hand_over_then_close) == "closed"
    assert [i.candidate_no for i in f.inputs["draft_patch"]] == [1, 2, 3] and f.calls[
        "run_sandbox_test"
    ] == 3
    assert act.MAX_PATCH_CANDIDATES == 3 and f.calls["request_approval"] == 0
    assert (
        f.transitions[-1] == ("testing", "handed_over", "test_failed")
        and f.records[-1].action_id == "a3"
    )
    assert f.handovers == ["fix not proven: failed (development-runc)"]


@pytest.mark.parametrize("outcome", ["not_reproduced", "inconclusive", "crashed", "timeout"])
@pytest.mark.asyncio
async def test_only_a_fix_that_ran_and_failed_earns_another_candidate(outcome):
    f = Fakes(test=outcome)
    await run_case(f, hand_over_then_close)
    expected = (
        "fix not tested: the failure did not reproduce at the patch's base revision"
        " (development-runc); a person checks the diagnosis"
        if outcome == "not_reproduced"
        else f"fix not proven: {outcome} (development-runc)"
    )
    assert f.calls["draft_patch"] == 1 and f.handovers == [expected]


@pytest.mark.asyncio
async def test_no_second_candidate_hands_over_with_the_first_marked_failed():
    f = Fakes(test="failed")
    real = f.skill

    def skill(name, output, inp):
        if name == "draft_patch" and inp.candidate_no == 2:
            return SkillResult(
                failure=SkillFailure(
                    skill=name, code="no_allowed_fix", detail="nothing else on the list"
                ),
                usage=SkillUsage(model_calls=1),
            )
        return real(name, output, inp)

    f.skill = skill
    await run_case(f, hand_over_then_close)
    assert (
        f.calls["run_sandbox_test"] == 1 and ("testing", "testing", "test_failed") in f.transitions
    )
    assert f.transitions[-1] == ("testing", "handed_over", None)
    assert f.handovers == ["fix not proven: failed; no candidate 2: nothing else on the list"]


@pytest.mark.asyncio
async def test_cancel_after_approval_before_intent_is_a_stop_not_a_revocation():
    """ADR-69: whoever may cancel may stop it. Nothing is dispatched, and no approval is withdrawn."""
    f = Fakes(delays={"record:executing": 1})

    async def drive(env, h):
        await to_approval(h)
        while not f.calls["attempt:executing"]:
            await asyncio.sleep(0.05)
        await cancel(h)
        await hand_over_then_close(env, h)

    assert await run_case(f, drive) == "closed"  # `executing` never goes to `cancelled` (ADR-46)
    assert (
        f.calls["dispatch_change"] == 0
        and ("executing", "handed_over", "withdrawn") in f.transitions
    )
    assert f.handovers == ["stopped by sam before intent: false alarm"]
    assert (
        f.calls["close_approval_request"] == 0
    )  # the request was decided; the approvals stand as recorded


@pytest.mark.asyncio
async def test_the_dispatchers_own_stop_refusal_withdraws_the_action():
    f = Fakes(
        dispatch="refused",
        refusal_reason="stop requested by vee; no approval was withdrawn (ADR-69)",
    )
    await run_case(f, approve_then_close)
    assert (
        "executing",
        "handed_over",
        "withdrawn",
    ) in f.transitions and "stop requested by vee" in f.handovers[0]


@pytest.mark.asyncio
async def test_a_refusal_that_voids_the_approval_closes_the_request_as_voided():
    """ST-01, ST-02, ST-13: the dispatcher can't write approval events, so the workflow asks."""
    reason = "patch content is 1f, approved as 2e"
    f = Fakes(dispatch="refused", refusal_reason=reason, void_approval=True)
    await run_case(f, approve_then_close)
    closing = f.inputs["close_approval_request"]
    assert [
        (c.action_id, c.action_digest, c.request_key, c.close_reason, c.close_detail)
        for c in closing
    ] == [("a1", FIX_DIGEST, "a1:approval:1", "voided", reason)]
    assert ("executing", "handed_over", "withdrawn") in f.transitions and f.calls[
        "reconcile_execution"
    ] == 0
    assert f.inputs["notify:handed_over"][0].audience == [
        "on_call",
        "incident_lead",
        "approvers",
        "security",
    ]


@pytest.mark.asyncio
async def test_control_an_ordinary_refusal_voids_nothing():
    f = Fakes(dispatch="refused", refusal_reason="approval expired: past valid_until")
    await run_case(f, approve_then_close)
    assert f.calls["close_approval_request"] == 0
    assert f.inputs["notify:handed_over"][0].audience == ["on_call", "incident_lead"]


@pytest.mark.asyncio
async def test_a_voided_rollback_approval_is_closed_the_same_way():
    f = Fakes(
        verify="failed",
        rollback_dispatch="refused",
        refusal_reason="1 decision(s) ignored: bad signature or digest",
        void_approval=True,
    )
    await run_case(f, approve_then_close)
    closing = f.inputs["close_approval_request"][-1]
    assert (closing.action_id, closing.action_digest, closing.close_reason) == (
        "r1",
        ROLLBACK_DIGEST,
        "voided",
    )
    assert "security" in f.inputs["notify:handed_over"][0].audience


@pytest.mark.asyncio
async def test_the_slot_request_carries_the_run_that_queued_it():
    f = Fakes(status="handed_over")

    async def drive(env, h):
        await wait_state(h, "awaiting_approval")
        await cancel(h)

    await run_case(f, drive, run=2)
    slot = f.inputs["acquire_investigation_slot"][0]
    assert (
        slot.run_id == f"incident-{f.incident_id}-r2"
        and slot.idempotency_key == f"{f.incident_id}-r2:slot"
    )


# ---------------------------------------------------------------------------
# Cancellation (8.7)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cancel_while_awaiting_approval_withdraws_action():
    f = Fakes()

    async def drive(env, h):
        await wait_state(h, "awaiting_approval")
        await cancel(h)

    assert await run_case(f, drive) == "cancelled"
    assert f.calls["dispatch_change"] == 0 and f.calls["close_approval_request"] == 1
    assert f.inputs["close_approval_request"][0].close_reason == "withdrawn"
    assert ("awaiting_approval", "cancelled", "withdrawn") in f.transitions


@pytest.mark.parametrize("via_signal", [True, False])
@pytest.mark.asyncio
async def test_cancel_stops_a_running_sandbox_within_30_s(via_signal):
    f = Fakes(blocks={"run_sandbox_test"})

    async def drive(env, h):
        await wait_state(h, "testing")
        while not f.calls["run_sandbox_test"]:
            await asyncio.sleep(0.05)
        await (cancel(h) if via_signal else h.cancel())

    started = asyncio.get_running_loop().time()
    assert await run_case(f, drive) == "cancelled"
    assert asyncio.get_running_loop().time() - started < 30
    assert f.events == [
        "run_sandbox_test:stopped",
        "cancelled",
    ]  # destroyed before the workflow moved on
    assert ("testing", "cancelled", "withdrawn") in f.transitions


@pytest.mark.parametrize("name", ["collect_evidence", "acquire_investigation_slot"])
@pytest.mark.asyncio
async def test_cancel_stops_the_running_investigation_activity(name):
    f = Fakes(blocks={name})

    async def drive(env, h):
        while not f.calls[name]:
            await asyncio.sleep(0.05)
        await cancel(h)

    assert await run_case(f, drive) == "cancelled"
    assert f.events == [f"{name}:stopped", "cancelled"] and f.calls["hypothesise"] == 0


@pytest.mark.asyncio
async def test_cancel_during_write_lets_write_finish_and_does_not_pretend_reversal():
    f = Fakes(delays={"dispatch_change": 2})

    async def drive(env, h):
        await wait_state(h, "awaiting_approval")
        await approve(h)
        await wait_state(h, "executing")
        await cancel(h, "c-2")
        await wait_state(h, "recovered")
        await close(h)

    result = await run_case(f, drive)
    assert result == "closed"  # not "cancelled": the change happened and was verified
    assert f.calls["dispatch_change"] == 1


@pytest.mark.asyncio
async def test_temporal_cancel_during_write_waits_for_outcome():
    f = Fakes(delays={"dispatch_change": 2})

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


@pytest.mark.parametrize("state,dispatch", [("executing", "applied"), ("reconciling", "unknown")])
@pytest.mark.asyncio
async def test_temporal_cancel_during_transition_write_records_outcome(state, dispatch):
    f = Fakes(delays={"record:" + state: 1.0}, dispatch=dispatch)

    async def drive(env, h):
        await to_approval(h)
        while not f.calls["attempt:" + state]:
            await asyncio.sleep(0.02)
        await h.cancel()  # the write for `state` is in flight
        await wait_state(h, "recovered")
        await close(h)

    assert await run_case(f, drive) == "closed"
    to_states = [t[1] for t in f.transitions]
    assert state in to_states and "cancelled" not in to_states and "recovered" in to_states


@pytest.mark.asyncio
async def test_temporal_cancel_during_transition_write_before_any_change_cancels_after_it():
    f = Fakes(delays={"record:diagnosed": 1.0})

    async def drive(env, h):
        while not f.calls["attempt:diagnosed"]:
            await asyncio.sleep(0.02)
        await h.cancel()

    assert await run_case(f, drive) == "cancelled"
    assert [t[1] for t in f.transitions] == ["investigating", "diagnosed", "cancelled"]


# ---------------------------------------------------------------------------
# Activity exhaustion (8.10) and the database outage
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raises,expected",
    [
        ("draft_patch", ("diagnosed", "handed_over", None)),
        ("run_sandbox_test", ("testing", "handed_over", "test_failed")),
        ("notify:opened", ("inconclusive", "handed_over", None)),
        ("notify:summary", ("inconclusive", "handed_over", None)),
        ("request_approval", ("awaiting_approval", "handed_over", "withdrawn")),
        ("notify:approval_needed", ("awaiting_approval", "handed_over", "withdrawn")),
    ],
)
@pytest.mark.asyncio
async def test_exhausted_activity_hands_over_with_table_path(raises, expected):
    f = Fakes(raises={raises})
    assert await run_case(f, hand_over_then_close) == "closed"
    assert f.transitions[-1] == expected and f.calls["dispatch_change"] == 0


@pytest.mark.asyncio
async def test_escalation_failure_closes_the_request_and_hands_over():
    f = Fakes(raises={"escalate_approval"})

    async def drive(env, h):
        await wait_state(h, "awaiting_approval")
        await env.sleep(16 * 60)
        await hand_over_then_close(env, h)

    await run_case(f, drive)
    assert f.transitions[-1] == ("awaiting_approval", "handed_over", "withdrawn")
    assert f.calls["close_approval_request"] == 1


@pytest.mark.asyncio
async def test_close_request_failure_keeps_the_chosen_path():
    f = Fakes(raises={"close_approval_request"})

    async def drive(env, h):
        await wait_state(h, "awaiting_approval")
        await env.sleep(61 * 60)
        await hand_over_then_close(env, h)

    await run_case(f, drive)
    assert f.transitions[-1] == ("awaiting_approval", "handed_over", "expired")


@pytest.mark.parametrize(
    "raises,final", [("notify:recovered", "verified"), ("notify:handed_over", None)]
)
@pytest.mark.asyncio
async def test_failed_notification_with_no_transition_left_records_an_event(raises, final):
    f = Fakes(raises={raises}, test="passed" if final else "failed")

    async def drive(env, h):
        if final:
            await to_approval(h)
        await wait_state(h, "recovered" if final else "handed_over")
        await close(h)

    assert await run_case(f, drive) == "closed"
    assert (
        f.calls["notification_failed"] == 1
        and f.inputs["notification_failed"][0].kind == raises.split(":")[1]
    )


@pytest.mark.asyncio
async def test_database_outage_longer_than_40_s_pauses_and_resumes():
    f = Fakes(db_down_attempts=7, diagnosis="inconclusive")  # 1+2+4+8+16+30+30 = 91 s of backoff

    async def drive(env, h):
        await env.sleep(120)
        await hand_over_then_close(env, h)

    assert await run_case(f, drive) == "closed"
    assert f.calls["attempt:investigating"] == 8 and f.transitions[0][:2] == (
        "open",
        "investigating",
    )


@pytest.mark.asyncio
async def test_transition_refused_by_the_record_is_not_retried_forever():
    f = Fakes(refuse_transition_to="diagnosed")
    with pytest.raises(WorkflowFailureError):
        await run_case(f, no_driver)
    assert f.calls["attempt:diagnosed"] == 1


# ---------------------------------------------------------------------------
# The 24-hour limit (ADR-46)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_limit_in_open_passes_through_investigating(monkeypatch):
    monkeypatch.setattr(wf, "WORKFLOW_LIMIT", timedelta(minutes=2))
    f = Fakes(raises={"no_slot"})
    assert await run_case(f, no_driver, unsandboxed=True) == "handed_over"
    assert [t[:2] for t in f.transitions] == [
        ("open", "investigating"),
        ("investigating", "inconclusive"),
        ("inconclusive", "handed_over"),
    ]
    assert {r.reason for r in f.records} == {wf.LIMIT_REASON}
    assert f.handovers == [wf.LIMIT_REASON] and f.calls["collect_evidence"] == 0


@pytest.mark.asyncio
async def test_limit_in_investigating_goes_through_inconclusive(monkeypatch):
    monkeypatch.setattr(wf, "WORKFLOW_LIMIT", timedelta(seconds=5))
    f = Fakes(sleeps={"notify:opened": 12})
    assert await run_case(f, no_driver, unsandboxed=True) == "handed_over"
    assert [t[:2] for t in f.transitions] == [
        ("open", "investigating"),
        ("investigating", "inconclusive"),
        ("inconclusive", "handed_over"),
    ]
    assert f.calls["collect_evidence"] == 0


@pytest.mark.asyncio
async def test_limit_stops_a_running_sandbox_and_hands_over(monkeypatch):
    monkeypatch.setattr(wf, "WORKFLOW_LIMIT", timedelta(seconds=10))
    f = Fakes(blocks={"run_sandbox_test"})
    assert await run_case(f, no_driver, unsandboxed=True) == "handed_over"
    assert f.transitions[-1] == ("testing", "handed_over", "withdrawn") and f.events == [
        "run_sandbox_test:stopped"
    ]


@pytest.mark.asyncio
async def test_limit_waits_for_a_write_in_flight_then_hands_over(monkeypatch):
    monkeypatch.setattr(wf, "WORKFLOW_LIMIT", timedelta(seconds=30))
    f = Fakes(sleeps={"dispatch_change": 45})

    async def drive(env, h):
        await to_approval(h)

    assert await run_case(f, drive, unsandboxed=True) == "handed_over"
    assert f.transitions[-1] == ("executing", "handed_over", None)
    assert f.calls["dispatch_change"] == 1 and f.calls["verify_recovery"] == 0


@pytest.mark.asyncio
async def test_limit_leaves_recovered_as_it_is_and_closure_needs_no_run(monkeypatch):
    monkeypatch.setattr(wf, "WORKFLOW_LIMIT", timedelta(minutes=2))
    f = Fakes()

    async def drive(env, h):
        await to_approval(h)
        await wait_state(h, "recovered")

    assert await run_case(f, drive, unsandboxed=True) == "recovered"
    assert (
        f.transitions[-1][1] == "recovered"
    )  # the API can still record `closed`; the run wrote nothing


@pytest.mark.asyncio
async def test_limit_ends_the_wait_after_a_hand_over(monkeypatch):
    monkeypatch.setattr(wf, "WORKFLOW_LIMIT", timedelta(minutes=2))
    f = Fakes(diagnosis="inconclusive")
    assert await run_case(f, no_driver, unsandboxed=True) == "handed_over"
    assert f.transitions[-1][1] == "handed_over"


# ---------------------------------------------------------------------------
# Investigation slots (C13)
# ---------------------------------------------------------------------------
def test_may_grant_takes_severity_then_oldest_up_to_five():
    t = datetime(2026, 11, 2, tzinfo=timezone.utc)
    rows = [
        act.QueueRow("old", "a", "sev2", t),
        act.QueueRow("new", "b", "sev2", t + timedelta(seconds=1)),
        act.QueueRow("worst", "c", "sev1", t + timedelta(seconds=2)),
    ]
    assert act.may_grant("worst", rows) and not act.may_grant("old", rows)
    rows[2] = dataclasses.replace(rows[2], granted=True)
    assert act.may_grant("old", rows) and not act.may_grant("new", rows)
    full = [act.QueueRow(f"g{i}", f"s{i}", "sev1", t, True) for i in range(5)]
    assert not act.may_grant("old", full + rows[:1])


def test_may_grant_holds_one_service_to_four_of_five_slots():
    """NFR-44: the service at its limit waits, and the next eligible incident takes the slot."""
    t = datetime(2026, 11, 2, tzinfo=timezone.utc)
    noisy = [act.QueueRow(f"n{i}", "noisy", "sev2", t, True) for i in range(4)]
    worse = act.QueueRow("noisy-sev1", "noisy", "sev1", t)
    quiet = act.QueueRow("quiet-sev4", "quiet", "sev4", t + timedelta(hours=1))
    assert not act.may_grant("noisy-sev1", noisy + [worse, quiet])
    assert act.may_grant("quiet-sev4", noisy + [worse, quiet])
    assert act.may_grant("noisy-sev1", noisy[:3] + [worse, quiet])  # at three, severity order holds
    assert not act.may_grant(
        "noisy-sev1", noisy + [worse]
    )  # nobody else waiting: the fifth stays free


@pytest.mark.asyncio
async def test_six_concurrent_incidents_give_five_running_in_severity_order():
    f = Fakes(diagnosis="inconclusive")
    ids = [str(uuid.uuid4()) for _ in range(6)]
    severities = ["sev3", "sev1", "sev4", "sev2", "sev2", "sev3"]
    t = datetime(2026, 11, 2, tzinfo=timezone.utc)
    f.queue = [
        act.QueueRow(i, f"svc{n}", s, t + timedelta(seconds=n))
        for n, (i, s) in enumerate(zip(ids, severities))
    ]
    order = [r.incident_id for r in sorted(f.queue, key=lambda r: (r.severity, r.queued_at))]

    async with await start_env() as env:
        f.env = env
        inv_worker, disp_worker, test_worker = workers(env, f)
        async with inv_worker, disp_worker, test_worker:
            handles = {
                i: await env.client.start_workflow(
                    IncidentWorkflow.run,
                    IncidentWorkflowInput(i),
                    id=f"incident-{i}",
                    task_queue=act.INVESTIGATION_QUEUE,
                )
                for i in ids
            }
            for _ in range(10):
                if len(f.grants) == 5:
                    break
                await env.sleep(16)
            await env.sleep(32)  # the sixth keeps asking and is refused
            assert f.grants == order[:5]
            assert (await handles[order[5]].query(IncidentWorkflow.current_state))[
                "incident"
            ] == "open"
            # A slot frees when an incident leaves `investigating` (record_transition's delete).
            f.queue = [r for r in f.queue if r.incident_id != order[0]]
            await env.sleep(16)
            assert f.grants == order
            for h in handles.values():
                await wait_state(h, "handed_over")
                await close(h)
            for h in handles.values():
                await h.result()


# ---------------------------------------------------------------------------
# Rules the activities apply, and the reconciliation job
# ---------------------------------------------------------------------------
def gate():
    now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
    return ToolGate("i1", "checkout", now - timedelta(hours=1), now)


def test_requests_are_prechecked_and_a_refusal_is_not_a_failure():
    allowed, refused = act.precheck_requests(
        gate(),
        [LOG_REQUEST.model_dump(), {"tool": "apply_change", "reason": "write", "params": {}}],
        "hypothesise",
    )
    assert [r["tool"] for r in allowed] == ["get_logs"]
    assert [r["refused"] for r in refused] == ["write_forbidden"]


@pytest.mark.parametrize(
    "summary,code",
    [
        ("key AKIAABCDEFGHIJKLMNOP leaked", "security_risk"),
        ("see https://evil.example/x", "invalid_output"),
    ],
)
def test_model_output_is_checked_before_the_coordinator_sees_it(summary, code):
    draft = diagnosis("established", 1).draft.model_copy(update={"summary": summary})
    result = act.checked_result("diagnose", draft, SkillUsage(model_calls=1))
    assert result.failure.code == code and result.output is None and result.usage.model_calls == 1


def test_clean_model_output_passes_through():
    draft = diagnosis("established", 1).draft
    assert act.checked_result("diagnose", draft, SkillUsage()).output == draft


def test_dispatcher_results_map_to_dispatch_out():
    refused = act.dispatch_out(
        DispatchResult(Outcome.REFUSED, "approval expired: late", "a1", intent_recorded=False)
    )
    assert refused.outcome == "refused" and refused.refusal_reason.startswith(act.REFUSED_EXPIRED)
    assert refused.intent_recorded is False
    applied = act.dispatch_out(
        DispatchResult(Outcome.APPLIED, "release process: applied", "a1"), "x1", "rel-1"
    )
    assert (applied.outcome, applied.refusal_reason, applied.intent_recorded) == (
        "applied",
        None,
        True,
    )
    assert "ReleaseInProgress" not in wf.READ_RETRY.non_retryable_error_types


def test_database_refusals_become_invalid_transition():
    assert isinstance(act.db_error("23514", "illegal"), InvalidTransition)
    assert isinstance(act.db_error("23001", "restricted"), InvalidTransition)
    assert not isinstance(act.db_error("08006", "gone"), InvalidTransition)


def test_reconciliation_findings_follow_the_8_9_table():
    incidents = {
        "lost": "testing",
        "stuck": "open",
        "queued": "open",
        "starting": "open",
        "done": "closed",
        "differs": "verifying",
        "fine": "investigating",
        "owned": "handed_over",
        "ok": "recovered",
    }
    running = {"done": "recovered", "differs": "executing", "fine": "investigating"}
    queue = {"queued": "incident-queued", "fine": "incident-fine", "owned": "incident-owned"}
    stalled, orphans = act.reconciliation_findings(
        incidents, running, queue, {"starting"}, live_runs={"incident-queued", "incident-fine"}
    )
    assert stalled == {"testing": 1, "open": 1, "closed": 1, "verifying": 1}
    assert orphans == ["owned"]


def test_reconciliation_keeps_the_queue_row_of_a_run_that_is_still_waiting():
    """ADR-69: a re-investigation queues while its incident is still handed_over. Its row stays until the
    run ends; the first run's leftover row, on the same status, goes."""
    incidents = {"again": "handed_over", "left": "handed_over"}
    running = {"again": "handed_over"}
    queue = {"again": "incident-again-r2", "left": "incident-left"}
    stalled, orphans = act.reconciliation_findings(
        incidents, running, queue, set(), live_runs={"incident-again-r2"}
    )
    assert stalled == {} and orphans == ["left"]


def test_may_grant_sorts_an_unset_severity_last_as_the_table_does():
    t = datetime(2026, 11, 2, tzinfo=timezone.utc)
    rows = [
        act.QueueRow("unset", "a", None, t),
        act.QueueRow("minor", "b", "sev4", t + timedelta(seconds=1)),
        act.QueueRow("unset-later", "c", None, t + timedelta(seconds=2)),
    ]
    assert act.may_grant("minor", rows) and not act.may_grant("unset", rows)
    assert act.may_grant("unset", rows[:1] + rows[2:])  # among the unset, oldest first


def test_reconciliation_schedule_runs_every_five_minutes_on_the_worker_queue():
    s = act.reconciliation_schedule()
    assert act.RECONCILIATION_SCHEDULE == "aisre-reconciliation"
    assert s.spec.intervals[0].every == timedelta(minutes=5)
    assert (
        s.action.workflow == "ReconciliationWorkflow"
        and s.action.task_queue == act.INVESTIGATION_QUEUE
    )


@pytest.mark.asyncio
async def test_reconciliation_workflow_runs_its_one_activity():
    @activity.defn(name="reconcile_workflows")
    async def reconcile_workflows(inp: act.ReconciliationIn) -> act.ReconciliationOut:
        return act.ReconciliationOut(2)

    async with await start_env() as env:
        async with Worker(
            env.client,
            task_queue=act.INVESTIGATION_QUEUE,
            workflows=[ReconciliationWorkflow],
            activities=[reconcile_workflows],
        ):
            result = await env.client.execute_workflow(
                ReconciliationWorkflow.run, id="recon", task_queue=act.INVESTIGATION_QUEUE
            )
    assert result == 2


@pytest.mark.asyncio
async def test_completed_history_replays_deterministically():
    """Replay strategy (8.8): every recorded history must replay against the current code.
    In CI, the same check runs over histories exported from production before each deploy."""
    from temporalio.worker import Replayer

    f = Fakes(critic=["revise", "accept"], verify="failed", conditional=False)
    async with await start_env() as env:
        f.env = env
        inv_worker, disp_worker, test_worker = workers(env, f)
        async with inv_worker, disp_worker, test_worker:
            iid = str(uuid.uuid4())
            h = await env.client.start_workflow(
                IncidentWorkflow.run,
                IncidentWorkflowInput(iid),
                id=f"incident-{iid}",
                task_queue=act.INVESTIGATION_QUEUE,
            )
            await wait_state(h, "awaiting_approval")
            await approve(h)
            await wait_state(h, "not_recovered")
            await rollback_decision(h)
            await wait_state(h, "handed_over")
            await close(h)
            assert await h.result() == "closed"
            history = await h.fetch_history()
    await Replayer(workflows=[IncidentWorkflow], data_converter=act.DATA_CONVERTER).replay_workflow(
        history
    )


def test_reconciliation_tells_a_withheld_start_from_a_stall():
    """M3 follow-up: an incident whose start was withheld on purpose (the integration tests'
    `quiet` ones) isn't counted while no run is live; the same states with a start the relay
    really delivered are stalls, and a withheld one with a live run is judged as usual."""
    incidents = {
        "held-open": "open",
        "held-moved": "investigating",
        "held-live": "testing",
        "lost-open": "open",
        "lost-moved": "investigating",
    }
    running = {"held-live": "investigating"}
    stalled, _ = act.reconciliation_findings(
        incidents,
        running,
        {},
        set(),
        live_runs={"incident-held-live"},
        unstarted={"held-open", "held-moved", "held-live"},
    )
    assert stalled == {"open": 1, "investigating": 1, "testing": 1}
