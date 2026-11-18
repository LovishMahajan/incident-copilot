"""Artifact 11.3–11.5: sandbox launch specification, resource isolation, and the trusted test harness result.

The launcher is platform code (owned by the security lead). It builds every sandbox from a
fixed, versioned specification; nothing in a patch or a model output can add mounts, devices,
capabilities, network or environment variables (Q11.33, Q11.34). The launcher also applies the
two limits `docker run` has no flag for, the workspace size and the wall clock, and owns cleanup.

ADR-71 (T2.2, T2.4, T2.5): the runtime comes from settings (`sandbox_spec`): `runsc` (gVisor),
or `runc` only when the environment is development or test, with every other flag unchanged and
every result labelled with its isolation (`isolation`). The checkout reaches the sandbox on
standard input (`build_payload`): `git archive` of the exact base commit, the patch's canonical
bytes and the run description, never a mounted path. A patch touching a protected path is
refused before anything starts (`refuse_protected`). The image is the one built from
`ops/sandbox/`, named by digest.
"""

from __future__ import annotations

import fnmatch
import hashlib
import io
import json
import logging
import re
import subprocess
import tarfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from aisre.agent.validation import mask_secrets
from aisre.remediation.patch import PROTECTED, PatchRejected

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SandboxSpec:
    """Q11.17–Q11.29. Values are provisional; the security lead owns them."""

    spec_version: str = "sandbox-spec@1"
    image: str = "registry.internal/aisre/test-runner@sha256:<pinned-digest>"  # pinned by digest, never a tag
    runtime: str = "runsc"  # gVisor: user-space kernel between the code and the host kernel
    cpus: float = 2.0  # Q11.24 (cgroup cpu quota)
    memory: str = "2g"  # Q11.25 (cgroup memory limit; swap disabled)
    pids_limit: int = 256  # Q11.26 (cgroup pids)
    tmpfs_mb: int = 1024  # Q11.27 (scratch space, in memory, bounded)
    workspace_mb: int = 2048  # Q11.27 (patched checkout, on a size-limited volume)
    wall_clock_s: int = 900  # Q11.28 (whole run)
    per_test_s: int = 120  # Q11.47 (each test)
    user: str = "65534:65534"  # Q11.20 (nobody; never root)
    network: str = "none"  # Q11.21, ADR-60 (no network; nothing else is accepted)
    environment: str = "production"  # ADR-71: `runc` only when this is development or test


STOP_GRACE_S = 10  # Q11.28: stop, then kill after this
REAP_INTERVAL_S = 300  # Q11.30: the Test Runner schedules reap() this often
REAP_AGE_S = 3600  # Q11.30, FR-23: nothing labelled survives past this
REAP_MARGIN_S = 300  # ADR-71: the scheduled reaper removes what is past its wall clock plus this
MAX_SANDBOXES = 3  # ADR-56: at most this many run at once on the test host (provisional)
MIN_RUN_S = 60  # a queued run stops waiting while this much of its budget is left to run in

# ADR-71: what each runtime proves. A `development-runc` result proves the flow, never isolation.
ISOLATION = {"runsc": "gvisor", "runc": "development-runc"}
DEVELOPMENT_ENVS = ("development", "test")


class SpecRejected(Exception):
    pass


class SandboxBusy(Exception):
    """No slot came free in time. Nothing was started, so nothing needs cleaning up."""


class SandboxCancelled(Exception):
    """Cancelled before the container started. Whatever was created has been removed."""


def sandbox_spec(environment: str, runtime: str, image: str) -> SandboxSpec:
    """ADR-71: the spec from settings. `runsc` anywhere; `runc` only in development and test,
    refused otherwise like scripted skills. The image must be named by digest, never a tag."""
    if runtime not in ISOLATION:
        raise SpecRejected(f"unknown sandbox runtime {runtime!r}")
    if runtime == "runc" and environment not in DEVELOPMENT_ENVS:
        raise SpecRejected(f"the runc sandbox runtime is refused when AISRE_ENV is {environment}")
    if not _pinned(image):
        raise SpecRejected("the sandbox image must be named by digest")
    return SandboxSpec(image=image, runtime=runtime, environment=environment)


