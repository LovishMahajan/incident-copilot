"""Artifact 13.5 T2.5 (ADR-71): the real launcher against Docker, under `runc`.

Each run has every launch flag (no network, read-only root, user 65534, no capabilities,
no-new-privileges, cgroup limits, bounded tmpfs, size-limited workspace, wall clock). Under
`runc` these prove the flow, never isolation: `system: launch flags work under gVisor` stays for
the test host (T2.1). Shown here: the reference failure reproduces at the bad revision and the
revert passes (a deployment file restored exactly, by the one protected-path rule, ADR-72); a
base that doesn't fail (the reference repository's `tolerant` branch) is `not_reproduced` and a
fix that doesn't fix is `failed`; a long test hits the wall clock; a cancel kills the container; the reaper removes a
leftover; nothing labelled aisre.sandbox=1 is left afterwards.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aisre.remediation import sandbox as sb
from aisre.remediation.patch import Patch, build_patch_from_contents

CONFIG = "deploy/production.env"


def labelled() -> list[str]:
    containers = subprocess.run(
        ["docker", "ps", "--all", "--quiet", "--filter", "label=aisre.sandbox=1"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    volumes = subprocess.run(
        ["docker", "volume", "ls", "--quiet", "--filter", "label=aisre.sandbox=1"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    return containers + volumes


@pytest.fixture(scope="module", autouse=True)
def nothing_left_after(sandbox_image):
    yield
    assert labelled() == [], "a sandbox container or volume outlived the tests"


def show(repo: Path, rev: str, path: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "show", f"{rev}:{path}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def revert_patch(
    repo: Path,
    rev: str,
    old_line: str,
    new_line: str,
    path: str = CONFIG,
    reverts: str | None = "cfg-42",
) -> Patch:
    """An edit at `rev`. A deployment file may only be restored: `reverts` names the commit."""
    old = show(repo, rev, path)
    commit = sb.checkout(repo, rev)[0]
    return build_patch_from_contents(
        old={path: old},
        new={path: old.replace(old_line, new_line)},
        rationale="put the tax rate back in basis points",
        evidence=["ev-config-diff"],
        incident_id="inc-1",
        diagnosis_id="diag-1",
        candidate_no=1,
        action_kind="config_revert",
        repository="orders-api",
        base_revision=commit,
        target_service="orders-api",
        target_environment="production",
        summary="Revert ORDER_TAX_RATE_BPS to 825",
        reverted_commit=sb.checkout(repo, reverts)[0] if reverts else None,
    )


def payload_for(spec: sb.SandboxSpec, repo: Path, p: Patch, run_id: str) -> tuple[str, bytes]:
    """What the Test Runner does before a slot: the one protected rule, then the checkout."""
    facts = sb.revert_facts(repo, p.reverted_commit) if p.reverted_commit else None
    restored = sb.refuse_protected(p.canonical(), facts)
    commit, tar = sb.checkout(repo, p.base_revision)
    plan = sb.HARNESS_PLANS["orders-api"]
    return commit, sb.build_payload(spec, run_id, commit, tar, p.canonical_bytes(), plan, restored)


def launch(image: str, repo: Path, p: Patch, spec: sb.SandboxSpec | None = None, **kw):
    spec = spec or sb.sandbox_spec("test", "runc", image)
    run_id = uuid.uuid4().hex[:12]
    commit, payload = payload_for(spec, repo, p, run_id)
    bundle = sb.run_sandbox(spec, run_id, p.digest, commit, payload=payload, **kw)
    return run_id, bundle


def test_the_reference_failure_reproduces_and_the_revert_passes(sandbox_image, reference_repo):
    repo, _ = reference_repo
    p = revert_patch(repo, "cfg-42", "ORDER_TAX_RATE_BPS=8.25", "ORDER_TAX_RATE_BPS=825")
    _, b = launch(sandbox_image, repo, p)
    assert b is not None and b.reproduction_before is True and b.reproduction_after is False
    assert b.patch_sha256 == p.digest and b.image_digest == sandbox_image
    assert {s.name for s in b.suites} >= set(sb.REQUIRED_SUITES)
    assert sb.classify(b, p.digest) == sb.TestOutcome.PASSED


def test_a_base_that_does_not_fail_is_not_reproduced(sandbox_image, reference_repo):
    """The same revert on the `tolerant` branch, whose code parses 8.25: nothing fails at its head,
    so the patch is never tested. On `main` a harmless edit leaves the failure: `failed`."""
    repo, _ = reference_repo
    p = revert_patch(repo, "tolerant-42", "=8.25", "=825", reverts="tolerant-42")
    _, good = launch(sandbox_image, repo, p)
    assert good is not None and good.reproduction_before is False and good.suites == ()
    assert sb.classify(good, p.digest) == sb.TestOutcome.NOT_REPRODUCED
    q = revert_patch(
        repo, "cfg-42", '"""Settings', '"""The settings', path="orders/settings.py", reverts=None
    )
    _, bad = launch(sandbox_image, repo, q)
    assert bad is not None and bad.reproduction_before is True and bad.reproduction_after is True
    assert sb.classify(bad, q.digest) == sb.TestOutcome.FAILED


def test_a_deploy_edit_that_is_not_a_restoration_is_refused_before_anything_starts(
    sandbox_image, reference_repo
):
    repo, _ = reference_repo
    p = revert_patch(repo, "cfg-42", "LOG_LEVEL=INFO", "LOG_LEVEL=DEBUG")
    with pytest.raises(sb.PatchRejected) as e:
        launch(sandbox_image, repo, p)
    assert e.value.code == "not_a_restoration" and labelled() == []


def test_a_patch_touching_tests_is_refused_before_anything_starts(sandbox_image, reference_repo):
    repo, _ = reference_repo
    path = "tests/smoke/test_smoke.py"
    p = revert_patch(repo, "cfg-42", '"32.48"', '"32.47"', path=path, reverts=None)
    with pytest.raises(sb.PatchRejected, match="protected"):
        launch(sandbox_image, repo, p)
    assert labelled() == []


@pytest.fixture(scope="module")
def slow_repo(reference_repo, tmp_path_factory) -> Path:
    """The reference repository plus a commit whose unit suite never finishes."""
    repo = tmp_path_factory.mktemp("slow") / "orders-api"
    shutil.copytree(reference_repo[0], repo)
    (repo / "tests/unit/test_slow.py").write_text(
        "import time, unittest\n\n\nclass Slow(unittest.TestCase):\n"
        "    def test_hangs(self):\n        time.sleep(600)\n"
    )
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "HOME": str(repo),
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    }
    for args in (["add", "--all"], ["commit", "-q", "--no-gpg-sign", "-m", "a slow test"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)
    return repo


def test_a_long_test_hits_the_wall_clock(sandbox_image, slow_repo):
    p = revert_patch(slow_repo, "main", "ORDER_TAX_RATE_BPS=8.25", "ORDER_TAX_RATE_BPS=825")
    spec = sb.SandboxSpec(
        image=sandbox_image, runtime="runc", environment="test", wall_clock_s=8, per_test_s=300
    )
    started = time.monotonic()
    run_id, b = launch(sandbox_image, slow_repo, p, spec)
    assert b is not None and b.timed_out and sb.classify(b, p.digest) == sb.TestOutcome.TIMEOUT
    assert time.monotonic() - started < 8 + sb.STOP_GRACE_S + 15
    assert not [x for x in labelled() if run_id in x]


def test_cancel_kills_the_container(sandbox_image, slow_repo):
    p = revert_patch(slow_repo, "main", "ORDER_TAX_RATE_BPS=8.25", "ORDER_TAX_RATE_BPS=825")
    spec = sb.SandboxSpec(image=sandbox_image, runtime="runc", environment="test", wall_clock_s=300)
    run_id = uuid.uuid4().hex[:12]
    commit, payload = payload_for(spec, slow_repo, p, run_id)
    cancel, result = threading.Event(), {}

    def go():
        result["bundle"] = sb.run_sandbox(
            spec, run_id, p.digest, commit, payload=payload, cancel=cancel
        )

    worker = threading.Thread(target=go)
    worker.start()
    name = f"aisre-test-{run_id}"
    for _ in range(100):
        running = subprocess.run(
            ["docker", "ps", "--quiet", "--filter", f"name={name}"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        if running:
            break
        time.sleep(0.2)
    assert running, "the sandbox never started"
    killed_at = time.monotonic()
    cancel.set()
    sb.kill(run_id)
    worker.join(30)
    assert not worker.is_alive() and time.monotonic() - killed_at < 15
    assert sb.classify(result["bundle"], p.digest) == sb.TestOutcome.CRASHED  # killed: no bundle
    assert not [x for x in labelled() if run_id in x]


def test_the_reaper_removes_a_leftover(sandbox_image):
    run_id = f"leftover-{uuid.uuid4().hex[:8]}"
    spec = sb.sandbox_spec("test", "runc", sandbox_image)
    subprocess.run(sb.volume_args(spec, run_id), check=True, capture_output=True)
    subprocess.run(
        [
            "docker",
            "create",
            "--name",
            f"aisre-test-{run_id}",
            "--label=aisre.sandbox=1",
            f"--label=aisre.run={run_id}",
            "--network=none",
            sandbox_image,
        ],
        check=True,
        capture_output=True,
    )
    assert len([x for x in labelled()]) >= 2
    now = datetime.now(UTC)
    assert sb.reap(now, max_age_s=600) == 0  # young: left alone
    later = now + timedelta(seconds=spec.wall_clock_s + sb.REAP_MARGIN_S + 1)
    assert sb.reap(later, max_age_s=spec.wall_clock_s + sb.REAP_MARGIN_S) == 2
    assert labelled() == []


@pytest.mark.system
def test_launch_flags_work_under_gvisor(sandbox_image, reference_repo):
    """T2.5's `system` test: the same run under runsc. Only a gVisor host can pass it."""
    runtimes = subprocess.run(
        ["docker", "info", "--format", "{{json .Runtimes}}"], capture_output=True, text=True
    ).stdout
    if '"runsc"' not in runtimes:
        pytest.skip("needs the gVisor test host (T2.1): this Docker has no runsc runtime")
    repo, _ = reference_repo
    p = revert_patch(repo, "cfg-42", "ORDER_TAX_RATE_BPS=8.25", "ORDER_TAX_RATE_BPS=825")
    spec = sb.sandbox_spec("production", "runsc", sandbox_image)
    _, b = launch(sandbox_image, repo, p, spec)
    assert sb.classify(b, p.digest) == sb.TestOutcome.PASSED
