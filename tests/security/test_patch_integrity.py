"""ST-13 to ST-15: the patch cannot change after approval. Real patch.py, action.py, dispatcher.py, contracts.py."""

from __future__ import annotations

import inspect
import uuid

import pytest
from pydantic import ValidationError

from aisre.remediation.action import record_decision
from aisre.workflow.activities import DispatchIn
from aisre.contracts import ApprovalCreate, RejectionCreate, RevocationCreate
from aisre.remediation.dispatcher import Outcome
from support import FakeRelease, MemoryStore, decide, make_action, make_patch, rig


# ---------------------------------------------------------------- ST-13
def test_st13_any_content_change_changes_the_fingerprint():
    other = {"config/checkout/payments.yaml": "payments:\n  timeout_ms: 2999\n  retries: 2\n"}
    assert make_patch().digest != make_patch(new=other).digest


def test_st13_changed_fingerprint_on_the_action_voids_its_approval():
    a = make_action()
    d = decide(a, "priya")
    swapped = make_action(action_id=a.action_id, patch_sha256="f" * 64)
    r = rig((swapped, [d]))
    assert r.run(a.action_id).outcome == Outcome.REFUSED and r.release.calls == []


def test_st13_bytes_swapped_in_the_artifact_store_stop_execution():
    a = make_action()
    store = MemoryStore().add(a, [decide(a, "priya")], content=b"timeout_ms: 1  # swapped after approval")
    r = rig(store=store)
    res = r.run(a.action_id)
    assert res.outcome == Outcome.REFUSED and r.release.calls == []


def test_st13_control_the_stored_bytes_that_hash_to_the_approval_run():
    a = make_action()
    r = rig(store=MemoryStore().add(a, [decide(a, "priya")], content=make_patch().canonical_bytes()))
    assert r.run(a.action_id).outcome == Outcome.APPLIED


@pytest.mark.parametrize("failure", [KeyError("gone"), ConnectionError("artifact store down")])
def test_st13_unreadable_patch_content_stops_execution(failure):
    class Broken(MemoryStore):
        def patch_content(self, patch_sha256):
            raise failure
    a = make_action()
    r = rig(store=Broken().add(a, [decide(a, "priya")]))
    res = r.run(a.action_id)
    assert res.outcome == Outcome.REFUSED and r.release.calls == [] and "can't be read" in res.reason


def test_st13_a_fix_with_no_patch_at_all_stops_execution():
    a = make_action(patch_sha256=None)
    r = rig((a, [decide(a, "priya")]))
    assert r.run(a.action_id).outcome == Outcome.REFUSED and r.release.calls == []


# ---------------------------------------------------------------- ST-14
@pytest.mark.parametrize("extra", [{"patch": "x"}, {"patch_sha256": "f" * 64}, {"target": "checkout-prod-us1"},
                                   {"target_service": "payments"}, {"files": []}, {"base_revision": "ffffffff"}])
@pytest.mark.parametrize("contract,body", [
    (ApprovalCreate, {"action_digest": "a" * 64}),
    (RejectionCreate, {"action_digest": "a" * 64, "reason": "Wrong target entirely"}),
    (RevocationCreate, {"approval_id": str(uuid.uuid4()), "action_digest": "a" * 64, "reason": "Wrong target entirely"}),
])
def test_st14_decision_request_carrying_a_patch_or_target_is_rejected_whole(contract, body, extra):
    contract.model_validate(body)
    with pytest.raises(ValidationError):
        contract.model_validate({**body, **extra})


def test_st14_the_approval_core_has_no_way_to_receive_a_patch_or_target():
    names = set(inspect.signature(record_decision).parameters)
    assert not {n for n in names if n in ("patch", "patch_sha256", "target", "deployment", "files")}


def test_st14_task_message_cannot_name_a_different_patch():
    with pytest.raises(TypeError):
        DispatchIn("a1", patch_sha256="f" * 64)


def test_st14_dispatcher_sends_only_what_trusted_storage_holds():
    a = make_action()
    r = rig((a, [decide(a, "priya")]))
    r.run(a.action_id)
    assert r.release.calls == [dict(deployment=a.target.deployment, key=a.action_id, base=a.base_revision,
                                    patch=a.patch_sha256, to=a.expected_after_revision)]


# ---------------------------------------------------------------- ST-15
def test_st15_deployment_moved_off_the_base_revision_is_refused():
    a = make_action()
    r = rig((a, [decide(a, "priya")]), release=FakeRelease(revision="c0ffee00"))
    res = r.run(a.action_id)
    assert res.outcome == Outcome.REFUSED and r.release.calls == [] and "c0ffee00" in res.reason


def test_st15_move_between_check_and_write_is_caught_by_the_release_compare_and_set():
    class MovesAfterCheck(FakeRelease):
        async def apply(self, *args):
            self.revision = "c0ffee00"
            return await super().apply(*args)
    a = make_action()
    r = rig((a, [decide(a, "priya")]), release=MovesAfterCheck())
    assert r.run(a.action_id).outcome == Outcome.NOT_APPLIED and r.release.applied_keys == {}


def test_st15_a_new_commit_is_a_new_action_nobody_has_approved():
    p1, p2 = make_patch(), make_patch(base_revision="ffff0000")
    assert p1.digest != p2.digest
    a = make_action(patch_sha256=p1.digest)
    b = make_action(action_id=a.action_id, patch_sha256=p2.digest, base_revision="ffff0000")
    assert a.digest != b.digest
    r = rig((b, [decide(a, "priya")]), release=FakeRelease(revision="ffff0000"))
    assert r.run(a.action_id).outcome == Outcome.REFUSED


def test_st15_review_lock_needs_a_review_system():
    pytest.skip("The review lock lives in the review system; no reference module talks to one")
