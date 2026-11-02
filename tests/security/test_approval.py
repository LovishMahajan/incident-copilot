"""ST-01 to ST-12: approval forgery, self-approval and the wrong person.
Real action.py, dispatcher.py and states.py. workflow.py's signal handler runs with only its clock replaced."""

from __future__ import annotations

import base64
import dataclasses
import inspect
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import workflow as wf
from action import ApprovalError, Directory, ImpactFacts, Role, Signer, Standing, evaluate, verify_signature
from activities import DispatchIn
from dispatcher import Dispatcher, Ledger, Outcome
from fence import Actor, FenceReason, FenceState, ReconciliationReport
from states import ActionState as A, InvalidTransition, check_action
from support import (KEYS, ORDINARY, POLICY, SIGNER, SOON, T0, FakeRelease, MemoryStore, decide, make_action, make_rollback,
                     people, rig, rows)
from workflow import ApprovalSignal, IncidentWorkflow

ATTACKER_KEY = Ed25519PrivateKey.generate()


def refused_by_dispatcher(r, action_id):
    res = r.run(action_id)
    assert res.outcome == Outcome.REFUSED and res.intent_recorded is False
    assert r.release.calls == [] and r.ledger.rows == {}
    assert [e["type"] for e in r.ledger.events] == ["aisre.execution.refused.v1"]
    return res


def refused_by_approval_service(code, a, subject, *args, **kw):
    events = kw.setdefault("events", [])
    with pytest.raises(ApprovalError) as e:
        rows(a, subject, *args, **kw)
    assert e.value.code == code
    assert [x["type"] for x in events] == ["aisre.approval.refused.v1"]
    return events[0]["data"]


def standing(a, decisions, now=SOON, directory=None, **kw):
    return evaluate(a, decisions, now=now, keys=KEYS, directory=directory or people(), policy=POLICY, impact=ORDINARY,
                    requested_at=T0, **kw)[0]


def test_control_a_valid_approval_executes():
    """Every refusal below is measured against this: the same set-up, unattacked, does run."""
    a = make_action()
    r = rig((a, [decide(a, "priya")]))
    assert r.run(a.action_id).outcome == Outcome.APPLIED and len(r.release.calls) == 1


# ---------------------------------------------------------------- ST-01
def test_st01_row_signed_with_a_key_the_platform_does_not_hold_is_ignored():
    a = make_action()
    forged = decide(a, "priya", signer=Signer("k1", ATTACKER_KEY))
    assert verify_signature(forged, KEYS) is False
    assert "ignored" in refused_by_dispatcher(rig((a, [forged])), a.action_id).reason


def test_st01_forged_row_cannot_be_the_second_approver():
    a = make_action(high_impact=True)
    forged = decide(a, "sam", signer=Signer("k1", ATTACKER_KEY))
    refused_by_dispatcher(rig((a, [decide(a, "priya"), forged])), a.action_id)


@pytest.mark.parametrize("signature", [
    "", "!!!!", base64.b64encode(b"short").decode(), base64.b64encode(b"\0" * 64).decode(),
    "a",
])
def test_st01_malformed_signature_is_ignored_not_fatal(signature):
    a = make_action()
    bad = dataclasses.replace(decide(a, "priya"), signature=signature)
    refused_by_dispatcher(rig((a, [bad])), a.action_id)


def test_st01_st02_st13_voiding_the_approval_and_alerting_are_not_implemented():
    pytest.skip("Needs a component that doesn't exist: the dispatcher's role may write only aisre.execution.* events "
                "(schema.sql check_timeline_writer), ExecutionRefusedData has no alert flag, and neither alerts.yml nor "
                "any module turns a signature refusal into aisre.approval.voided.v1 or a security alert")


def test_st01_a_good_signature_with_junk_added_is_not_good():
    """Lenient base64 drops characters outside the alphabet, which would let two strings verify as one."""
    a = make_action()
    d = decide(a, "priya")
    assert verify_signature(d, KEYS) and not verify_signature(dataclasses.replace(d, signature=d.signature + "!"), KEYS)


