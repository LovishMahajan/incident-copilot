"""Artifact 11.3–11.5: sandbox launch specification, resource isolation, and the trusted test harness result.

The launcher is platform code (owned by the security lead). It builds every sandbox from a
fixed, versioned specification; nothing in a patch or a model output can add mounts, devices,
capabilities, network or environment variables (Q11.33, Q11.34).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
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
    network: str = "none"                  # Q11.21 (default deny)
    package_mirror: Optional[str] = None   # if set: the ONLY egress, via a filtering proxy


FORBIDDEN_MOUNT_SOURCES = ("/var/run/docker.sock", "/run/docker.sock", "/run/containerd", "/proc", "/sys",
                           "/dev", "/etc", "/root", "/home", "/var/lib/docker")


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
        "--ipc=private", "--pid=private", "--uts=private",     # own process, IPC and host-name namespaces
        f"--stop-timeout=10",
        "--label=aisre.sandbox=1", f"--label=aisre.run={run_id}",
        "--env=AISRE_SANDBOX=1",                               # the only variables: no secrets, ever (Q11.22)
        spec.image, "/harness/run",                            # trusted harness from the image, not the workspace
    ]
    validate_args(args)
    return args


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
    CRASHED = "crashed"                    # sandbox died before returning a valid result bundle (Q11.46)


@dataclass(frozen=True)
class SuiteResult:
    name: str                              # reproduction | unit | integration | regression | smoke
    required: bool
    ran: bool
    passed: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ResultBundle:
    """Q11.44, Q11.45: what the harness returns, bound to the exact patch and environment."""

    run_id: str
    patch_digest: str
    base_revision: str
    image_digest: str
    spec_version: str
    harness_version: str
    reproduction_before: Optional[bool]    # failure observed on the base revision?
    reproduction_after: Optional[bool]     # failure observed after the patch?
    suites: tuple[SuiteResult, ...]
    new_error_signatures: tuple[str, ...]  # errors after the patch not present before (Q11.41)
    exit_code: Optional[int]
    duration_s: float
    timed_out: bool
    log_artifact_sha256: Optional[str]

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.__dict__, default=lambda o: o.__dict__, sort_keys=True).encode()).hexdigest()


def classify(b: Optional[ResultBundle], expected_patch_digest: str) -> TestOutcome:
    """The one place a test run becomes a verdict. Fail closed everywhere."""
    if b is None or b.exit_code is None:
        return TestOutcome.CRASHED
    if b.patch_digest != expected_patch_digest:
        return TestOutcome.CRASHED                           # results for a different patch are not results
    if b.timed_out:
        return TestOutcome.TIMEOUT
    if b.reproduction_before is not True:
        return TestOutcome.NOT_REPRODUCED
    if any(s.required and not s.ran for s in b.suites):
        return TestOutcome.INCONCLUSIVE
    if b.reproduction_after or b.new_error_signatures or any(s.failed or s.errors for s in b.suites):
        return TestOutcome.FAILED
    return TestOutcome.PASSED
