"""Artifact 11.3–11.5: sandbox launch specification, resource isolation, and the trusted test harness result.

The launcher is platform code (owned by the security lead). It builds every sandbox from a
fixed, versioned specification; nothing in a patch or a model output can add mounts, devices,
capabilities, network or environment variables (Q11.33, Q11.34). The launcher also applies the
two limits `docker run` has no flag for, the workspace size and the wall clock, and owns cleanup.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


@dataclass(frozen=True)
class SandboxSpec:
    """Q11.17–Q11.29. Values are provisional; the security lead owns them."""

    spec_version: str = "sandbox-spec@1"
    image: str = "registry.internal/aisre/test-runner@sha256:<pinned-digest>"   # pinned by digest, never a tag
    runtime: str = "runsc"                 # gVisor: user-space kernel between the code and the host kernel
    cpus: float = 2.0                      # Q11.24 (cgroup cpu quota)
    memory: str = "2g"                     # Q11.25 (cgroup memory limit; swap disabled)
    pids_limit: int = 256                  # Q11.26 (cgroup pids)
    tmpfs_mb: int = 1024                   # Q11.27 (scratch space, in memory, bounded)
    workspace_mb: int = 2048               # Q11.27 (patched checkout, on a size-limited volume)
    wall_clock_s: int = 900                # Q11.28 (whole run)
    per_test_s: int = 120                  # Q11.47 (each test)
    user: str = "65534:65534"              # Q11.20 (nobody; never root)
    network: str = "none"                  # Q11.21, ADR-60 (no network; nothing else is accepted)


FORBIDDEN_MOUNT_SOURCES = ("/var/run/docker.sock", "/run/docker.sock", "/run/containerd", "/proc", "/sys",
                           "/dev", "/etc", "/root", "/home", "/var/lib/docker")


STOP_GRACE_S = 10                          # Q11.28: stop, then kill after this
REAP_INTERVAL_S = 300                      # Q11.30: the Test Runner schedules reap() this often
REAP_AGE_S = 3600                          # Q11.30, FR-23: nothing labelled survives past this


class SpecRejected(Exception):
    pass


def launch_args(spec: SandboxSpec, run_id: str, workspace_volume: str) -> list[str]:
    """The exact container command the trusted launcher runs. Built only from SandboxSpec."""
    args = [
        "docker", "run", "--rm", "--name", f"aisre-test-{run_id}",
        f"--runtime={spec.runtime}",
        f"--network={spec.network}",
        "--read-only",                                         # image filesystem immutable
        f"--tmpfs=/tmp:rw,noexec,nosuid,nodev,size={spec.tmpfs_mb}m",
        f"--mount=type=volume,src={workspace_volume},dst=/workspace",
        f"--cpus={spec.cpus}",
        f"--memory={spec.memory}", f"--memory-swap={spec.memory}",   # no swap beyond the limit
        f"--pids-limit={spec.pids_limit}",
        f"--user={spec.user}",
        "--cap-drop=ALL",                                      # no Linux capabilities
        "--security-opt=no-new-privileges",                    # no setuid escalation
        "--ipc=private",                                       # PID and UTS are private by default; Docker
        f"--stop-timeout={STOP_GRACE_S}",                      # refuses --pid=private and --uts=private
        "--label=aisre.sandbox=1", f"--label=aisre.run={run_id}",
        "--env=AISRE_SANDBOX=1",                               # the only variables: no secrets, ever (Q11.22)
        spec.image, "/harness/run",                            # trusted harness from the image, not the workspace
    ]
    validate_args(args)
    return args


def workspace_volume(run_id: str) -> str:
    return f"aisre-ws-{run_id}"


def volume_args(spec: SandboxSpec, run_id: str) -> list[str]:
    """Q11.27: the run's own workspace, a memory-backed volume, because tmpfs is what limits its size."""
    uid, gid = spec.user.split(":")
    return ["docker", "volume", "create", "--driver=local", "--opt=type=tmpfs", "--opt=device=tmpfs",
            f"--opt=o=size={spec.workspace_mb}m,uid={uid},gid={gid},nosuid,nodev",
            "--label=aisre.sandbox=1", f"--label=aisre.run={run_id}", workspace_volume(run_id)]