# ---------------------------------------------------------------- ST-02
@pytest.mark.parametrize("field,value", [
    ("subject", "sam"), ("valid_until", (T0 + timedelta(days=30)).isoformat()), ("action_digest", "0" * 64),
    ("action_id", "other-action"), ("decision", "approved"), ("mfa", False), ("role", "incident_lead"),
    ("policy_version", "edit-policy@9|approval-policy@9"), ("conditional", True), ("key_id", "k2"),
])
def test_st02_any_edited_signed_field_fails_the_signature(field, value):
    a = make_action()
    d = decide(a, "priya", mfa=True)
    if getattr(d, field) == value:                     # decision is already "approved": edit a rejection instead
        d = decide(a, "sam", "rejected", reason="touches the wrong key")
    edited = dataclasses.replace(d, **{field: value})
    assert verify_signature(d, KEYS) is True and verify_signature(edited, KEYS) is False
    assert standing(a, [edited]) == Standing.PENDING


@pytest.mark.parametrize("change", [{"patch_sha256": "f" * 64}, {"base_revision": "ffffffff"},
                                    {"expected_after_revision": "ffffffff"}, {"high_impact": True},
                                    {"recovery_predicates_digest": "f" * 64}])
def test_st02_edited_action_row_no_longer_matches_the_signed_digest(change):
    a = make_action()
    d = decide(a, "priya")
    edited = dataclasses.replace(a, **change)
    refused_by_dispatcher(rig((edited, [d])), a.action_id)


def test_st02_target_edit_cannot_be_followed_by_a_digest_edit():
    a = make_action()
    d = decide(a, "priya")
    moved = dataclasses.replace(a, target=dataclasses.replace(a.target, deployment="checkout-prod-us1"))
    refused_by_dispatcher(rig((moved, [dataclasses.replace(d, action_digest=moved.digest)])), a.action_id)


# ---------------------------------------------------------------- ST-03
def test_st03_valid_approval_attached_to_another_action_is_ignored():
    a, b = make_action(), make_action(expected_after_revision="99999999")
    d = decide(a, "priya")
    assert verify_signature(d, KEYS)                   # the signature itself is genuine
    refused_by_dispatcher(rig((b, [d])), b.action_id)


def test_st03_approval_cannot_be_rebound_by_editing_its_action_fields():
    a, b = make_action(), make_action(expected_after_revision="99999999")
    moved = dataclasses.replace(decide(a, "priya"), action_id=b.action_id, action_digest=b.digest)
    refused_by_dispatcher(rig((b, [moved])), b.action_id)


# ---------------------------------------------------------------- ST-04
def test_st04_stolen_key_decision_that_was_never_stored_is_not_read():
    a = make_action()
    signed_with_real_key = decide(a, "priya")          # valid, but only the attacker holds it
    r = rig((a, []))
    assert verify_signature(signed_with_real_key, KEYS)
    refused_by_dispatcher(r, a.action_id)


def test_st04_nothing_but_the_action_id_reaches_the_dispatcher():
    assert list(inspect.signature(Dispatcher.execute).parameters) == ["self", "action_id"]
    assert [f.name for f in dataclasses.fields(DispatchIn)] == ["action_id"]
    with pytest.raises(TypeError):
        DispatchIn("a1", decisions=["forged"])


# ---------------------------------------------------------------- ST-05
def test_st05_decision_signed_with_a_retired_key_is_ignored():
    a = make_action()
    d = decide(a, "priya")
    refused_by_dispatcher(rig((a, [d]), keys={}), a.action_id)     # the public key was removed


def test_st05_a_new_key_id_cannot_borrow_the_retired_keys_row():
    a = make_action()
    d = dataclasses.replace(decide(a, "priya"), key_id="k2")
    refused_by_dispatcher(rig((a, [d]), keys={"k2": ATTACKER_KEY.public_key()}), a.action_id)