def isolation(spec: SandboxSpec) -> str:
    """The label every result carries (ADR-71)."""
    return ISOLATION[spec.runtime]


def _pinned(image: str) -> bool:
    """A registry digest (`name@sha256:<hex>`) or a local content-addressed image ID."""
    return bool(
        re.fullmatch(r"[\w./:-]+@sha256:[0-9a-f]{64}|sha256:[0-9a-f]{64}", image)
        or ("@sha256:" in image and not image.startswith("-"))  # the reference placeholder
    )


class SandboxLimiter:
    """The cap on sandboxes running at once, in one Test Runner process (unit tests, and a
    single process). Across processes the Test Runner uses PgSandboxSlots (ADR-71), which has
    the same two methods. A run waits for a slot; the launcher never starts a fourth."""

    def __init__(self, limit: int = MAX_SANDBOXES):
        self.limit = limit
        self._slots = threading.BoundedSemaphore(limit)

    def acquire(self, wait_s: float, cancel: Optional[threading.Event] = None) -> bool:
        deadline = time.monotonic() + wait_s
        while True:
            left = deadline - time.monotonic()
            got = (
                self._slots.acquire(timeout=min(left, 1.0))
                if left > 0
                else self._slots.acquire(blocking=False)
            )
            if got:
                return True
            if left <= 0 or (cancel is not None and cancel.is_set()):
                return False

    def release(self) -> None:
        self._slots.release()


LIMITER = SandboxLimiter()


def launch_args(spec: SandboxSpec, run_id: str, workspace_volume: str) -> list[str]:
    """The exact container command the trusted launcher runs. Built only from SandboxSpec."""
    if spec.runtime not in ISOLATION:
        raise SpecRejected(f"unknown sandbox runtime {spec.runtime!r}")
    args = [
        "docker",
        "run",
        "--rm",
        "--interactive",  # the checkout arrives on standard input (ADR-71)
        "--name",
        f"aisre-test-{run_id}",
        f"--runtime={spec.runtime}",
        f"--network={spec.network}",
        "--read-only",  # image filesystem immutable
        f"--tmpfs=/tmp:rw,noexec,nosuid,nodev,size={spec.tmpfs_mb}m",
        f"--mount=type=volume,src={workspace_volume},dst=/workspace",
        f"--cpus={spec.cpus}",
        f"--memory={spec.memory}",
        f"--memory-swap={spec.memory}",  # no swap beyond the limit
        f"--pids-limit={spec.pids_limit}",
        f"--user={spec.user}",
        "--cap-drop=ALL",  # no Linux capabilities
        "--security-opt=no-new-privileges",  # no setuid escalation
        "--ipc=private",  # PID and UTS are private by default; Docker
        f"--stop-timeout={STOP_GRACE_S}",  # refuses --pid=private and --uts=private
        "--label=aisre.sandbox=1",
        f"--label=aisre.run={run_id}",
        f"--label=aisre.isolation={isolation(spec)}",
        "--env=AISRE_SANDBOX=1",  # the only variables: no secrets, ever (Q11.22)
        spec.image,
        "/harness/run",  # trusted harness from the image, not the workspace
    ]
    validate_args(args, development=spec.environment in DEVELOPMENT_ENVS)
    return args


def workspace_volume(run_id: str) -> str:
    return f"aisre-ws-{run_id}"


def volume_args(spec: SandboxSpec, run_id: str) -> list[str]:
    """Q11.27: the run's own workspace, a memory-backed volume, because tmpfs is what limits its size."""
    uid, gid = spec.user.split(":")
    return [
        "docker",
        "volume",
        "create",
        "--driver=local",
        "--opt=type=tmpfs",
        "--opt=device=tmpfs",
        f"--opt=o=size={spec.workspace_mb}m,uid={uid},gid={gid},nosuid,nodev",
        "--label=aisre.sandbox=1",
        f"--label=aisre.run={run_id}",
        workspace_volume(run_id),
    ]


