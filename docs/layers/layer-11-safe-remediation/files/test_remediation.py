"""Tests for safe remediation: patch identity and boundaries, sandbox spec and result classification,
approvals and authorization, the dispatcher and its ledger, verification and rollback.
Run: python -m pytest -q test_remediation.py
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import subprocess
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from action import (ApprovalError, ApprovalPolicy, Directory, ImmutableAction, MATRIX, Perm, Role, Signer, Standing,
                    Target, evaluate, record_decision)
from dispatcher import (Dispatcher, Ledger, LedgerEntry, LedgerState, Outcome, ReleaseInProgress, TargetState)
from patch import DEFAULT_POLICY, EditPolicy, PatchRejected, build_patch_from_contents, check_boundaries
from sandbox import (REQUIRED_SUITES, ResultBundle, SandboxSpec, SpecRejected, SuiteResult, TestOutcome, classify,
                     launch_args, read_bundle, reap, run_sandbox, validate_args, volume_args)
from verification import (Predicate, Sample, Verdict, VerificationPlan, baseline, reference_plan, resolution_allowed,
                          rollback_action_fields, verify)

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


COMMIT = dict(action_kind="commit_revert", reverted_commit="c0ffee12")


def edit(path, **kw):
    return patch(old={path: "a\n"}, new={path: "b\n"}, **kw)


def test_editable_globs_reach_the_root_of_each_folder():
    check_boundaries(edit("config/app.yaml"))
    check_boundaries(edit("config/checkout/payments.toml"))
    check_boundaries(edit("src/app.py", **COMMIT), commit_paths=["src/app.py"])
    check_boundaries(edit("src/app/orders.py", **COMMIT), commit_paths=["src/app/orders.py"])


@pytest.mark.parametrize("path", ["src/config/x/a.yaml", "vendor/config/a.yaml"])
def test_editable_globs_are_anchored_at_the_root(path):
    with pytest.raises(PatchRejected) as e:
        check_boundaries(edit(path))
    assert e.value.code == "outside_policy"


@pytest.mark.parametrize("path", [".env", "poetry.lock", "test_x.py", "key.pem", "secrets.yaml", "x_test.py", "a.key",
                                  "policy.rego", "src/deep/.env", "src/ci/run.py", "src/Makefile"])
def test_protected_paths_at_any_depth_whatever_the_policy_says(path):
    wide_open = EditPolicy(policy_version="edit-policy@9", allowed={"config_revert": ["*"]})
    with pytest.raises(PatchRejected) as e:
        check_boundaries(edit(path), wide_open)
    assert e.value.code == "protected_path"


def test_commit_revert_stays_inside_the_commit():
    p = edit("src/app.py", **COMMIT)
    check_boundaries(p, commit_paths=["src/app.py", "src/other.py"])
    with pytest.raises(PatchRejected) as e:
        check_boundaries(p, commit_paths=["src/other.py"])
    assert e.value.code == "outside_commit"
    with pytest.raises(PatchRejected) as e:
        check_boundaries(p)                                 # the list couldn't be read
    assert e.value.code == "commit_unreadable"


def test_reverted_commit_is_part_of_identity_and_only_for_commit_reverts():
    assert edit("src/app.py", **COMMIT).digest != edit("src/app.py", **{**COMMIT, "reverted_commit": "deadbeef"}).digest
    with pytest.raises(Exception):
        edit("src/app.py", action_kind="commit_revert")
    with pytest.raises(Exception):
        edit("config/app.yaml", reverted_commit="c0ffee12")


# ---------------------------------------------------------------- sandbox
def test_launch_args_are_locked_down():
    a = launch_args(SandboxSpec(), "run1", "ws-run1")
    j = " ".join(a)
    for flag in ("--runtime=runsc", "--network=none", "--read-only", "--cap-drop=ALL", "--pids-limit=256",
                 "--memory=2g", "--memory-swap=2g", "--cpus=2.0", "no-new-privileges", "--user=65534:65534"):
        assert flag in j
    assert "docker.sock" not in j


def test_launch_args_hold_only_what_docker_accepts():
    a = launch_args(SandboxSpec(), "run1", "ws-run1")
    assert "--pid=private" not in a and "--uts=private" not in a       # Docker: invalid PID mode, invalid UTS mode
    assert "--ipc=private" in a and "--stop-timeout=10" in a


@pytest.mark.parametrize("net", ["--network=bridge", "--network=container:x", "--net=host", "--network"])
def test_only_network_none_is_accepted(net):
    a = launch_args(SandboxSpec(), "run1", "ws")
    with pytest.raises(SpecRejected):
        validate_args([net if x == "--network=none" else x for x in a])
    with pytest.raises(SpecRejected):
        validate_args([x for x in a if x != "--network=none"])


def test_spec_has_no_package_mirror():
    assert "package_mirror" not in {f.name for f in dataclasses.fields(SandboxSpec)}


def test_workspace_volume_is_labelled_and_size_limited():
    a = volume_args(SandboxSpec(workspace_mb=512), "run1")
    assert a[:3] == ["docker", "volume", "create"] and a[-1] == "aisre-ws-run1"
    assert "--opt=type=tmpfs" in a and "--opt=o=size=512m,uid=65534,gid=65534,nosuid,nodev" in a
    assert "--label=aisre.sandbox=1" in a and "--label=aisre.run=run1" in a


@pytest.mark.parametrize("extra", ["--privileged", "-v=/var/run/docker.sock:/var/run/docker.sock",
                                   "--network=host", "--env=DATABASE_URL=postgres://prod", "--user=0:0",
                                   "--cap-add=SYS_ADMIN"])
def test_dangerous_options_refused(extra):
    a = launch_args(SandboxSpec(), "run1", "ws")
    with pytest.raises(SpecRejected):
        validate_args(a[:-2] + [extra] + a[-2:])


def bundle(p, **kw):
    base = dict(run_id="r1", patch_sha256=p.digest, base_revision="a1b2c3d4", image_digest="sha256:img",
                spec_version="sandbox-spec@1", harness_version="harness@3", reproduction_before=True,
                reproduction_after=False, suites=(SuiteResult("reproduction", True, True, 1, 0),
                                                  SuiteResult("unit", True, True, 120, 0),
                                                  SuiteResult("integration", True, True, 14, 0),
                                                  SuiteResult("smoke", True, True, 6, 0),
                                                  SuiteResult("error_signature", True, True, 1, 0)),
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
    assert classify(bundle(p, patch_sha256="f" * 64), p.digest) == TestOutcome.CRASHED   # results for another patch


def without(name, p):
    return tuple(s for s in bundle(p).suites if s.name != name)


def replaced(p, **suite):
    return tuple(SuiteResult(**suite) if s.name == suite["name"] else s for s in bundle(p).suites)


def test_a_dead_harness_is_never_passed():
    p = patch()
    assert classify(bundle(p, exit_code=137), p.digest) == TestOutcome.CRASHED
    assert classify(bundle(p, exit_code=1), p.digest) == TestOutcome.CRASHED
    assert classify(bundle(p, timed_out=True, exit_code=137), p.digest) == TestOutcome.TIMEOUT   # timeout kill exits 137


def test_a_missing_reproduction_result_is_inconclusive():
    p = patch()
    assert classify(bundle(p, reproduction_before=None), p.digest) == TestOutcome.INCONCLUSIVE
    assert classify(bundle(p, reproduction_after=None), p.digest) == TestOutcome.INCONCLUSIVE
    assert classify(bundle(p, reproduction_before=False), p.digest) == TestOutcome.NOT_REPRODUCED


@pytest.mark.parametrize("name", REQUIRED_SUITES)
def test_every_required_suite_must_really_run(name):
    p = patch()
    assert classify(bundle(p, suites=without(name, p)), p.digest) == TestOutcome.INCONCLUSIVE           # absent
    assert classify(bundle(p, suites=replaced(p, name=name, required=True, ran=True)), p.digest) == TestOutcome.INCONCLUSIVE    # zero tests
    assert classify(bundle(p, suites=replaced(p, name=name, required=True, ran=False)), p.digest) == TestOutcome.INCONCLUSIVE   # didn't run


def test_the_bundle_cannot_excuse_a_required_suite():
    p = patch()
    assert classify(bundle(p, suites=()), p.digest) == TestOutcome.INCONCLUSIVE
    assert classify(bundle(p, suites=replaced(p, name="unit", required=False, ran=False)), p.digest) == TestOutcome.INCONCLUSIVE
    empty_twin = bundle(p).suites + (SuiteResult("unit", True, True),)
    assert classify(bundle(p, suites=empty_twin), p.digest) == TestOutcome.INCONCLUSIVE


class FakeProc:
    """A `docker run` client: hangs past the deadline if told to, then exits as the stop leaves it."""

    def __init__(self, out="", returncode=0, hang=False):
        self.out, self.returncode, self.hang, self.deadlines = out, returncode, hang, []

    def communicate(self, timeout=None):
        self.deadlines.append(timeout)
        if self.hang and len(self.deadlines) == 1:
            raise subprocess.TimeoutExpired("docker", timeout)
        return self.out, ""

    def kill(self):
        self.returncode = -9


def docker_recorder():
    calls = []

    def run(args, **kw):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")
    return calls, run


def test_wall_clock_ends_the_run_as_a_timeout_and_cleans_up():
    p, proc = patch(), FakeProc(returncode=137, hang=True)
    calls, run = docker_recorder()
    spec = SandboxSpec(wall_clock_s=900)
    b = run_sandbox(spec, "r1", p.digest, "a1b2c3d4", popen=lambda *a, **k: proc, run=run)
    assert proc.deadlines[0] == 900
    assert ["docker", "stop", "--time=10", "aisre-test-r1"] in calls
    assert b.timed_out and b.exit_code == 137 and b.patch_sha256 == p.digest
    assert classify(b, p.digest) == TestOutcome.TIMEOUT                # no bundle would have read as crashed
    assert calls[-2:] == [["docker", "rm", "--force", "aisre-test-r1"], ["docker", "volume", "rm", "--force", "aisre-ws-r1"]]


def test_a_stuck_stop_still_ends_the_run():
    p, proc = patch(), FakeProc(returncode=137, hang=True)
    _, recorded = docker_recorder()

    def run(args, **kw):
        if "stop" in args:
            raise subprocess.TimeoutExpired("docker stop", 30)
        return recorded(args, **kw)
    b = run_sandbox(SandboxSpec(), "r1", p.digest, "a1b2c3d4", popen=lambda *a, **k: proc, run=run)
    assert b.exit_code == -9 and classify(b, p.digest) == TestOutcome.TIMEOUT


def bundle_line(p, **kw):
    return json.dumps(dataclasses.asdict(bundle(p, **kw)))


def test_a_finished_run_returns_the_harness_bundle_with_dockers_exit_code():
    p = patch()
    calls, run = docker_recorder()
    b = run_sandbox(SandboxSpec(), "r1", p.digest, "a1b2c3d4",
                    popen=lambda *a, **k: FakeProc("log line\n" + bundle_line(p)), run=run)
    assert classify(b, p.digest) == TestOutcome.PASSED and calls[0][:3] == ["docker", "volume", "create"]
    assert b.digest == bundle(p).digest
    claimed_clean = read_bundle(bundle_line(p), 137)                   # the bundle says 0, Docker says 137
    assert classify(claimed_clean, p.digest) == TestOutcome.CRASHED


@pytest.mark.parametrize("out", ["", "not json", "[]", '{"run_id": "r1"}', '{"suites": [1], "new_error_signatures": []}'])
def test_unreadable_output_is_no_bundle(out):
    assert read_bundle(out, 0) is None


def test_cleanup_runs_when_the_run_fails():
    calls, run = docker_recorder()

    def popen(*a, **k):
        raise OSError("docker is gone")
    with pytest.raises(OSError):
        run_sandbox(SandboxSpec(), "r1", "0" * 64, "a1b2c3d4", popen=popen, run=run)
    assert ["docker", "volume", "rm", "--force", "aisre-ws-r1"] in calls


def test_reaper_removes_only_what_is_old_and_counts_it():
    now = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    removed = []

    def run(args, **kw):
        out = ""
        if args[1:2] == ["ps"]:
            assert "label=aisre.sandbox=1" in args
            out = "c1\nc2\n"
        elif args[1:3] == ["volume", "ls"]:
            assert "label=aisre.sandbox=1" in args
            out = "v1\nv2\n"
        elif args[1:2] == ["inspect"]:
            out = "c1 2026-11-02T10:30:00.123456789Z\nc2 2026-11-02T11:30:00.123456789Z\n"
        elif args[1:3] == ["volume", "inspect"]:
            out = "v1 2026-11-02T10:59:59Z\nv2 2026-11-02T11:01:00Z\n"
        else:
            removed.append(args[1:])
        return subprocess.CompletedProcess(args, 0, out, "")
    assert reap(now, run=run) == 2
    assert removed == [["rm", "--force", "c1"], ["volume", "rm", "--force", "v1"]]


def test_reaper_with_nothing_labelled():
    assert reap(datetime(2026, 11, 2, tzinfo=timezone.utc),
                run=lambda args, **kw: subprocess.CompletedProcess(args, 0, "", "")) == 0


# ---------------------------------------------------------------- approvals
KEY = Ed25519PrivateKey.generate()
SIGNER = Signer("k1", KEY)
KEYS = {"k1": KEY.public_key()}
POLICY = ApprovalPolicy()


def action(high_impact=False, **kw):
    p = patch()
    base = dict(action_id=str(uuid.uuid4()), incident_id="inc-1", diagnosis_id="d1", kind="config_revert",
                patch_sha256=p.digest, base_revision="a1b2c3d4", expected_after_revision="e5f6a7b8",
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


def rows(a, subject, decision="approved", now=T0, d=None, prior=(), **kw):
    state = dict(action_status="awaiting_approval", request_open=True, intent_recorded=False, events=[],
                 auth_time=now - timedelta(minutes=2), mfa=True, policy=POLICY, approval_id=str(uuid.uuid4()),
                 proposer_subjects={"system:workflow"})
    state.update(kw)
    return record_decision(action=a, subject=subject, decision=decision, now=now, directory=d or directory(),
                           signer=SIGNER, presented_digest=state.pop("digest", a.digest), prior=list(prior), **state)


def decide(*args, **kw):
    return rows(*args, **kw)[0]


def refused(code, *args, **kw):
    events = kw.setdefault("events", [])
    with pytest.raises(ApprovalError) as e:
        rows(*args, **kw)
    assert e.value.code == code
    assert [x["data"]["code"] for x in events] == [code]
    return events[0]


def test_approval_binds_to_action_digest_and_is_signed():
    a = action()
    d = decide(a, "priya")
    assert d.action_digest == a.digest
    assert evaluate(a, [d], now=T0, keys=KEYS, directory=directory(), policy=POLICY, requested_at=T0)[0] == Standing.APPROVED


def test_stale_view_refused():
    with pytest.raises(ApprovalError) as e:
        decide(action(), "priya", digest="0" * 64)
    assert e.value.code == "digest_mismatch"


@pytest.mark.parametrize("subject,code", [("system:workflow", "self_approval"), ("ravi", "not_found"),
                                          ("admin", "forbidden"), ("vee", "forbidden")])
def test_who_cannot_approve(subject, code):
    with pytest.raises(ApprovalError) as e:
        decide(action(), subject)
    assert e.value.code == code


def test_old_sign_in_refused():
    a = action()
    with pytest.raises(ApprovalError) as e:
        record_decision(action=a, subject="priya", decision="approved", auth_time=T0 - timedelta(hours=1), mfa=True,
                        now=T0, directory=directory(), policy=POLICY, signer=SIGNER, approval_id="x",
                        presented_digest=a.digest, proposer_subjects=set(), action_status="awaiting_approval",
                        request_open=True, intent_recorded=False, events=[])
    assert e.value.code == "reauth_required"


def test_tampered_or_changed_action_invalidates_approval():
    a = action()
    d = decide(a, "priya")
    changed = action(action_id=a.action_id, patch_sha256="f" * 64)        # same ID, different content
    assert evaluate(changed, [d], now=T0, keys=KEYS, directory=directory(), policy=POLICY, requested_at=T0)[0] == Standing.PENDING
    forged = d.__class__(**{**d.__dict__, "subject": "mallory"})
    assert evaluate(a, [forged], now=T0, keys=KEYS, directory=directory(), policy=POLICY, requested_at=T0)[0] == Standing.PENDING


def test_rejection_wins_over_approval():
    a = action()
    ok = decide(a, "priya")
    no = decide(a, "sam", decision="rejected", reason="touches the wrong key")
    assert evaluate(a, [ok, no], now=T0, keys=KEYS, directory=directory(), policy=POLICY, requested_at=T0)[0] == Standing.REJECTED


def test_high_impact_needs_two_people_and_revocation_counts():
    a = action(high_impact=True)
    one = decide(a, "priya")
    st = lambda ds: evaluate(a, ds, now=T0, keys=KEYS, directory=directory(), policy=POLICY, requested_at=T0)[0]
    assert st([one]) == Standing.PENDING
    two = decide(a, "sam")
    assert st([one, two]) == Standing.APPROVED
    rev = decide(a, "priya", decision="revoked", revokes=one.approval_id, prior=[one, two], reason="wrong target")
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


def test_action_kinds_and_required_fields():
    with pytest.raises(ValueError):
        action(kind="apply_patch")                          # retired (ADR-64)
    with pytest.raises(ValueError):
        action(expected_after_revision=None)                # no second source, so reconcile could never agree (C17)
    with pytest.raises(ValueError):
        action(kind="rollback")                             # a rollback names its parent
    assert action().hold_seconds == 600 and action(hold_seconds=1800).digest != action(hold_seconds=600).digest


def test_rejection_is_a_cheap_safety_stop():
    a, stale = action(), T0 - timedelta(hours=8)
    d = decide(a, "vee", decision="rejected", reason="wrong target", auth_time=stale, mfa=False)
    assert d.decision == "rejected" and d.valid_until is None


def test_revocation_needs_recent_sign_in_and_a_reason():
    a = action()
    ok = decide(a, "priya")
    args = dict(decision="revoked", revokes=ok.approval_id, prior=[ok])
    refused("reauth_required", a, "priya", auth_time=T0 - timedelta(hours=1), reason="changed my mind", **args)
    refused("reason_required", a, "priya", **args)
    assert decide(a, "priya", reason="changed my mind", **args).decision == "revoked"


def test_platform_identities_are_forbidden_rejecting_or_revoking_but_a_proposer_person_may_reject():
    a = action()
    refused("forbidden", a, "system:workflow", decision="rejected", reason="no")
    ok = decide(a, "priya")
    refused("forbidden", a, "system:dispatcher", decision="revoked", revokes=ok.approval_id, prior=[ok], reason="no")
    assert decide(a, "priya", decision="rejected", reason="not this one", proposer_subjects={"priya"}).decision == "rejected"
    refused("self_approval", a, "priya", proposer_subjects={"priya"})


def test_rights_are_checked_before_the_digest():
    refused("forbidden", action(), "vee", digest="0" * 64)
    refused("not_found", action(), "ravi", digest="0" * 64)


def test_only_someone_elses_or_missing_approval_cannot_be_revoked():
    a = action()
    ok = decide(a, "priya")
    refused("not_own_approval", a, "sam", decision="revoked", revokes=ok.approval_id, prior=[ok], reason="no")
    refused("not_own_approval", a, "priya", decision="revoked", revokes="nope", prior=[ok], reason="no")


@pytest.mark.parametrize("status,request_open", [("proposed", True), ("testing", True), ("approved", True),
                                                 ("applying", True), ("rejected", True), ("expired", True),
                                                 ("awaiting_approval", False)])
def test_approval_needs_an_open_request_on_an_action_awaiting_approval(status, request_open):
    refused("action_not_open", action(), "priya", action_status=status, request_open=request_open)     # ST-12


@pytest.mark.parametrize("status,open_", [("awaiting_approval", True), ("approved", True), ("proposed", False),
                                          ("executed", False), ("expired", False), ("withdrawn", False)])
def test_who_can_still_reject(status, open_):
    a = action()
    args = dict(decision="rejected", reason="not this", action_status=status)
    if open_:
        assert decide(a, "vee", **args).decision == "rejected"
    else:
        refused("action_not_open", a, "vee", **args)


def test_nothing_can_be_stopped_after_intent():
    a = action()
    ok = decide(a, "priya")
    state = dict(action_status="applying", intent_recorded=True, reason="too late")
    refused("execution_started", a, "vee", decision="rejected", **state)
    refused("execution_started", a, "priya", decision="revoked", revokes=ok.approval_id, prior=[ok], **state)


def test_repeats_are_duplicates():
    a = action()
    ok = decide(a, "priya")
    refused("duplicate", a, "priya", prior=[ok])
    rev = decide(a, "priya", decision="revoked", revokes=ok.approval_id, prior=[ok], reason="wrong")
    refused("duplicate", a, "priya", decision="revoked", revokes=ok.approval_id, prior=[ok, rev], reason="again")


def test_nobody_is_sole_approver_of_a_revert_of_their_own_change():
    a, hi = action(), action(high_impact=True)
    refused("self_approval", a, "priya", change_author="priya")                      # ST-07
    assert decide(a, "sam", change_author="priya").subject == "sam"
    one = decide(hi, "priya", change_author="priya")                                 # the other must be someone else
    st = lambda ds: evaluate(hi, ds, now=T0, keys=KEYS, directory=directory(), policy=POLICY, requested_at=T0)[0]
    assert st([one]) == Standing.PENDING and st([one, decide(hi, "sam", change_author="priya")]) == Standing.APPROVED


def test_every_refusal_writes_the_event_and_no_row():
    a = action()
    ok = decide(a, "priya")
    alert = lambda e: e["data"]["security_alert"]
    e = refused("digest_mismatch", a, "priya", digest="0" * 64)
    assert e["type"] == "aisre.approval.refused.v1" and e["data"]["action_id"] == a.action_id
    assert e["data"]["decision"] == "approved" and alert(e)
    assert alert(refused("reauth_required", a, "priya", mfa=False))
    assert alert(refused("self_approval", a, "system:workflow"))
    assert alert(refused("forbidden", a, "vee"))
    assert alert(refused("not_found", a, "ravi"))
    assert not alert(refused("duplicate", a, "priya", prior=[ok]))
    assert not alert(refused("action_not_open", action(), "priya", action_status="expired"))
    assert not alert(refused("reason_required", a, "vee", decision="rejected"))


def test_decisions_record_both_policies_and_a_change_to_either_voids_them():
    a = action()
    d = decide(a, "priya")
    assert d.policy_version == f"{DEFAULT_POLICY.policy_version}|{POLICY.version}" == a.policy_version
    other = ApprovalPolicy(edit_version="edit-policy@2")
    assert evaluate(a, [d], now=T0, keys=KEYS, directory=directory(), policy=other, requested_at=T0)[0] == Standing.PENDING


def test_all_four_approval_timers_have_one_home():
    assert (POLICY.escalates, POLICY.request_expires, POLICY.valid_for, POLICY.max_auth_age) == (
        timedelta(minutes=15), timedelta(minutes=60), timedelta(minutes=15), timedelta(minutes=15))


ROWS = {                                                    # ADR-50, Artifact 11.7
    Perm.READ: {Role.VIEWER, Role.RESPONDER, Role.APPROVER, Role.INCIDENT_LEAD, Role.PLATFORM_ADMIN,
                Role.SYSTEM_WORKFLOW, Role.SYSTEM_DISPATCHER},
    Perm.CREATE: {Role.RESPONDER, Role.APPROVER, Role.INCIDENT_LEAD, Role.MONITORING, Role.SYSTEM_WORKFLOW},
    Perm.NOTE: {Role.RESPONDER, Role.APPROVER, Role.INCIDENT_LEAD},
    Perm.CANCEL: {Role.RESPONDER, Role.APPROVER, Role.INCIDENT_LEAD},
    Perm.REJECT: {Role.RESPONDER, Role.APPROVER, Role.INCIDENT_LEAD},
    Perm.REINVESTIGATE: {Role.RESPONDER, Role.APPROVER, Role.INCIDENT_LEAD},
    Perm.APPROVE: {Role.APPROVER, Role.INCIDENT_LEAD},
    Perm.REVOKE_OWN: {Role.APPROVER, Role.INCIDENT_LEAD},
    Perm.CONFIRM_SEVERITY: {Role.INCIDENT_LEAD},
    Perm.CLOSE: {Role.INCIDENT_LEAD},
    Perm.AUDIT_EXPORT: {Role.INCIDENT_LEAD},
    Perm.RESOLVE_OUTCOME: {Role.INCIDENT_LEAD},
    Perm.SET_FENCE: {Role.INCIDENT_LEAD, Role.PLATFORM_ADMIN},
    Perm.OPERATE: {Role.PLATFORM_ADMIN},
    Perm.EDIT_POLICY: {Role.SECURITY_LEAD},
    Perm.MANAGE_KEYS: {Role.SECURITY_LEAD},
    Perm.MANAGE_SANDBOX: {Role.SECURITY_LEAD},
    Perm.EXECUTE: {Role.SYSTEM_DISPATCHER},
}


@pytest.mark.parametrize("perm", list(Perm))
def test_permission_matrix_matches_adr_50_cell_by_cell(perm):
    assert {r for r in Role if perm in MATRIX[r]} == ROWS[perm]


def test_separations_of_duty():
    assert not MATRIX[Role.PLATFORM_ADMIN] & {Perm.APPROVE, Perm.MANAGE_SANDBOX, Perm.EDIT_POLICY, Perm.MANAGE_KEYS}
    assert not MATRIX[Role.APPROVER] & {Perm.EDIT_POLICY, Perm.MANAGE_KEYS}
    assert not MATRIX[Role.SECURITY_LEAD] & {Perm.APPROVE, Perm.REJECT}
    assert not (MATRIX[Role.SYSTEM_WORKFLOW] | MATRIX[Role.SYSTEM_DISPATCHER]) & {Perm.APPROVE, Perm.REJECT}


# ---------------------------------------------------------------- conditional approval of a rollback (ADR-48)
def with_rollback(high_impact=False, **kw):
    fix = action(high_impact=high_impact, **kw)
    return fix, ImmutableAction(action_id=str(uuid.uuid4()), **rollback_action_fields(fix))


def sign_both(fix, back, subject, **kw):
    return rows(fix, subject, rollback=back, rollback_digest=back.digest, rollback_approval_id=str(uuid.uuid4()), **kw)


def standing(a, ds, at=T0, verdict=None):
    return evaluate(a, ds, now=at, keys=KEYS, directory=directory(), policy=POLICY, requested_at=T0, parent_verdict=verdict)[0]


def test_signing_the_rollback_writes_a_second_signed_row():
    fix, back = with_rollback(hold_seconds=1200)
    ok, cond = sign_both(fix, back, "priya")
    assert (cond.action_id, cond.action_digest, cond.decision) == (back.action_id, back.digest, "approved")
    assert cond.conditional and cond.conditional_on_approval_id == ok.approval_id and not ok.conditional
    assert datetime.fromisoformat(cond.valid_until) == datetime.fromisoformat(ok.valid_until) + timedelta(seconds=1200 + 900)
    assert standing(fix, [ok]) == Standing.APPROVED
    forged = dataclasses.replace(cond, conditional=False)               # both fields are inside the signature
    assert standing(back, [forged], verdict="failed") == Standing.PENDING


def test_no_rollback_digest_means_no_conditional_row():
    fix, back = with_rollback()
    assert len(rows(fix, "priya", rollback=back)) == 1


def test_a_mismatched_rollback_digest_is_refused():
    fix, back = with_rollback()
    refused("digest_mismatch", fix, "priya", rollback=back, rollback_digest="0" * 64)
    _, someone_elses = with_rollback()
    refused("digest_mismatch", fix, "priya", rollback=someone_elses, rollback_digest=someone_elses.digest)


def test_conditional_row_counts_only_after_failed_and_only_until_valid_until():
    fix, back = with_rollback()
    _, cond = sign_both(fix, back, "priya")
    assert standing(back, [cond]) == Standing.PENDING
    assert standing(back, [cond], verdict="inconclusive") == Standing.PENDING
    assert standing(back, [cond], verdict="verified") == Standing.PENDING
    assert standing(back, [cond], verdict="failed") == Standing.APPROVED
    lapse = datetime.fromisoformat(cond.valid_until)
    assert standing(back, [cond], at=lapse, verdict="failed") == Standing.APPROVED
    assert standing(back, [cond], at=lapse + timedelta(seconds=1), verdict="failed") == Standing.PENDING


def test_high_impact_rollback_needs_both_signatures():
    fix, back = with_rollback(high_impact=True)
    assert back.high_impact
    _, one = sign_both(fix, back, "priya")
    _, two = sign_both(fix, back, "sam")
    assert standing(back, [one], verdict="failed") == Standing.PENDING
    assert standing(back, [one, two], verdict="failed") == Standing.APPROVED


def test_revoking_the_fix_approval_takes_its_conditional_row_with_it():
    fix, back = with_rollback()
    ok, cond = sign_both(fix, back, "priya")
    rev, rev_cond = rows(fix, "priya", decision="revoked", revokes=ok.approval_id, prior=[ok], reason="wrong",
                         rollback_prior=[cond], rollback_approval_id=str(uuid.uuid4()))
    assert rev.action_id == fix.action_id and (rev_cond.action_id, rev_cond.revokes) == (back.action_id, cond.approval_id)
    assert standing(back, [cond, rev_cond], verdict="failed") == Standing.PENDING


def test_after_the_fix_executed_the_conditional_row_survives_and_is_revoked_on_its_own_action():
    fix, back = with_rollback()
    ok, cond = sign_both(fix, back, "priya")
    refused("execution_started", fix, "priya", decision="revoked", revokes=ok.approval_id, prior=[ok], reason="late",
            action_status="executed", intent_recorded=True, rollback_prior=[cond])
    assert standing(back, [cond], verdict="failed") == Standing.APPROVED
    rev = decide(back, "priya", decision="revoked", revokes=cond.approval_id, prior=[cond], reason="no longer safe",
                 action_status="proposed")
    assert standing(back, [cond, rev], verdict="failed") == Standing.PENDING


def test_a_proposed_rollback_is_not_approved_directly():
    _, back = with_rollback()
    refused("action_not_open", back, "priya", action_status="proposed")
    assert decide(back, "vee", decision="rejected", reason="unsafe", action_status="proposed").decision == "rejected"
    refused("action_not_open", action(), "vee", decision="rejected", reason="unsafe", action_status="proposed")


# ---------------------------------------------------------------- dispatcher
class Store:
    def __init__(self, a, ds, others=(), verdicts=None):
        self.a, self.ds, self.verdicts = a, ds, verdicts or {}
        self.actions = {x.action_id: x for x in (a, *others)}
        self.by_action = {}

    def action(self, action_id):
        return self.actions.get(action_id, self.a)

    def decisions(self, action_id):
        return self.by_action.get(action_id, self.ds)

    def requested_at(self, _):
        return T0

    def verdict(self, action_id):
        return self.verdicts.get(action_id)


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


def unknown(a):
    return LedgerEntry(a.action_id, a.digest, a.target.deployment, "appr-1", 1, LedgerState.UNKNOWN)


def test_unknown_outcome_blocks_resend():
    a = action()
    led = Ledger()
    rel = FakeRelease()

    async def lookup(deployment, key):
        return "applied"                                  # release says applied...
    rel.lookup = lookup                                   # ...but the live revision is still the base: disagreement
    led.put(unknown(a))
    r = asyncio.run(disp(a, [decide(a, "priya")], rel, led).execute(a.action_id))
    assert r.outcome == Outcome.UNKNOWN and rel.apply_calls == 0


def test_two_approvers_one_key_and_the_attempt_names_the_approval_that_completed_the_count():
    a = action(high_impact=True)
    one, two = decide(a, "priya"), decide(a, "sam", now=T0 + timedelta(seconds=30))
    rel, led = FakeRelease(), Ledger()
    keys = []
    original = rel.apply

    async def apply(deployment, key, base, sha, to):
        keys.append(key)
        return await original(deployment, key, base, sha, to)
    rel.apply = apply
    r = asyncio.run(disp(a, [one, two], rel, led).execute(a.action_id))
    assert r.outcome == Outcome.APPLIED and keys == [a.action_id] == [r.execution_key]
    e = led.get(a.action_id)
    assert (e.attempt_no, e.approval_id, e.deployment) == (1, two.approval_id, "checkout-prod-eu1")


def test_intent_and_status_are_written_before_the_call_and_the_outcome_after():
    a = action()
    rel, led = FakeRelease(), Ledger()
    seen = []
    original = rel.apply

    async def apply(*args):
        seen.append((led.get(a.action_id).state, led.action_status[a.action_id]))
        return await original(*args)
    rel.apply = apply
    asyncio.run(disp(a, [decide(a, "priya")], rel, led).execute(a.action_id))
    assert seen == [(LedgerState.STARTED, "applying")]
    assert led.action_status[a.action_id] == "executed" and led.action_version[a.action_id] == 3
    assert LedgerState.STARTED.value == "started"


def test_refusal_writes_the_event_and_no_attempt():
    a = action()
    led = Ledger()
    r = asyncio.run(disp(a, [], FakeRelease(), led).execute(a.action_id))
    assert r.outcome == Outcome.REFUSED and not r.intent_recorded
    assert led.get(a.action_id) is None and a.action_id not in led.action_status
    assert led.events[0]["type"] == "aisre.execution.refused.v1" and led.events[0]["data"]["reason"] == r.reason


def blocked_pair():
    """Two actions on one deployment, the first with an unknown outcome."""
    first, second = action(), action()
    led = Ledger()
    led.put(unknown(first))
    store = Store(second, [decide(second, "priya")], others=[first])
    return first, second, store, led


def test_an_unknown_outcome_blocks_the_deployment_for_every_key():
    first, second, store, led = blocked_pair()
    rel = FakeRelease()
    d = Dispatcher(store, rel, led, KEYS, directory(), POLICY, clock=lambda: T0 + timedelta(minutes=1))
    r = asyncio.run(d.execute(second.action_id))
    assert r.outcome == Outcome.REFUSED and first.action_id in r.reason and rel.apply_calls == 0
    assert led.get(second.action_id) is None and [e.execution_key for e in led.unresolved("checkout-prod-eu1")] == [first.action_id]
    assert led.unresolved("checkout-prod-us1") == []


class YieldingRelease(FakeRelease):
    """Hands control back during the read, so two dispatches both get through their pre-checks."""

    async def current_state(self, deployment):
        state = await super().current_state(deployment)
        await asyncio.sleep(0)
        return state


def test_a_started_attempt_blocks_too_and_the_block_cannot_be_raced():
    a, b = action(), action()
    store = Store(a, [], others=[b])
    store.by_action = {a.action_id: [decide(a, "priya")], b.action_id: [decide(b, "sam")]}
    rel, led = YieldingRelease(), Ledger()
    d = Dispatcher(store, rel, led, KEYS, directory(), POLICY, clock=lambda: T0 + timedelta(minutes=1))

    async def both():
        return await asyncio.gather(d.execute(a.action_id), d.execute(b.action_id))
    results = asyncio.run(both())
    assert sorted(r.outcome.value for r in results) == ["applied", "refused"] and rel.apply_calls == 1
    assert "unresolved change" in next(r for r in results if r.outcome == Outcome.REFUSED).reason


def test_the_same_action_dispatched_twice_at_once_sends_once():
    a = action()
    rel = YieldingRelease()
    d = disp(a, [decide(a, "priya")], rel)

    async def both():
        return await asyncio.gather(d.execute(a.action_id), d.execute(a.action_id))
    results = asyncio.run(both())
    assert rel.apply_calls == 1 and {r.outcome for r in results} == {Outcome.APPLIED, Outcome.REFUSED}


def test_applied_but_the_target_disagrees_is_unknown():
    a = action()
    rel, led = FakeRelease(), Ledger()

    async def lying_apply(*args):
        rel.apply_calls += 1
        return "applied"                                    # ST-40: the release process says so, the target doesn't
    rel.apply = lying_apply
    r = asyncio.run(disp(a, [decide(a, "priya")], rel, led).execute(a.action_id))
    assert r.outcome == Outcome.UNKNOWN and rel.apply_calls == 1
    assert led.get(a.action_id).state == LedgerState.UNKNOWN and led.action_status[a.action_id] == "outcome_unknown"
    assert led.unresolved(a.target.deployment)


def test_an_apply_that_is_neither_yes_nor_no_is_not_called_not_applied():
    a = action()
    rel, led = FakeRelease(), Ledger()

    async def still_running(*args):
        return "in_progress"
    rel.apply = still_running

    async def lookup(deployment, key):
        return "in_progress"
    rel.lookup = lookup
    r = asyncio.run(disp(a, [decide(a, "priya")], rel, led).execute(a.action_id))
    assert r.outcome == Outcome.UNKNOWN and led.get(a.action_id).state == LedgerState.UNKNOWN


def test_reconcile_needs_both_sources():
    a = action()
    rel, led = FakeRelease(revision="something-else"), Ledger()
    rel.processed[a.action_id] = "applied"
    led.put(unknown(a))
    assert asyncio.run(disp(a, [], rel, led).reconcile(a.action_id)).outcome == Outcome.UNKNOWN
    rel.processed.clear()
    assert asyncio.run(disp(a, [], rel, led).reconcile(a.action_id)).outcome == Outcome.UNKNOWN
    assert len(led.rows[a.action_id]) == 1                  # still unknown: nothing new recorded


def test_reconcile_adds_a_row_and_leaves_the_unknown_attempt_frozen():
    a = action()
    rel, led = FakeRelease(revision="e5f6a7b8"), Ledger()
    rel.processed[a.action_id] = "applied"
    led.put(unknown(a))
    assert asyncio.run(disp(a, [], rel, led).reconcile(a.action_id)).outcome == Outcome.APPLIED
    first, second = led.rows[a.action_id]
    assert (first.state, first.reconciliation, first.attempt_no) == (LedgerState.UNKNOWN, False, 1)
    assert (second.state, second.reconciliation, second.attempt_no) == (LedgerState.APPLIED, True, 2)
    assert led.unresolved(a.target.deployment) == []
    with pytest.raises(RuntimeError):
        led.put(dataclasses.replace(first, state=LedgerState.APPLIED))


def test_reconcile_raises_while_in_progress_so_read_retry_is_the_wait():
    a = action()
    rel, led = FakeRelease(), Ledger()
    led.put(unknown(a))

    async def lookup(deployment, key):
        return "in_progress"
    rel.lookup = lookup
    with pytest.raises(ReleaseInProgress):
        asyncio.run(disp(a, [], rel, led).reconcile(a.action_id))


def test_reconcile_after_a_crash_freezes_the_started_attempt():
    a = action()
    rel, led = FakeRelease(), Ledger()
    led.put(dataclasses.replace(unknown(a), state=LedgerState.STARTED))
    r = asyncio.run(disp(a, [], rel, led).reconcile(a.action_id))
    assert r.outcome == Outcome.NOT_APPLIED and [e.state for e in led.rows[a.action_id]] == [LedgerState.UNKNOWN, LedgerState.NOT_APPLIED]


def test_reconcile_with_no_intent_says_so():
    a = action()
    r = asyncio.run(disp(a, [], FakeRelease()).reconcile(a.action_id))
    assert r.outcome == Outcome.NOT_APPLIED and not r.intent_recorded and "no intent" in r.reason


def test_edit_policy_change_refuses_the_dispatch():
    a = action()
    d = disp(a, [decide(a, "priya")], FakeRelease())
    d.policy = ApprovalPolicy(edit_version="edit-policy@2")
    r = asyncio.run(d.execute(a.action_id))
    assert r.outcome == Outcome.REFUSED and "edit-policy@2" in r.reason
    old = action(policy_version=f"edit-policy@0|{POLICY.version}")      # the decision is current, the action isn't
    r = asyncio.run(disp(old, [decide(old, "priya")], FakeRelease()).execute(old.action_id))
    assert r.outcome == Outcome.REFUSED and "edit-policy@0" in r.reason


RESOLVED = dict(subject="sam", observed_revision="a1b2c3d4", reason="checked the deployment by hand",
                auth_time=T0, mfa=True)


def test_a_persons_resolution_clears_the_block():
    first, second, store, led = blocked_pair()
    rel = FakeRelease()
    d = Dispatcher(store, rel, led, KEYS, directory(), POLICY, clock=lambda: T0 + timedelta(minutes=1))
    r = d.resolve_outcome(first.action_id, outcome="not_applied", **RESOLVED)
    assert r.outcome == Outcome.NOT_APPLIED and led.action_status[first.action_id] == "not_applied"
    last = led.get(first.action_id)
    assert last.reconciliation and last.resolution.subject == "sam" and last.resolution.observed_revision == "a1b2c3d4"
    assert asyncio.run(d.execute(second.action_id)).outcome == Outcome.APPLIED


@pytest.mark.parametrize("change,code", [
    (dict(subject="priya"), "forbidden"), (dict(subject="admin"), "forbidden"), (dict(mfa=False), "reauth_required"),
    (dict(auth_time=T0 - timedelta(hours=1)), "reauth_required"), (dict(reason=""), "reason_required"),
    (dict(outcome="failed"), "invalid_request"), (dict(observed_revision=""), "invalid_request")])
def test_who_may_resolve_an_outcome(change, code):
    a, led = action(), Ledger()
    led.put(unknown(a))
    with pytest.raises(ApprovalError) as e:
        disp(a, [], FakeRelease(), led).resolve_outcome(a.action_id, **{"outcome": "applied", **RESOLVED, **change})
    assert e.value.code == code and led.unresolved(a.target.deployment)


def test_only_an_unknown_outcome_can_be_resolved():
    a = action()
    with pytest.raises(ApprovalError) as e:
        disp(a, [], FakeRelease()).resolve_outcome(a.action_id, outcome="applied", **RESOLVED)
    assert e.value.code == "action_not_open"


def rollback_dispatch(verdict, revision="e5f6a7b8"):
    fix, back = with_rollback()
    _, cond = sign_both(fix, back, "priya")
    rel, led = FakeRelease(revision=revision), Ledger()
    store = Store(back, [cond], verdicts={fix.action_id: verdict})
    d = Dispatcher(store, rel, led, KEYS, directory(), POLICY, clock=lambda: T0 + timedelta(minutes=20))
    return back, rel, led, asyncio.run(d.execute(back.action_id))


def test_rollback_is_dispatched_under_its_own_key_once_the_fix_failed():
    back, rel, led, r = rollback_dispatch("failed")
    assert r.outcome == Outcome.APPLIED and r.execution_key == back.action_id and rel.revision == "a1b2c3d4"


def test_rollback_is_refused_before_a_failed_verdict_and_if_the_target_moved():
    assert rollback_dispatch(None)[3].outcome == Outcome.REFUSED
    assert rollback_dispatch("verified")[3].outcome == Outcome.REFUSED
    back, rel, _, r = rollback_dispatch("failed", revision="99999999")
    assert r.outcome == Outcome.REFUSED and rel.apply_calls == 0


# ---------------------------------------------------------------- verification and rollback
BASE = {"http_5xx_rate_path": 0.002, "http_5xx_rate": 0.001, "http_latency_p95_ms": 220.0}
PLAN = reference_plan("checkout", "/api/pay", BASE)


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
    assert verify(PLAN, samples()).verdict == Verdict.VERIFIED


def test_relapse_inside_window_fails():
    r = verify(PLAN, samples(http_5xx_rate_path=lambda i: 0.002 if i < 2 else 0.2))
    assert r.verdict == Verdict.FAILED and "error_rate_failing_path" in r.reasons[0]


def test_latency_regression_fails_even_if_errors_fixed():
    assert verify(PLAN, samples(http_latency_p95_ms=lambda i: 900.0)).verdict == Verdict.FAILED


def test_new_error_group_fails():
    assert verify(PLAN, samples(new_error_group_count=lambda i: 1 if i == 7 else 0)).verdict == Verdict.FAILED


def test_too_little_traffic_or_too_short_is_inconclusive():
    low = [Sample(s.t, 5, s.values) for s in samples()]
    assert verify(PLAN, low).verdict == Verdict.INCONCLUSIVE
    assert verify(PLAN, samples(n=3)).verdict == Verdict.INCONCLUSIVE


def test_a_metric_missing_from_the_baseline_is_insufficient_data_never_held():
    gaps = {k: v for k, v in BASE.items() if k != "http_latency_p95_ms"}
    r = verify(reference_plan("checkout", "/api/pay", gaps), samples())
    assert r.verdict == Verdict.INCONCLUSIVE and r.per_predicate["latency_p95"] == "insufficient_data"
    r = verify(reference_plan("checkout", "/api/pay", {}), samples(http_latency_p95_ms=lambda i: 9e9))
    assert r.verdict == Verdict.INCONCLUSIVE
    assert verify(reference_plan("checkout", "/api/pay", {}), samples(new_error_group_count=lambda i: 1)).verdict == Verdict.FAILED


def test_a_zero_baseline_uses_the_error_rate_floor():
    zero = reference_plan("checkout", "/api/pay", {**BASE, "http_5xx_rate_path": 0.0, "http_5xx_rate": 0.0})
    assert verify(zero, samples(http_5xx_rate_path=lambda i: 0.0009, http_5xx_rate=lambda i: 0.001)).verdict == Verdict.VERIFIED
    assert verify(zero, samples(http_5xx_rate_path=lambda i: 0.0011)).verdict == Verdict.FAILED


def test_the_floor_is_only_on_error_rates():
    plan = VerificationPlan("s", "/", (Predicate("p95", "lat", "<=", 1.2, relative_to_baseline=True),),
                            window_s=60, baseline={"lat": 0.0})
    assert verify(plan, [Sample(0, 100, {"lat": 0.0005})]).verdict == Verdict.FAILED


def test_baseline_is_the_median_of_the_window_before_the_incident():
    start = 10_000
    points = [(start - 3601, 100.0), (start - 3600, 1.0), (start - 2000, 3.0), (start - 301, 5.0), (start - 300, 100.0),
              (start - 60, 100.0), (start, 100.0)]
    got = baseline([Sample(t, 400, {"m": v, "gone": None}) for t, v in points], start)
    assert got == {"m": 3.0}                                # start of the range kept, its end and the last 5 minutes not


def test_the_plan_digest_covers_baseline_and_hold():
    assert reference_plan("c", "/", {"m": 1.0}).digest != reference_plan("c", "/", {"m": 2.0}).digest
    long = reference_plan("c", "/", BASE, hold_seconds=1800)
    assert long.window_s == 1800 and long.digest != reference_plan("c", "/", BASE).digest
    assert verify(long, samples(n=10)).verdict == Verdict.INCONCLUSIVE and verify(long, samples(n=30)).verdict == Verdict.VERIFIED


def test_rollback_is_linked_and_resolution_needs_all_conditions():
    p = action(high_impact=True, hold_seconds=1200)
    f = rollback_action_fields(p)
    r = ImmutableAction(action_id="act-2", **f)
    assert f["parent_action_id"] == p.action_id and f["expected_after_revision"] == "a1b2c3d4"
    assert r.kind == "rollback" and r.base_revision == "e5f6a7b8" and r.patch_sha256 is None
    assert (r.test_bundle_digest, r.recovery_predicates_digest, r.hold_seconds, r.high_impact, r.target, r.policy_version) == (
        p.test_bundle_digest, p.recovery_predicates_digest, 1200, True, p.target, p.policy_version)
    assert resolution_allowed(Verdict.VERIFIED, True, 0)[0]
    assert not resolution_allowed(Verdict.INCONCLUSIVE, True, 0)[0]
    assert not resolution_allowed(Verdict.VERIFIED, False, 0)[0]
    assert not resolution_allowed(Verdict.VERIFIED, True, 1)[0]