def test_st05_no_write_while_the_global_fence_is_on():
    a = make_action()
    fence = FenceState()
    fence.fence(FenceReason.MANUAL, actor=Actor("sam", "incident_lead"), why="signing key stolen")
    assert fence.writes_allowed(a.target.deployment, fence.epoch)[0] is False
    r = rig((a, [decide(a, "priya")]), fence=fence)
    res = r.run(a.action_id)
    assert res.outcome == Outcome.REFUSED and "manual" in res.reason and r.release.calls == []


def test_st05_no_write_while_this_deployment_is_fenced():
    a = make_action()
    fence = FenceState()
    fence.fence(FenceReason.UNKNOWN_OUTCOME, a.target.deployment)
    r = rig((a, [decide(a, "priya")]), fence=fence)
    assert r.run(a.action_id).outcome == Outcome.REFUSED and r.release.calls == []


def test_st05_a_fence_that_lands_during_the_prechecks_stops_the_write():
    class FencedDuringRead(FakeRelease):
        async def current_state(self, deployment):
            fence.fence(FenceReason.BACKUP_QUIESCE)
            return await super().current_state(deployment)
    a, fence = make_action(), FenceState()
    r = rig((a, [decide(a, "priya")]), fence=fence, release=FencedDuringRead())
    assert r.run(a.action_id).outcome == Outcome.REFUSED and r.release.calls == [] and not r.ledger.rows


def test_st05_a_stale_epoch_token_is_refused_after_the_restore_completes():
    a = make_action()
    fence = FenceState()
    r = rig((a, [decide(a, "priya")]), fence=fence)    # this dispatcher took its token before the restore
    fence.begin_restore()
    fence.complete_restore(ReconciliationReport(), Actor("sam", "incident_lead"), Actor("admin", "platform_admin"))
    assert fence.writes_allowed(a.target.deployment, fence.epoch)[0] is True
    res = r.run(a.action_id)
    assert res.outcome == Outcome.REFUSED and "stale fence token" in res.reason and r.release.calls == []


def test_st05_a_dispatcher_started_after_the_restore_writes_again():
    a = make_action()
    fence = FenceState()
    fence.begin_restore()
    fence.complete_restore(ReconciliationReport(), Actor("sam", "incident_lead"), Actor("admin", "platform_admin"))
    assert rig((a, [decide(a, "priya")]), fence=fence).run(a.action_id).outcome == Outcome.APPLIED


def test_st05_a_fenced_dispatcher_refuses_without_calling_a_release_process_that_is_down():
    class Down(FakeRelease):
        async def current_state(self, deployment):
            raise ConnectionError("release process down")
    a, fence = make_action(), FenceState()
    fence.fence(FenceReason.RELEASE_PROCESS_DOWN)
    r = rig((a, [decide(a, "priya")]), fence=fence, release=Down())
    assert r.run(a.action_id).outcome == Outcome.REFUSED


def test_st05_the_fence_is_required_so_no_dispatcher_can_be_built_without_it():
    with pytest.raises(TypeError):
        Dispatcher(MemoryStore(), FakeRelease(), Ledger(), KEYS, people(), POLICY, lambda: SOON)


# ---------------------------------------------------------------- ST-06
@pytest.mark.parametrize("subject", ["system:workflow", "system:dispatcher", "system:agent", "system:anything"])
def test_st06_no_platform_identity_can_approve_even_if_granted_the_role(subject):
    directory = Directory({(subject, "checkout"): {Role.APPROVER}})
    data = refused_by_approval_service("self_approval", make_action(), subject, directory=directory)
    assert data["security_alert"] is True


@pytest.mark.parametrize("subject", ["priya", "sam"])
def test_st06_requester_or_proposer_cannot_approve(subject):
    refused_by_approval_service("self_approval", make_action(), subject, proposer_subjects={subject, "system:workflow"})


# ---------------------------------------------------------------- ST-07
def test_st07_author_cannot_be_sole_approver_of_a_revert_and_the_action_waits():
    a = make_action(kind="commit_revert")
    refused_by_approval_service("self_approval", a, "priya", change_author="priya")
    assert standing(a, []) == Standing.PENDING
    assert standing(a, [decide(a, "sam", change_author="priya")]) == Standing.APPROVED