# Every token launch_args may emit between `docker run` and the image. Anything else is refused,
# so a new way to weaken the launch fails here without anyone having listed it (Q11.23, Q11.31).
_ALLOWED_ARGS = re.compile(
    r"--rm|--interactive|--name|aisre-test-[\w-]+|--runtime=(?:runsc|runc)|--network=none|--read-only"
    r"|--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=\d+m|--mount=type=volume,src=[\w-]+,dst=/workspace"
    r"|--cpus=[\d.]+|--memory=\w+|--memory-swap=\w+|--pids-limit=\d+|--user=0*[1-9]\d*:0*[1-9]\d*"
    r"|--cap-drop=ALL|--security-opt=no-new-privileges|--ipc=private|--stop-timeout=\d+"
    r"|--label=aisre\.sandbox=1|--label=aisre\.run=[\w-]+|--env=AISRE_SANDBOX=1"
    r"|--label=aisre\.isolation=(?:gvisor|development-runc)"
)
_REQUIRED_ARGS = (
    "--network=none",
    "--read-only",
    "--cap-drop=ALL",
    "--security-opt=no-new-privileges",
    "--ipc=private",
)


def validate_args(args: list[str], *, development: bool = False) -> None:
    """Defence in depth: an allow-list, so only what launch_args builds can run, whatever the
    spelling (Q11.23, Q11.31). The list ends with the pinned image and the trusted harness.
    Exactly one runtime: `runsc`, or `runc` only for a development or test spec, and the
    isolation label must say which (ADR-71)."""
    if args[:2] != ["docker", "run"] or len(args) < 4:
        raise SpecRejected("the launcher runs only `docker run`")
    for a in args[2:-2]:
        if not _ALLOWED_ARGS.fullmatch(a):
            raise SpecRejected(
                f"option not on the allow-list: {a.split('=')[0]}"
            )  # never the value
    if not _pinned(args[-2]) or args[-1] != "/harness/run":
        raise SpecRejected("a pinned image and the trusted harness are required")
    missing = [a for a in _REQUIRED_ARGS if a not in args]
    if missing:
        raise SpecRejected(f"required: {', '.join(missing)} (ADR-60)")
    runtimes = [a.split("=", 1)[1] for a in args if a.startswith("--runtime=")]
    labels = [a for a in args if a.startswith("--label=aisre.isolation=")]
    if len(runtimes) != 1 or len(labels) != 1:
        raise SpecRejected("exactly one runtime and one isolation label are required")
    if runtimes[0] == "runc" and not development:
        raise SpecRejected("the runc runtime is for development and test only (ADR-71)")
    if labels[0] != f"--label=aisre.isolation={ISOLATION[runtimes[0]]}":
        raise SpecRejected("the isolation label must match the runtime (ADR-71)")
    if not any(a.startswith("--user=") for a in args):
        raise SpecRejected("sandbox must not run as root")


# ---------------------------------------------------------------------------
# Trusted test harness result (Q11.38–Q11.48)
# ---------------------------------------------------------------------------
class TestOutcome(str, Enum):
    __test__ = False
    PASSED = "passed"  # reproduced before, gone after, no regressions, all suites ran
    FAILED = "failed"  # reproduced, but the failure remains or a regression appeared
    NOT_REPRODUCED = (
        "not_reproduced"  # the original failure never appeared: no valid comparison (Q11.39)
    )
    INCONCLUSIVE = "inconclusive"  # required suites couldn't run (Q11.48)
    TIMEOUT = "timeout"  # wall clock or per-test limit hit (Q11.47)
    CRASHED = "crashed"  # harness died, or no valid result bundle for this patch (Q11.46)


REQUIRED_SUITES = (
    "reproduction",
    "unit",
    "integration",
    "smoke",
    "error_signature",
)  # fixed here, not by the bundle


@dataclass(frozen=True)
class SuiteResult:
    name: str  # REQUIRED_SUITES, plus any extra evidence
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
    reproduction_before: Optional[bool]  # failure observed on the base revision?
    reproduction_after: Optional[bool]  # failure observed after the patch?
    suites: tuple[SuiteResult, ...]
    new_error_signatures: tuple[str, ...]  # errors after the patch not present before (Q11.41)
    exit_code: Optional[int]  # the harness's own: 0 if it wrote a bundle, even with failing tests
    duration_s: float
    timed_out: bool
    log_artifact_sha256: Optional[str]

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(self.__dict__, default=lambda o: o.__dict__, sort_keys=True).encode()
        ).hexdigest()