def validate_args(args: list[str]) -> None:
    """Defence in depth: refuse any argument list that could weaken isolation (Q11.23, Q11.31)."""
    joined = " ".join(args)
    for bad in ("--privileged", "--cap-add", "--device", "--network=host", "--pid=host", "--ipc=host",
                "--userns=host", "seccomp=unconfined", "apparmor=unconfined", "--security-opt=label=disable"):
        if bad in joined:
            raise SpecRejected(f"forbidden option {bad}")
    for a in args:
        if a.startswith(("-v", "--volume", "--mount")) and any(s in a for s in FORBIDDEN_MOUNT_SOURCES):
            raise SpecRejected(f"forbidden mount {a}")
        if a.startswith("--env=") and any(k in a.upper() for k in ("TOKEN", "SECRET", "PASSWORD", "KEY=", "AWS_", "DATABASE_URL")):
            raise SpecRejected("credentials must never enter the sandbox")
        if a.startswith("--user=") and a.split("=", 1)[1].split(":")[0] in ("0", "root"):
            raise SpecRejected("sandbox must not run as root")
    if [a for a in args if a.startswith("--net")] != ["--network=none"]:
        raise SpecRejected("--network=none is required (ADR-60)")
    if "--runtime=runsc" not in args:
        raise SpecRejected("gVisor runtime required")


# ---------------------------------------------------------------------------
# Trusted test harness result (Q11.38–Q11.48)
# ---------------------------------------------------------------------------
class TestOutcome(str, Enum):
    __test__ = False
    PASSED = "passed"                      # reproduced before, gone after, no regressions, all suites ran
    FAILED = "failed"                      # reproduced, but the failure remains or a regression appeared
    NOT_REPRODUCED = "not_reproduced"      # the original failure never appeared: no valid comparison (Q11.39)
    INCONCLUSIVE = "inconclusive"          # required suites couldn't run (Q11.48)
    TIMEOUT = "timeout"                    # wall clock or per-test limit hit (Q11.47)
    CRASHED = "crashed"                    # harness died, or no valid result bundle for this patch (Q11.46)


REQUIRED_SUITES = ("reproduction", "unit", "integration", "smoke", "error_signature")   # fixed here, not by the bundle


@dataclass(frozen=True)
class SuiteResult:
    name: str                              # REQUIRED_SUITES, plus any extra evidence
    required: bool
    ran: bool
    passed: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ResultBundle:
    """Q11.44, Q11.45: what the harness returns, bound to the exact patch and environment."""

    run_id: str
    patch_sha256: str
    base_revision: str
    image_digest: str
    spec_version: str
    harness_version: str
    reproduction_before: Optional[bool]    # failure observed on the base revision?
    reproduction_after: Optional[bool]     # failure observed after the patch?
    suites: tuple[SuiteResult, ...]
    new_error_signatures: tuple[str, ...]  # errors after the patch not present before (Q11.41)
    exit_code: Optional[int]               # the harness's own: 0 if it wrote a bundle, even with failing tests
    duration_s: float
    timed_out: bool
    log_artifact_sha256: Optional[str]

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.__dict__, default=lambda o: o.__dict__, sort_keys=True).encode()).hexdigest()


def classify(b: Optional[ResultBundle], expected_patch_sha256: str) -> TestOutcome:
    """The one place a test run becomes a verdict. The order is the table in Artifact 11.5:
    the first match wins, and `passed` is reached only if nothing before it matched."""
    if b is None or b.exit_code is None:
        return TestOutcome.CRASHED
    if b.patch_sha256 != expected_patch_sha256:
        return TestOutcome.CRASHED                           # results for a different patch are not results
    if b.timed_out:
        return TestOutcome.TIMEOUT                           # before the exit code: a timeout kill exits 137
    if b.exit_code != 0:
        return TestOutcome.CRASHED
    if b.reproduction_before is False:
        return TestOutcome.NOT_REPRODUCED
    if b.reproduction_before is None or b.reproduction_after is None:
        return TestOutcome.INCONCLUSIVE
    if any(s.required and not s.ran for s in b.suites):
        return TestOutcome.INCONCLUSIVE
    for name in REQUIRED_SUITES:                             # by name: the bundle's `required` flag can't excuse one
        found = [s for s in b.suites if s.name == name]
        if not found or any(not s.ran or s.passed + s.failed == 0 for s in found):
            return TestOutcome.INCONCLUSIVE
    if b.reproduction_after or b.new_error_signatures or any(s.failed or s.errors for s in b.suites):
        return TestOutcome.FAILED
    return TestOutcome.PASSED