def test_st07_author_can_only_be_one_of_two_on_a_high_impact_revert():
    a = make_action(kind="commit_revert", high_impact=True)
    one = decide(a, "priya", change_author="priya")
    assert standing(a, [one]) == Standing.PENDING


# ---------------------------------------------------------------- ST-08
@pytest.mark.parametrize("subject", ["wes", "vee", "admin"])
def test_st08_people_who_can_see_the_service_but_not_approve_are_forbidden(subject):
    assert refused_by_approval_service("forbidden", make_action(), subject)["security_alert"] is True


@pytest.mark.parametrize("subject", ["ravi", "sol"])
def test_st08_no_role_on_the_service_is_not_found_so_existence_is_hidden(subject):
    """ADR-58 hides existence from someone with no read right, so ST-08 expects `not_found` here."""
    assert refused_by_approval_service("not_found", make_action(), subject)["security_alert"] is True


# ---------------------------------------------------------------- ST-09
@pytest.mark.parametrize("state", [dict(auth_time=T0 - timedelta(minutes=15, seconds=1)), dict(mfa=False)])
def test_st09_stale_or_single_factor_session_cannot_approve_but_can_reject(state):
    a = make_action()
    assert refused_by_approval_service("reauth_required", a, "priya", **state)["security_alert"] is True
    assert decide(a, "priya", "rejected", reason="not this target", **state).decision == "rejected"


def test_st09_the_limit_is_inclusive_at_fifteen_minutes():
    assert decide(make_action(), "priya", auth_time=T0 - timedelta(minutes=15)).decision == "approved"


# ---------------------------------------------------------------- ST-10
def test_st10_approval_past_its_validity_is_refused():
    a = make_action()
    refused_by_dispatcher(rig((a, [decide(a, "priya")]), now=T0 + timedelta(minutes=15, seconds=1)), a.action_id)


def test_st10_approver_who_lost_rights_is_refused_against_the_current_directory():
    a = make_action()
    refused_by_dispatcher(rig((a, [decide(a, "priya")]), directory=Directory({})), a.action_id)


@pytest.mark.parametrize("rejection_first", [True, False])
def test_st10_rejection_wins_in_either_order(rejection_first):
    a = make_action()
    approved = decide(a, "priya")
    rejected = decide(a, "vee", "rejected", reason="touches the wrong key", now=T0 + timedelta(seconds=30))
    r = rig((a, [rejected, approved] if rejection_first else [approved, rejected]))
    assert "rejected" in refused_by_dispatcher(r, a.action_id).reason


def test_st10_revocation_before_intent_is_refused():
    a = make_action()
    ok = decide(a, "priya")
    gone = decide(a, "priya", "revoked", revokes=ok.approval_id, prior=[ok], reason="wrong target")
    refused_by_dispatcher(rig((a, [ok, gone])), a.action_id)


# ---------------------------------------------------------------- ST-11
def test_st11_one_approval_on_a_high_impact_action_stays_pending_and_nothing_runs():
    a = make_action(high_impact=True)
    assert "pending" in refused_by_dispatcher(rig((a, [decide(a, "priya")])), a.action_id).reason


def test_st11_same_person_twice_is_refused_duplicate_and_counts_once():
    a = make_action(high_impact=True)
    first = decide(a, "priya")
    refused_by_approval_service("duplicate", a, "priya", prior=[first])
    second_row = decide(a, "priya")                    # as if it reached storage anyway
    refused_by_dispatcher(rig((a, [first, second_row])), a.action_id)


def test_st11_two_different_people_run_it():
    a = make_action(high_impact=True)
    r = rig((a, [decide(a, "priya"), decide(a, "sam")]))
    assert r.run(a.action_id).outcome == Outcome.APPLIED


def test_st11_a_rollback_keeps_its_parents_two_person_rule():
    assert POLICY.approvers_needed(make_rollback(make_action(high_impact=True)), ORDINARY) == 2