def classify(b: Optional[ResultBundle], expected_patch_sha256: str) -> TestOutcome:
    """The one place a test run becomes a verdict. The order is the table in Artifact 11.5:
    the first match wins, and `passed` is reached only if nothing before it matched."""
    if b is None or b.exit_code is None:
        return TestOutcome.CRASHED
    if b.patch_sha256 != expected_patch_sha256:
        return TestOutcome.CRASHED  # results for a different patch are not results
    if b.timed_out:
        return TestOutcome.TIMEOUT  # before the exit code: a timeout kill exits 137
    if b.exit_code != 0:
        return TestOutcome.CRASHED
    if b.reproduction_before is False:
        return TestOutcome.NOT_REPRODUCED
    if b.reproduction_before is None or b.reproduction_after is None:
        return TestOutcome.INCONCLUSIVE
    if any(s.required and not s.ran for s in b.suites):
        return TestOutcome.INCONCLUSIVE
    for name in REQUIRED_SUITES:  # by name: the bundle's `required` flag can't excuse one
        found = [s for s in b.suites if s.name == name]
        if not found or any(not s.ran or s.passed + s.failed == 0 for s in found):
            return TestOutcome.INCONCLUSIVE
    if (
        b.reproduction_after
        or b.new_error_signatures
        or any(s.failed or s.errors for s in b.suites)
    ):
        return TestOutcome.FAILED
    return TestOutcome.PASSED


