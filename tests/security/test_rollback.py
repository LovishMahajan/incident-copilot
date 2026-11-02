"""ST-39: a rollback is its own approved action. Real action.py, dispatcher.py, verification.py and states.py."""

from __future__ import annotations

import ast
import inspect
from datetime import timedelta
from pathlib import Path

import pytest

import verification
import workflow as wf
from action import ApprovalError, MATRIX, Perm, Role
from dispatcher import Outcome
from states import ActionState as A, InvalidTransition, check_action
from support import AFTER, BASE, T0, FakeRelease, decide, make_action, make_rollback, rig, rows
from workflow import IncidentWorkflow


def pair():
    fix = make_action()
    return fix, make_rollback(fix)


def conditional(fix, rb, **kw):
    return rows(fix, "priya", rollback=rb, rollback_digest=rb.digest, rollback_approval_id="rb-ok", **kw)[1]


def at_parent_revision(rb, ds, verdict, now=None, revision=AFTER):
    r = rig((rb, ds), release=FakeRelease(revision=revision), **({"now": now} if now else {}))
    if verdict:
        r.store.verdicts[rb.parent_action_id] = verdict
    return r


def refused(r, rb):
    assert r.run(rb.action_id).outcome == Outcome.REFUSED and r.release.calls == []


def test_st39_control_conditional_approval_after_a_failed_verdict_runs_and_restores_the_base():
    fix, rb = pair()
    r = at_parent_revision(rb, [conditional(fix, rb)], "failed")
    assert r.run(rb.action_id).outcome == Outcome.APPLIED and r.release.revision == BASE
    assert r.release.calls[0]["patch"] is None and rb.action_id != fix.action_id


def test_st39_no_approval_at_all_is_refused():
    _, rb = pair()
    refused(at_parent_revision(rb, [], "failed"), rb)


@pytest.mark.parametrize("verdict", [None, "verified", "inconclusive"])
def test_st39_conditional_approval_needs_a_failed_parent(verdict):
    fix, rb = pair()
    refused(at_parent_revision(rb, [conditional(fix, rb)], verdict), rb)


def test_st39_conditional_approval_is_refused_after_its_valid_until():
    fix, rb = pair()
    d = conditional(fix, rb)
    late = T0 + timedelta(minutes=15) + timedelta(seconds=fix.hold_seconds) + timedelta(minutes=15, seconds=1)
    refused(at_parent_revision(rb, [d], "failed", now=late), rb)


def test_st39_conditional_approval_lapses_exactly_where_adr48_says():
    fix, rb = pair()
    assert conditional(fix, rb).valid_until == (T0 + timedelta(minutes=40)).isoformat()


@pytest.mark.parametrize("revision", [BASE, "ffffffff"])
def test_st39_deployment_off_the_parents_produced_revision_is_refused(revision):
    fix, rb = pair()
    refused(at_parent_revision(rb, [conditional(fix, rb)], "failed", revision=revision), rb)


def test_st39_a_conditional_approval_for_a_tampered_rollback_is_ignored():
    fix, rb = pair()
    d = conditional(fix, rb)
    other = make_rollback(fix, action_id=rb.action_id, expected_after_revision="ffffffff")
    refused(at_parent_revision(other, [d], "failed"), other)


def test_st39_a_plain_approval_on_a_proposed_rollback_is_refused():
    _, rb = pair()
    with pytest.raises(ApprovalError) as e:
        rows(rb, "priya", action_status="proposed")
    assert e.value.code == "action_not_open"


def test_st39_only_a_rollback_skips_testing():
    check_action(A.PROPOSED, A.AWAITING_APPROVAL, "rollback")
    with pytest.raises(InvalidTransition):
        check_action(A.PROPOSED, A.AWAITING_APPROVAL, "config_revert")


@pytest.mark.parametrize("verdict", [None, "verified"])
def test_st39_a_plain_approval_cannot_run_a_rollback_of_a_fix_that_did_not_fail(verdict):
    _, rb = pair()
    refused(at_parent_revision(rb, [decide(rb, "priya")], verdict), rb)


def test_st39_control_a_fresh_plain_approval_after_a_failed_verdict_runs():
    _, rb = pair()
    assert at_parent_revision(rb, [decide(rb, "priya")], "failed").run(rb.action_id).outcome == Outcome.APPLIED


def test_st39_the_verifier_has_no_path_to_the_dispatcher():
    tree = ast.parse(Path(verification.__file__).read_text())
    modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | \
              {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not modules & {"dispatcher", "activities", "workflow"}
    assert modules & {"action"}                        # it does import action, so this check reads real imports


def test_st39_no_person_or_worker_role_can_execute():
    assert [r for r, perms in MATRIX.items() if Perm.EXECUTE in perms] == [Role.SYSTEM_DISPATCHER]


def test_st39_only_a_failed_verdict_calls_the_workflows_rollback():
    source = inspect.getsource(IncidentWorkflow._lifecycle)
    tree = ast.parse("class _:\n" + source)
    guards = [n for n in ast.walk(tree) if isinstance(n, ast.If)
              and any(isinstance(c, ast.Attribute) and c.attr == "_roll_back" for c in ast.walk(n))]
    calls = [c for c in ast.walk(tree) if isinstance(c, ast.Attribute) and c.attr == "_roll_back"]
    assert len(calls) == 1
    tests = [ast.unparse(n.test) for n in guards]
    assert any("verdict == 'failed'" in t for t in tests)


def test_st39_api_route_needs_the_api():
    pytest.skip("Needs a running API: no route exists to show a rollback request through it is refused. The workflow "
                "paths run on Temporal in test_workflow.py (test_inconclusive_verification_hands_over_without_rollback)")