# ---------------------------------------------------------------------------
# The launcher's run, and cleanup (Q11.27, Q11.28, Q11.30)
# ---------------------------------------------------------------------------
def read_bundle(stdout: str, exit_code: int) -> Optional[ResultBundle]:
    """The bundle is the last line of the harness's output, the only channel left: `--rm` removes
    the container and the tmpfs volume empties with it. The exit code is what Docker reported,
    not what the bundle claims. Anything unreadable is no bundle, which classifies as crashed."""
    try:
        d = json.loads(stdout.strip().splitlines()[-1])
        suites = tuple(SuiteResult(**s) for s in d.pop("suites"))
        return ResultBundle(**{**d, "suites": suites, "exit_code": exit_code,
                               "new_error_signatures": tuple(d["new_error_signatures"])})
    except (IndexError, KeyError, TypeError, ValueError, AttributeError):
        return None


def run_sandbox(spec: SandboxSpec, run_id: str, expected_patch_sha256: str, base_revision: str, *,
                popen=subprocess.Popen, run=subprocess.run) -> Optional[ResultBundle]:
    """The Test Runner's launcher step: workspace, run under the wall clock, cleanup. On the deadline
    it writes the bundle itself, or classify would see none and call a timeout a crash."""
    name, volume = f"aisre-test-{run_id}", workspace_volume(run_id)
    started = time.monotonic()
    try:
        run(volume_args(spec, run_id), check=True, capture_output=True)
        proc = popen(launch_args(spec, run_id, volume), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            out, _ = proc.communicate(timeout=spec.wall_clock_s)
            return read_bundle(out, proc.returncode)
        except subprocess.TimeoutExpired:
            try:
                run(["docker", "stop", f"--time={STOP_GRACE_S}", name], capture_output=True, timeout=STOP_GRACE_S + 20)
                proc.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()                                  # a stuck stop; the reaper removes what's left
                proc.communicate()
            return ResultBundle(run_id=run_id, patch_sha256=expected_patch_sha256, base_revision=base_revision,
                                image_digest=spec.image, spec_version=spec.spec_version, harness_version="",
                                reproduction_before=None, reproduction_after=None, suites=(), new_error_signatures=(),
                                exit_code=proc.returncode, duration_s=time.monotonic() - started, timed_out=True,
                                log_artifact_sha256=None)
    finally:                                                 # also on cancellation; a failure here is the reaper's
        run(["docker", "rm", "--force", name], capture_output=True)
        run(["docker", "volume", "rm", "--force", volume], capture_output=True)


def _older(lines: str, now: datetime) -> list[str]:
    """Names from "<name> <RFC 3339 time>" lines, for those past REAP_AGE_S."""
    pairs = [line.split(None, 1) for line in lines.splitlines() if line.strip()]
    return [n for n, t in pairs if (now - datetime.fromisoformat(t.strip())).total_seconds() > REAP_AGE_S]


def reap(now: datetime, *, run=subprocess.run) -> int:
    """Q11.30: remove containers and volumes labelled aisre.sandbox=1 older than 60 minutes. Returns
    how many were left over, for aisre_sandboxes_leftover, which should be zero. `now` is timezone-aware."""
    def docker(*args: str) -> str:
        return run(["docker", *args], check=True, capture_output=True, text=True).stdout

    label = "label=aisre.sandbox=1"
    containers = docker("ps", "--all", "--quiet", "--filter", label).split()
    stale = _older(docker("inspect", "--format={{.Id}} {{.Created}}", *containers), now) if containers else []
    volumes = docker("volume", "ls", "--quiet", "--filter", label).split()
    stale_volumes = _older(docker("volume", "inspect", "--format={{.Name}} {{.CreatedAt}}", *volumes), now) if volumes else []
    if stale:
        docker("rm", "--force", *stale)                      # first: a volume in use can't be removed
    if stale_volumes:
        docker("volume", "rm", "--force", *stale_volumes)
    return len(stale) + len(stale_volumes)