@pytest.fixture
def signals(monkeypatch):
    """IncidentWorkflow's real handlers. Only temporalio's clock is replaced, as no worker is running."""
    monkeypatch.setattr(wf.workflow, "now", lambda: T0.astimezone())
    w = IncidentWorkflow()
    w.awaited, w.approvers_needed = ("a1", "d" * 64), 2
    return w


def test_st11_workflow_counts_one_person_once(signals):
    for n in (1, 2):
        signals.approval_decided(ApprovalSignal(f"ap-{n}", "a1", "approved", "d" * 64, "priya"))
    assert signals._valid_approvals() == 1 and signals._decided() is False
    signals.approval_decided(ApprovalSignal("ap-3", "a1", "approved", "d" * 64, "sam"))
    assert signals._valid_approvals() == 2 and signals._decided() is True


def test_st11_release_rollback_is_always_two_person():
    a = make_action(kind="release_rollback", patch_sha256=None, high_impact=False)
    refused_by_dispatcher(rig((a, [decide(a, "priya")])), a.action_id)


@pytest.mark.parametrize("impact", [ImpactFacts("sev1"), ImpactFacts(None), ImpactFacts("sev3", critical=True),
                                    ImpactFacts("sev3", deployments=2), ImpactFacts("sev3", parent_kind="release_rollback")],
                         ids=["sev1", "severity_not_confirmed", "critical_service", "two_deployments", "rollback_of_a_release"])
def test_st11_each_trigger_needs_a_second_person_whatever_the_flag_says(impact):
    a = make_action(high_impact=False)
    r = rig((a, [decide(a, "priya")]))
    r.store.impacts[a.action_id] = impact
    refused_by_dispatcher(r, a.action_id)
    a2 = make_action(high_impact=False)
    r2 = rig((a2, [decide(a2, "priya"), decide(a2, "sam")]))
    r2.store.impacts[a2.action_id] = impact
    assert r2.run(a2.action_id).outcome == Outcome.APPLIED


def test_st11_control_one_approver_is_enough_for_a_confirmed_ordinary_change():
    a = make_action(high_impact=False)
    assert rig((a, [decide(a, "priya")])).run(a.action_id).outcome == Outcome.APPLIED


def test_st11_a_true_flag_still_counts():
    a = make_action(high_impact=True)
    refused_by_dispatcher(rig((a, [decide(a, "priya")])), a.action_id)


def test_st07_a_derived_second_approver_lets_the_author_be_one_of_two():
    a = make_action(kind="commit_revert")
    assert decide(a, "priya", change_author="priya", impact=ImpactFacts(None)).decision == "approved"


# ---------------------------------------------------------------- ST-12
@pytest.mark.parametrize("status", ["withdrawn", "expired", "rejected", "awaiting_approval", "applying", "not_applied"])
def test_st12_the_dispatcher_reads_status_so_only_an_approved_action_runs(status):
    a = make_action()
    r = rig((a, [decide(a, "priya")]))
    r.store.statuses[a.action_id] = status
    res = r.run(a.action_id)
    assert res.outcome == Outcome.REFUSED and status in res.reason and r.release.calls == []


@pytest.mark.parametrize("status", ["proposed", "testing", "test_failed", "approved", "applying", "rejected", "expired",
                                    "withdrawn", "executed"])
def test_st12_only_an_action_awaiting_approval_can_be_approved(status):
    refused_by_approval_service("action_not_open", make_action(), "priya", action_status=status)


def test_st12_a_closed_request_cannot_be_approved():
    refused_by_approval_service("action_not_open", make_action(), "priya", request_open=False)


@pytest.mark.parametrize("state", [s for s in A if s != A.AWAITING_APPROVAL])
def test_st12_approval_is_reachable_only_from_awaiting_approval(state):
    with pytest.raises(InvalidTransition):
        check_action(state, A.APPROVED, "config_revert")


@pytest.mark.parametrize("state", [A.PROPOSED, A.TEST_FAILED])
def test_st12_approval_cannot_move_an_action_past_testing(state):
    with pytest.raises(InvalidTransition):
        check_action(state, A.AWAITING_APPROVAL, "config_revert")