# ---------------------------------------------------------------------------
# The harness's bundle
# ---------------------------------------------------------------------------
def read_bundle(stdout: str, exit_code: int) -> Optional[ResultBundle]:
    """The bundle is the last line of the harness's output, the only channel left: `--rm` removes
    the container and the tmpfs volume empties with it. The exit code is what Docker reported,
    not what the bundle claims. Anything unreadable is no bundle, which classifies as crashed.
    Test output is free text, so a secret a test printed is masked before it leaves (ST-24)."""
    try:
        d = json.loads(stdout.strip().splitlines()[-1])
        suites = tuple(
            SuiteResult(**{**s, "errors": [mask_secrets(e) for e in s.get("errors", [])]})
            for s in d.pop("suites")
        )
        return ResultBundle(
            **{
                **d,
                "suites": suites,
                "exit_code": exit_code,
                "new_error_signatures": tuple(mask_secrets(e) for e in d["new_error_signatures"]),
            }
        )
    except (IndexError, KeyError, TypeError, ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# What the sandbox receives (ADR-71): protected paths, the checkout, the harness plan
# ---------------------------------------------------------------------------
# The launcher refuses these before anything starts, and the harness refuses them again: the
# tests, their configuration, CI, policies, the harness and the sandbox, secrets. `deploy/**` is
# a draft-time boundary only (check_boundaries), pending T2.10 (ADR-71).
LAUNCH_PROTECTED = [p for p in PROTECTED if p != "deploy/**"]
MAX_PATCH_BYTES = 1024 * 1024
REVISION = re.compile(r"[\w][\w./-]{0,199}")

# The harness plan per repository: the required suites, and the reproduction built from the
# evidence (Q11.38): start the service at the revision with its own configuration, send the
# failing request, expect the symptom. Platform data, never from the patch or the model.
HARNESS_PLANS: dict[str, dict[str, Any]] = {
    "orders-api": {
        "suites": {
            "unit": "tests/unit",
            "integration": "tests/integration",
            "smoke": "tests/smoke",
        },
        "reproduction": {
            "serve": ["python", "-B", "-m", "orders.app"],
            "port": 18080,
            "ready_path": "/healthz",
            "request": {
                "method": "POST",
                "path": "/v1/orders",
                "body": {"items": [{"sku": "sku-1", "qty": 1, "unit_price": "100.00"}]},
            },
            "symptom_status": 500,
        },
    }
}


def refuse_protected(canonical: dict[str, Any]) -> None:
    """T2.4: a patch touching a protected path is refused before any volume or container
    exists. `canonical` is Patch.canonical(), as the Test Runner reads it from storage."""
    for f in canonical.get("files") or []:
        path = str(f.get("path", ""))
        rooted = "/" + path
        if (
            not path
            or path.startswith("/")
            or ".." in path.split("/")
            or any(
                fnmatch.fnmatchcase(path, p)
                or fnmatch.fnmatchcase(rooted, p)
                or fnmatch.fnmatchcase(rooted, "*/" + p)
                for p in LAUNCH_PROTECTED
            )
        ):
            raise PatchRejected("protected_path", f"{path} is protected; nothing was started")


def checkout(repo: Path, base_revision: str, *, run=subprocess.run) -> tuple[str, bytes]:
    """T2.2: the exact base commit, as `git archive` of the Test Runner's own mirror. No `.git`,
    no history, no remote, no credential. Returns the full commit ID and the tar."""
    if not REVISION.fullmatch(base_revision):
        raise SpecRejected("not a revision name")
    git = ["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null"]
    commit = run(
        [
            *git,
            "rev-parse",
            "--verify",
            "--quiet",
            "--end-of-options",
            f"{base_revision}^{{commit}}",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    tar = run([*git, "archive", "--format=tar", commit], check=True, capture_output=True).stdout
    return commit, tar


def build_payload(
    spec: SandboxSpec,
    run_id: str,
    commit: str,
    checkout_tar: bytes,
    canonical_patch: bytes,
    plan: dict[str, Any],
) -> bytes:
    """The tar piped to the harness: run.json, patch.json (the patch's canonical bytes, which the
    harness hashes itself) and checkout.tar. Refused if it can't fit the workspace."""
    run_json = {
        "run_id": run_id,
        "base_revision": commit,
        "image_digest": spec.image,
        "spec_version": spec.spec_version,
        "per_test_s": spec.per_test_s,
        "protected": LAUNCH_PROTECTED,
        "plan": plan,
    }
    if len(canonical_patch) > MAX_PATCH_BYTES:
        raise SpecRejected("the patch is too large")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as t:
        for name, data in (
            ("run.json", json.dumps(run_json).encode()),
            ("patch.json", canonical_patch),
            ("checkout.tar", checkout_tar),
        ):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o444
            t.addfile(info, io.BytesIO(data))
    payload = buf.getvalue()
    if 3 * len(payload) > spec.workspace_mb * 1024 * 1024:  # unpacked, before and after copies
        raise SpecRejected("the checkout does not fit the workspace")
    return payload


# ---------------------------------------------------------------------------
# The launcher's run, kill and cleanup (Q11.27, Q11.28, Q11.30)
# ---------------------------------------------------------------------------
def timed_out_bundle(
    spec: SandboxSpec,
    run_id: str,
    expected_patch_sha256: str,
    base_revision: str,
    exit_code: Optional[int],
    duration_s: float,
) -> ResultBundle:
    return ResultBundle(
        run_id=run_id,
        patch_sha256=expected_patch_sha256,
        base_revision=base_revision,
        image_digest=spec.image,
        spec_version=spec.spec_version,
        harness_version="",
        reproduction_before=None,
        reproduction_after=None,
        suites=(),
        new_error_signatures=(),
        exit_code=exit_code,
        duration_s=duration_s,
        timed_out=True,
        log_artifact_sha256=None,
    )


def kill(run_id: str, *, run=subprocess.run) -> None:
    """Cancellation (WR-15): kill the run's container now. The run's own `finally` removes it
    and its volume; calling this before the container exists does nothing."""
    run(["docker", "kill", f"aisre-test-{run_id}"], capture_output=True, timeout=30)


def run_sandbox(
    spec: SandboxSpec,
    run_id: str,
    expected_patch_sha256: str,
    base_revision: str,
    *,
    popen=subprocess.Popen,
    run=subprocess.run,
    limiter: Any = LIMITER,
    slot_wait_s: float = 0,
    payload: Optional[bytes] = None,
    budget_s: Optional[float] = None,
    cancel: Optional[threading.Event] = None,
) -> Optional[ResultBundle]:
    """The Test Runner's launcher step: a slot, workspace, run under the wall clock, cleanup. On the
    deadline it writes the bundle itself, or classify would see none and call a timeout a crash.
    With every slot taken it waits `slot_wait_s`, then raises SandboxBusy before creating anything.

    `budget_s` (T2.6): the queued wait and the run share it, so a run that waited gets the rest,
    never more than `wall_clock_s`; it stops waiting while MIN_RUN_S is left. `payload` goes to
    the harness on standard input. `cancel`, once set, stops a wait or a start (SandboxCancelled);
    a running container is stopped by `kill`."""
    queued = time.monotonic()
    if budget_s is not None:
        slot_wait_s = max(0.0, budget_s - MIN_RUN_S)
    if not limiter.acquire(slot_wait_s, cancel):
        if cancel is not None and cancel.is_set():
            raise SandboxCancelled(run_id)
        raise SandboxBusy(f"{limiter.limit} sandboxes are already running")
    wall: float = spec.wall_clock_s
    if budget_s is not None:
        wall = min(wall, budget_s - (time.monotonic() - queued))
    name, volume = f"aisre-test-{run_id}", workspace_volume(run_id)
    started = time.monotonic()
    try:
        run(volume_args(spec, run_id), check=True, capture_output=True)
        if cancel is not None and cancel.is_set():
            raise SandboxCancelled(run_id)
        proc = popen(
            launch_args(spec, run_id, volume),
            stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        sent = {} if payload is None else {"input": payload}
        try:
            out, err = proc.communicate(timeout=wall, **sent)
            text = out.decode(errors="replace") if isinstance(out, bytes) else out
            bundle = read_bundle(text, proc.returncode)
            if bundle is None and err:
                tail = err.decode(errors="replace") if isinstance(err, bytes) else err
                log.warning("sandbox %s gave no bundle: %s", run_id, mask_secrets(tail[-500:]))
            return bundle
        except subprocess.TimeoutExpired:
            try:
                run(
                    ["docker", "stop", f"--time={STOP_GRACE_S}", name],
                    capture_output=True,
                    timeout=STOP_GRACE_S + 20,
                )
                proc.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()  # a stuck stop; the reaper removes what's left
                proc.communicate()
            return timed_out_bundle(
                spec,
                run_id,
                expected_patch_sha256,
                base_revision,
                proc.returncode,
                time.monotonic() - started,
            )
    finally:  # also on cancellation; a failure here is the reaper's
        try:
            run(["docker", "rm", "--force", name], capture_output=True)
            run(["docker", "volume", "rm", "--force", volume], capture_output=True)
        finally:
            limiter.release()


def _older(lines: str, now: datetime, max_age_s: float = REAP_AGE_S) -> list[str]:
    """Names from "<name> <RFC 3339 time>" lines, for those past `max_age_s`."""
    pairs = [line.split(None, 1) for line in lines.splitlines() if line.strip()]
    return [
        n for n, t in pairs if (now - datetime.fromisoformat(t.strip())).total_seconds() > max_age_s
    ]


def reap(now: datetime, *, run=subprocess.run, max_age_s: float = REAP_AGE_S) -> int:
    """Q11.30: remove containers and volumes labelled aisre.sandbox=1 older than `max_age_s`, at
    most 60 minutes; the scheduled job passes the wall clock plus REAP_MARGIN_S (ADR-71). Returns
    how many were left over, for aisre_sandboxes_leftover, which should be zero. `now` is timezone-aware."""

    def docker(*args: str) -> str:
        return run(["docker", *args], check=True, capture_output=True, text=True).stdout

    label = "label=aisre.sandbox=1"
    containers = docker("ps", "--all", "--quiet", "--filter", label).split()
    stale = (
        _older(docker("inspect", "--format={{.Id}} {{.Created}}", *containers), now, max_age_s)
        if containers
        else []
    )
    volumes = docker("volume", "ls", "--quiet", "--filter", label).split()
    stale_volumes = (
        _older(
            docker("volume", "inspect", "--format={{.Name}} {{.CreatedAt}}", *volumes),
            now,
            max_age_s,
        )
        if volumes
        else []
    )
    if stale:
        docker("rm", "--force", *stale)  # first: a volume in use can't be removed
    if stale_volumes:
        docker("volume", "rm", "--force", *stale_volumes)
    return len(stale) + len(stale_volumes)
