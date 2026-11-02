"""ST-25 to ST-28: the sandbox. Real launcher, validator and classifier, against a recorded `docker`. Isolation
itself needs gVisor, so what generated code can do at run time is skipped, not faked."""

from __future__ import annotations

import dataclasses
import inspect
import json
import subprocess
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from patch import PatchRejected, Patch, check_boundaries
from sandbox import (REAP_AGE_S, ResultBundle, SandboxSpec, SpecRejected, SuiteResult, TestOutcome, classify, launch_args,
                     reap, read_bundle, run_sandbox, validate_args, volume_args)
from support import BASE, make_patch
from tool_registry import PatchDraftParams

NEEDS_GVISOR = "Needs a running gVisor sandbox: runsc is not installed here, so {} can't be observed"


def spliced(*extra):
    a = launch_args(SandboxSpec(), "run1", "ws")
    return a[:-2] + list(extra) + a[-2:]


# ---------------------------------------------------------------- ST-25
def test_st25_every_launch_has_no_network():
    a = launch_args(SandboxSpec(), "run1", "ws")
    assert [x for x in a if x.startswith("--net")] == ["--network=none"]
    assert not [x for x in a if x.startswith(("--dns", "--add-host", "-p", "--publish"))]


@pytest.mark.parametrize("network", ["bridge", "host", "container:prod", "aisre-mirror"])
def test_st25_a_spec_asking_for_any_network_is_refused(network):
    with pytest.raises(SpecRejected):
        launch_args(SandboxSpec(network=network), "run1", "ws")


def test_st25_spec_has_no_package_mirror():
    assert not [f.name for f in dataclasses.fields(SandboxSpec) if "mirror" in f.name or "proxy" in f.name]


def test_st25_real_connections_need_a_sandbox():
    pytest.skip(NEEDS_GVISOR.format("connections to production, internal, metadata, mirror and DNS targets"))


# ---------------------------------------------------------------- ST-26
def test_st26_launch_takes_nothing_from_a_patch():
    p = make_patch()
    assert list(inspect.signature(launch_args).parameters) == ["spec", "run_id", "workspace_volume"]
    for field in ("privileged", "mounts", "network", "capabilities", "user"):
        with pytest.raises(ValidationError):
            Patch(**{**p.model_dump(), field: True})
        with pytest.raises(ValidationError):
            PatchDraftParams(service="checkout", action_kind="config_revert", target_ref="x", **{field: True})


@pytest.mark.parametrize("path", ["Dockerfile", "sandbox/launcher.py", "harness/run", "deploy/values.yaml",
                                  ".github/workflows/ci.yml"])
def test_st26_a_patch_cannot_edit_how_it_is_launched(path):
    p = make_patch(old={path: "a\n"}, new={path: "b\n"})
    with pytest.raises(PatchRejected) as e:
        check_boundaries(p)
    assert e.value.code == "protected_path"


@pytest.mark.parametrize("extra", [("--privileged",), ("--cap-add=SYS_ADMIN",), ("--device=/dev/kvm",),
                                   ("--network=host",), ("--pid=host",), ("--ipc=host",), ("--userns=host",),
                                   ("--user=0:0",), ("--user=root",), ("--security-opt=seccomp=unconfined",),
                                   ("--security-opt=apparmor=unconfined",), ("-v=/var/run/docker.sock:/s",),
                                   ("--volume=/etc:/etc",), ("--mount=type=bind,src=/proc,dst=/p",)])
def test_st26_weakening_options_are_refused(extra):
    with pytest.raises(SpecRejected):
        validate_args(spliced(*extra))


@pytest.mark.parametrize("extra", [
    ("--user", "0"), ("-u", "root"), ("--user=00:00",), ("-v=/:/host",), ("-v", "/var/run/docker.sock:/s"),
    ("--mount=type=bind,src=/,dst=/host",), ("--volumes-from=prod-db",), ("--pid", "host"), ("--uts=host",),
    ("--cgroupns=host",), ("--security-opt=systempaths=unconfined",), ("--runtime=runc",), ("--read-only=false",),
    ("--network=none,host",), ("--env=AISRE_SANDBOX=1x",),
])
def test_st26_weakening_options_are_refused_in_every_spelling(extra):
    with pytest.raises(SpecRejected):
        validate_args(spliced(*extra))


def test_st26_an_option_nobody_listed_is_refused_and_the_real_launch_passes():
    validate_args(spliced())
    with pytest.raises(SpecRejected):
        validate_args(spliced("--some-future-option=host"))


