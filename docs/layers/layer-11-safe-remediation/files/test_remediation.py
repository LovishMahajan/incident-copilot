"""Tests for safe remediation: patch identity and boundaries, sandbox spec and result classification,
approvals and authorization, the dispatcher and its ledger, verification and rollback.
Run: python -m pytest -q test_remediation.py
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from action import (ApprovalError, ApprovalPolicy, Directory, ImmutableAction, Role, Signer, Standing, Target,
                    evaluate, record_decision)
from dispatcher import Dispatcher, Ledger, LedgerState, Outcome, TargetState
from patch import DEFAULT_POLICY, PatchRejected, build_patch_from_contents, check_boundaries
from sandbox import (ResultBundle, SandboxSpec, SpecRejected, SuiteResult, TestOutcome, classify, launch_args,
                     validate_args)
from verification import Sample, Verdict, reference_plan, resolution_allowed, rollback_action_fields, verify

T0 = datetime(2026, 11, 1, 9, 0)
OLD = {"config/checkout/payments.yaml": "payments:\n  timeout_ms: 0\n  retries: 2\n"}
NEW = {"config/checkout/payments.yaml": "payments:\n  timeout_ms: 3000\n  retries: 2\n"}
META = dict(incident_id="inc-1", diagnosis_id="d1", candidate_no=1, action_kind="config_revert",
            repository="git@internal:shop/checkout.git", base_revision="a1b2c3d4", target_service="checkout",
            target_environment="production", summary="Revert payments.timeout_ms to its previous value")


def patch(**kw):
    return build_patch_from_contents(old=kw.pop("old", OLD), new=kw.pop("new", NEW),
                                     rationale="timeout_ms=0 makes every payment call fail immediately",
                                     evidence=["evidence:trace-1"], **{**META, **kw})


# ---------------------------------------------------------------- patch
def test_patch_is_structured_diff_with_rationale():
    p = patch()
    d = p.unified_diff()
    assert "-  timeout_ms: 0" in d and "+  timeout_ms: 3000" in d and "# why:" in d
    assert p.files[0].old_sha256 == hashlib.sha256(OLD["config/checkout/payments.yaml"].encode()).hexdigest()


def test_same_content_different_revision_is_a_different_patch():
    assert patch().digest != patch(base_revision="ffff0000").digest
    assert patch().digest == patch(summary="Reworded summary but same change").digest


def test_any_content_change_changes_digest():
    other = {"config/checkout/payments.yaml": "payments:\n  timeout_ms: 2999\n  retries: 2\n"}
    assert patch().digest != patch(new=other).digest


@pytest.mark.parametrize("path,code", [
    ("tests/test_payments.py", "protected_path"), ("src/app/conftest.py", "protected_path"),
    ("security/policy.rego", "protected_path"), ("sandbox/launcher.py", "protected_path"),
    (".github/workflows/ci.yml", "protected_path"), ("src/app/payments.py", "outside_policy"),
    ("../etc/passwd", "bad_path")])
def test_protected_and_out_of_policy_paths_rejected(path, code):
    p = patch(old={path: "a\n"}, new={path: "b\n"})
    with pytest.raises(PatchRejected) as e:
        check_boundaries(p)
    assert e.value.code == code


def test_config_revert_within_policy_passes():
    check_boundaries(patch())


def test_candidate_limit():
    with pytest.raises(Exception):
        patch(candidate_no=4)


# ---------------------------------------------------------------- sandbox
def test_launch_args_are_locked_down():
    a = launch_args(SandboxSpec(), "run1", "ws-run1")
    j = " ".join(a)
    for flag in ("--runtime=runsc", "--network=none", "--read-only", "--cap-drop=ALL", "--pids-limit=256",
                 "--memory=2g", "--memory-swap=2g", "--cpus=2.0", "no-new-privileges", "--user=65534:65534"):
        assert flag in j
    assert "docker.sock" not in j


@pytest.mark.parametrize("extra", ["--privileged", "-v=/var/run/docker.sock:/var/run/docker.sock",
                                   "--network=host", "--env=DATABASE_URL=postgres://prod", "--user=0:0",
                                   "--cap-add=SYS_ADMIN"])
def test_dangerous_options_refused(extra):
    a = launch_args(SandboxSpec(), "run1", "ws")
    with pytest.raises(SpecRejected):
        validate_args(a[:-2] + [extra] + a[-2:])


def bundle(p, **kw):
    base = dict(run_id="r1", patch_digest=p.digest, base_revision="a1b2c3d4", image_digest="sha256:img",
                spec_version="sandbox-spec@1", harness_version="harness@3", reproduction_before=True,
                reproduction_after=False, suites=(SuiteResult("reproduction", True, True, 1, 0),
                                                  SuiteResult("unit", True, True, 120, 0),
                                                  SuiteResult("integration", True, True, 14, 0)),
                new_error_signatures=(), exit_code=0, duration_s=88.0, timed_out=False, log_artifact_sha256="0" * 64)
    base.update(kw)
    return ResultBundle(**base)


def test_classification():
    p = patch()
    assert classify(bundle(p), p.digest) == TestOutcome.PASSED
    assert classify(bundle(p, reproduction_before=False), p.digest) == TestOutcome.NOT_REPRODUCED
    assert classify(bundle(p, reproduction_after=True), p.digest) == TestOutcome.FAILED
    assert classify(bundle(p, new_error_signatures=("KeyError in refunds",)), p.digest) == TestOutcome.FAILED
    assert classify(bundle(p, timed_out=True), p.digest) == TestOutcome.TIMEOUT
    assert classify(bundle(p, exit_code=None), p.digest) == TestOutcome.CRASHED
    assert classify(None, p.digest) == TestOutcome.CRASHED
    assert classify(bundle(p, suites=(SuiteResult("integration", True, False),)), p.digest) == TestOutcome.INCONCLUSIVE
    assert classify(bundle(p, patch_digest="f" * 64), p.digest) == TestOutcome.CRASHED   # results for another patch


# ---------------------------------------------------------------- approvals
KEY = Ed25519PrivateKey.generate()
SIGNER = Signer("k1", KEY)
KEYS = {"k1": KEY.public_key()}
POLICY = ApprovalPolicy()


def action(high_impact=False, **kw):
    p = patch()
    base = dict(action_id=str(uuid.uuid4()), incident_id="inc-1", diagnosis_id="d1", kind="apply_patch",
                patch_digest=p.digest, base_revision="a1b2c3d4", expected_after_revision="e5f6a7b8",
                target=Target("checkout", "production", "checkout-prod-eu1"),
                policy_version=f"{DEFAULT_POLICY.policy_version}|{POLICY.version}",
                test_bundle_digest=bundle(p).digest, recovery_predicates_digest=reference_plan("checkout", "/pay").digest,
                high_impact=high_impact)
    base.update(kw)
    return ImmutableAction(**base)


def directory():
    return Directory({("priya", "checkout"): {Role.APPROVER}, ("sam", "checkout"): {Role.INCIDENT_LEAD},
                      ("ravi", "payments"): {Role.APPROVER}, ("admin", "checkout"): {Role.PLATFORM_ADMIN},
                      ("vee", "checkout"): {Role.RESPONDER}})


def decide(a, subject, decision="approve", now=T0, d=None, prior=(), **kw):
    return record_decision(action=a, subject=subject, decision=decision, auth_time=now - timedelta(minutes=2),
                           mfa=True, now=now, directory=d or directory(), policy=POLICY, signer=SIGNER,
                           approval_id=str(uuid.uuid4()), presented_digest=kw.pop("digest", a.digest),
                           proposer_subjects={"system:workflow"}, prior=list(prior), **kw)


def test_approval_binds_to_action_digest_and_is_signed():
    a = action()
    d = decide(a, "priya")
    assert d.action_digest == a.digest
    assert evaluate(a, [d], now=T0, keys=KEYS, directory=directory(), policy=POLICY, requested_at=T0)[0] == Standing.APPROVED


def test_stale_view_refused():
    with pytest.raises(ApprovalError) as e:
        decide(action(), "priya", digest="0" * 64)
    assert e.value.code == "digest_mismatch"


@pytest.mark.parametrize("subject,code", [("system:workflow", "self_approval"), ("ravi", "forbidden"),
                                          ("admin", "forbidden"), ("vee", "forbidden")])
def test_who_cannot_approve(subject, code):
    with pytest.raises(ApprovalError) as e:
        decide(action(), subject)
    assert e.value.code == code


def test_old_sign_in_refused():
    a = action()
    with pytest.raises(ApprovalError) as e:
        record_decision(action=a, subject="priya", decision="approve", auth_time=T0 - timedelta(hours=1), mfa=True,
                        now=T0, directory=directory(), policy=POLICY, signer=SIGNER, approval_id="x",
                        presented_digest=a.digest, proposer_subjects=set())
    assert e.value.code == "reauth_required"


def test_tampered_or_changed_action_invalidates_approval():
    a = action()
    d = decide(a, "priya")
    changed = action(action_id=a.action_id, patch_digest="f" * 64)        # same ID, different content
    assert evaluate(changed, [d], now=T0, keys=KEYS, directory=directory(), policy=POLICY, requested_at=T0)[0] == Standing.PENDING
    forged = d.__class__(**{**d.__dict__, "subject": "mallory"})
    assert evaluate(a, [forged], now=T0, keys=KEYS, directory=directory(), policy=POLICY, requested_at=T0)[0] == Standing.PENDING


def test_rejection_wins_over_approval():
    a = action()
    ok = decide(a, "priya")
    no = decide(a, "sam", decision="reject", reason="touches the wrong key")
    assert evaluate(a, [ok, no], now=T0, keys=KEYS, directory=directory(), policy=POLICY, requested_at=T0)[0] == Standing.REJECTED


def test_high_impact_needs_two_people_and_revocation_counts():
    a = action(high_impact=True)
    one = decide(a, "priya")
    st = lambda ds: evaluate(a, ds, now=T0, keys=KEYS, directory=directory(), policy=POLICY, requested_at=T0)[0]
    assert st([one]) == Standing.PENDING
    two = decide(a, "sam")
    assert st([one, two]) == Standing.APPROVED
    rev = decide(a, "priya", decision="revoke", revokes=one.approval_id, prior=[one, two])
    assert st([one, two, rev]) == Standing.PENDING


def test_lost_permission_and_policy_change_and_expiry():
    a = action()
    d = decide(a, "priya")
    gone = Directory({("sam", "checkout"): {Role.INCIDENT_LEAD}})
    assert evaluate(a, [d], now=T0, keys=KEYS, directory=gone, policy=POLICY, requested_at=T0)[0] == Standing.PENDING
    newpol = ApprovalPolicy(version="approval-policy@2")
    assert evaluate(a, [d], now=T0, keys=KEYS, directory=directory(), policy=newpol, requested_at=T0)[0] == Standing.PENDING
    late = T0 + timedelta(minutes=16)
    assert evaluate(a, [d], now=late, keys=KEYS, directory=directory(), policy=POLICY, requested_at=T0)[0] == Standing.PENDING
    assert evaluate(a, [], now=T0 + timedelta(minutes=61), keys=KEYS, directory=directory(), policy=POLICY,
                    requested_at=T0)[0] == Standing.EXPIRED


# ---------------------------------------------------------------- dispatcher
class Store:
    def __init__(self, a, ds):
        self.a, self.ds = a, ds

    def action(self, _):
        return self.a

    def decisions(self, _):
        return self.ds

    def requested_at(self, _):
        return T0


class FakeRelease:
    """Keeps its own action-key ledger (Q11.72) and can lose responses."""

    def __init__(self, revision="a1b2c3d4", lose_response=False, apply_on_lost=True):
        self.revision, self.lose, self.apply_on_lost = revision, lose_response, apply_on_lost
        self.processed: dict[str, str] = {}
        self.apply_calls = 0

    async def current_state(self, deployment):
        return TargetState(self.revision, None)

    async def apply(self, deployment, key, base, digest, to_rev):
        self.apply_calls += 1
        if key in self.processed:
            return self.processed[key]
        if base != self.revision:
            return "rejected"
        if self.lose:
            if self.apply_on_lost:
                self.revision, self.processed[key] = to_rev, "applied"
            await asyncio.sleep(1)
        self.revision, self.processed[key] = to_rev, "applied"
        return "applied"

    async def lookup(self, deployment, key):
        return self.processed.get(key)


def disp(a, ds, rel, ledger=None):
    return Dispatcher(Store(a, ds), rel, ledger or Ledger(), KEYS, directory(), POLICY, clock=lambda: T0 + timedelta(minutes=1),
                      call_timeout_s=0.05)


def test_dispatch_applies_once_and_is_idempotent():
    a = action()
    rel, led = FakeRelease(), Ledger()
    d = disp(a, [decide(a, "priya")], rel, led)
    assert asyncio.run(d.execute(a.action_id)).outcome == Outcome.APPLIED
    assert asyncio.run(d.execute(a.action_id)).outcome == Outcome.APPLIED      # retry: no second write
    assert rel.apply_calls == 1


def test_dispatch_refuses_without_approval_and_on_revision_drift():
    a = action()
    assert asyncio.run(disp(a, [], FakeRelease()).execute(a.action_id)).outcome == Outcome.REFUSED
    r = asyncio.run(disp(a, [decide(a, "priya")], FakeRelease(revision="99999999")).execute(a.action_id))
    assert r.outcome == Outcome.REFUSED and "approved for a1b2c3d4" in r.reason


def test_lost_response_is_reconciled_not_retried():
    a = action()
    rel, led = FakeRelease(lose_response=True, apply_on_lost=True), Ledger()
    r = asyncio.run(disp(a, [decide(a, "priya")], rel, led).execute(a.action_id))
    assert r.outcome == Outcome.APPLIED and r.reason == "reconciled: applied" and rel.apply_calls == 1
    assert led.get(a.action_id).state == LedgerState.APPLIED


def test_lost_request_reconciled_as_not_applied():
    a = action()
    rel = FakeRelease(lose_response=True, apply_on_lost=False)

    async def never(*args):
        rel.apply_calls += 1
        await asyncio.sleep(1)
    rel.apply = never
    r = asyncio.run(disp(a, [decide(a, "priya")], rel).execute(a.action_id))
    assert r.outcome == Outcome.NOT_APPLIED and rel.apply_calls == 1


def test_unknown_outcome_blocks_resend():
    a = action()
    led = Ledger()
    rel = FakeRelease()

    async def lookup(deployment, key):
        return "applied"                                  # release says applied...
    rel.lookup = lookup                                   # ...but the live revision is still the base: disagreement
    led.put(__import__("dispatcher").LedgerEntry(a.action_id, a.digest, LedgerState.UNKNOWN))
    r = asyncio.run(disp(a, [decide(a, "priya")], rel, led).execute(a.action_id))
    assert r.outcome == Outcome.UNKNOWN and rel.apply_calls == 0


# ---------------------------------------------------------------- verification and rollback
PLAN = reference_plan("checkout", "/api/pay")
BASE = {"http_5xx_rate_path": 0.002, "http_5xx_rate": 0.001, "http_latency_p95_ms": 220.0}


def samples(n=10, **override):
    vals = {"http_5xx_rate_path": 0.002, "http_5xx_rate": 0.001, "http_latency_p95_ms": 230.0,
            "synthetic_probe_success_ratio": 1.0, "http_success_ratio": 0.999, "new_error_group_count": 0}
    out = []
    for i in range(n):
        v = dict(vals)
        for k, fn in override.items():
            v[k] = fn(i)
        out.append(Sample(i * 60, 400, v))
    return out


def test_verified_only_when_everything_holds_for_the_window():
    assert verify(PLAN, BASE, samples()).verdict == Verdict.VERIFIED


def test_relapse_inside_window_fails():
    r = verify(PLAN, BASE, samples(http_5xx_rate_path=lambda i: 0.002 if i < 2 else 0.2))
    assert r.verdict == Verdict.FAILED and "error_rate_failing_path" in r.reasons[0]


def test_latency_regression_fails_even_if_errors_fixed():
    assert verify(PLAN, BASE, samples(http_latency_p95_ms=lambda i: 900.0)).verdict == Verdict.FAILED


def test_new_error_group_fails():
    assert verify(PLAN, BASE, samples(new_error_group_count=lambda i: 1 if i == 7 else 0)).verdict == Verdict.FAILED


def test_too_little_traffic_or_too_short_is_inconclusive():
    low = [Sample(s.t, 5, s.values) for s in samples()]
    assert verify(PLAN, BASE, low).verdict == Verdict.INCONCLUSIVE
    assert verify(PLAN, BASE, samples(n=3)).verdict == Verdict.INCONCLUSIVE


def test_rollback_is_linked_and_resolution_needs_all_conditions():
    f = rollback_action_fields("act-1", "a1b2c3d4", "e5f6a7b8")
    assert f["parent_action_id"] == "act-1" and f["expected_after_revision"] == "a1b2c3d4"
    assert resolution_allowed(Verdict.VERIFIED, True, 0)[0]
    assert not resolution_allowed(Verdict.INCONCLUSIVE, True, 0)[0]
    assert not resolution_allowed(Verdict.VERIFIED, False, 0)[0]
    assert not resolution_allowed(Verdict.VERIFIED, True, 1)[0]