@pytest.mark.parametrize("cut", ["--read-only", "--cap-drop=ALL", "--network=none", "--runtime=runsc"])
def test_st26_a_launch_missing_a_required_option_is_refused(cut):
    with pytest.raises(SpecRejected):
        validate_args([a for a in spliced() if a != cut])


@pytest.mark.parametrize("field,value", [("runtime", "runc"), ("user", "0:0"), ("user", "root:root")])
def test_st26_a_weakened_spec_is_refused_at_launch(field, value):
    with pytest.raises(SpecRejected):
        launch_args(SandboxSpec(**{field: value}), "run1", "ws")


# ---------------------------------------------------------------- ST-27
def test_st27_escape_tests_need_the_chosen_isolation():
    pytest.skip(NEEDS_GVISOR.format("an escape to the host"))


# ---------------------------------------------------------------- ST-28
def bundle(p, **kw):
    suites = tuple(SuiteResult(n, True, True, 1, 0) for n in ("reproduction", "unit", "integration", "smoke", "error_signature"))
    return ResultBundle(**{**dict(run_id="r1", patch_sha256=p.digest, base_revision=BASE, image_digest="sha256:i",
                                  spec_version="sandbox-spec@1", harness_version="h@1", reproduction_before=True,
                                  reproduction_after=False, suites=suites, new_error_signatures=(), exit_code=0,
                                  duration_s=1.0, timed_out=False, log_artifact_sha256="0" * 64), **kw})


def test_st28_every_limit_is_on_the_launch():
    a, spec = launch_args(SandboxSpec(), "run1", "ws"), SandboxSpec()
    for flag in (f"--pids-limit={spec.pids_limit}", f"--memory={spec.memory}", f"--memory-swap={spec.memory}",
                 f"--cpus={spec.cpus}", "--read-only"):
        assert flag in a
    assert any(x.startswith("--tmpfs=/tmp") and f"size={spec.tmpfs_mb}m" in x for x in a)
    assert any(f"size={spec.workspace_mb}m" in x for x in volume_args(spec, "run1"))


def docker():
    calls = []

    def run(args, **kw):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")
    return calls, run


class Hung:
    returncode = 137

    def __init__(self):
        self.polled = 0

    def communicate(self, timeout=None):
        self.polled += 1
        if self.polled == 1:
            raise subprocess.TimeoutExpired("docker", timeout)
        return "", ""


def test_st28_a_run_past_the_time_limit_is_stopped_as_timeout_and_destroyed():
    p, (calls, run) = make_patch(), docker()
    b = run_sandbox(SandboxSpec(), "r1", p.digest, BASE, popen=lambda *a, **k: Hung(), run=run)
    assert classify(b, p.digest) == TestOutcome.TIMEOUT
    assert ["docker", "stop", "--time=10", "aisre-test-r1"] in calls
    assert calls[-2:] == [["docker", "rm", "--force", "aisre-test-r1"], ["docker", "volume", "rm", "--force", "aisre-ws-r1"]]


def test_st28_killed_by_a_limit_is_crashed_whatever_the_output_claims():
    p = make_patch()
    claims_clean = json.dumps(dataclasses.asdict(bundle(p)))
    assert classify(read_bundle(claims_clean, 137), p.digest) == TestOutcome.CRASHED
    assert classify(read_bundle("Killed", 137), p.digest) == TestOutcome.CRASHED


def test_st28_control_a_clean_run_passes():
    p = make_patch()
    assert classify(read_bundle(json.dumps(dataclasses.asdict(bundle(p))), 0), p.digest) == TestOutcome.PASSED


def test_st28_nothing_labelled_survives_past_sixty_minutes():
    now = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    removed = []

    def run(args, **kw):
        out = {"ps": "c1\nc2\n", "inspect": "c1 2026-11-02T10:59:00Z\nc2 2026-11-02T11:30:00Z\n"}.get(args[1], "")
        if args[1:3] == ["volume", "ls"]:
            out = "v1\n"
        elif args[1:3] == ["volume", "inspect"]:
            out = "v1 2026-11-02T10:00:00Z\n"
        if "rm" in args:
            removed.append(args[1:])
        return subprocess.CompletedProcess(args, 0, out, "")
    assert REAP_AGE_S == 3600 and reap(now, run=run) == 2
    assert removed == [["rm", "--force", "c1"], ["volume", "rm", "--force", "v1"]]


def test_st28_fork_bomb_and_fill_need_a_sandbox():
    pytest.skip(NEEDS_GVISOR.format("a real fork bomb, memory or disk fill being stopped with the host unaffected"))
