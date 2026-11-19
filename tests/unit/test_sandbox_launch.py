"""Artifact 13.5 T2.2, T2.4, T2.5 (ADR-71): the runtime from settings, the isolation label, the
checkout on standard input, and a patch touching a protected path refused before anything starts,
by the same rule as the draft (patch.check_paths, ADR-72). The real launcher against Docker is tests/integration/test_sandbox_docker.py."""

from __future__ import annotations

import io
import json
import subprocess
import tarfile
import threading
from pathlib import Path

import pytest

from aisre.config import Settings
from aisre.remediation.patch import PROTECTED, PatchRejected
from aisre.remediation.sandbox import (
    HARNESS_PLANS,
    SandboxCancelled,
    SandboxLimiter,
    SandboxSpec,
    SpecRejected,
    build_payload,
    checkout,
    isolation,
    launch_args,
    refuse_protected,
    revert_facts,
    run_sandbox,
    sandbox_spec,
    validate_args,
)

IMAGE = "sha256:" + "a" * 64


# ---------------------------------------------------------------- runtime and label (ADR-71)
def test_production_requires_gvisor():
    with pytest.raises(SpecRejected, match="refused when AISRE_ENV is production"):
        sandbox_spec("production", "runc", IMAGE)
    spec = sandbox_spec("production", "runsc", IMAGE)
    assert isolation(spec) == "gvisor" and "--runtime=runsc" in launch_args(spec, "r1", "ws")
    with pytest.raises(SpecRejected):
        sandbox_spec("test", "kata", IMAGE)


@pytest.mark.parametrize("env", ["development", "test"])
def test_runc_in_development_keeps_every_flag_and_says_so(env):
    dev, prod = sandbox_spec(env, "runc", IMAGE), sandbox_spec("production", "runsc", IMAGE)
    a, b = launch_args(dev, "r1", "ws"), launch_args(prod, "r1", "ws")
    assert isolation(dev) == "development-runc"
    assert "--runtime=runc" in a and "--label=aisre.isolation=development-runc" in a
    same = [x for x in a if not x.startswith(("--runtime=", "--label=aisre.isolation="))]
    assert same == [x for x in b if not x.startswith(("--runtime=", "--label=aisre.isolation="))]


def test_the_launcher_refuses_runc_for_a_production_spec_however_built():
    with pytest.raises(SpecRejected):
        launch_args(SandboxSpec(runtime="runc", image=IMAGE), "r1", "ws")
    dev = launch_args(sandbox_spec("test", "runc", IMAGE), "r1", "ws")
    validate_args(dev, development=True)
    with pytest.raises(SpecRejected, match="development and test only"):
        validate_args(dev)
    relabelled = [
        "--label=aisre.isolation=gvisor" if x.startswith("--label=aisre.isolation") else x
        for x in dev
    ]
    with pytest.raises(SpecRejected, match="must match"):
        validate_args(relabelled, development=True)


@pytest.mark.parametrize("image", ["aisre/test-runner:latest", "aisre/test-runner", "sha256:abc"])
def test_the_image_is_named_by_digest(image):
    with pytest.raises(SpecRejected):
        sandbox_spec("test", "runc", image)


def test_settings_refuse_runc_in_production_and_an_unpinned_image():
    s = Settings.from_env(
        {"AISRE_ENV": "test", "AISRE_SANDBOX_RUNTIME": "runc", "AISRE_SANDBOX_IMAGE": IMAGE}
    )
    assert s.sandbox_runtime == "runc" and s.sandbox_image == IMAGE
    with pytest.raises(ValueError, match="runc"):
        Settings.from_env({"AISRE_ENV": "production", "AISRE_SANDBOX_RUNTIME": "runc"})
    with pytest.raises(ValueError):
        Settings.from_env({"AISRE_SANDBOX_IMAGE": "aisre/test-runner:latest"})
    assert Settings().sandbox_runtime == "runsc"


# ---------------------------------------------------------------- protected paths (T2.4)
@pytest.mark.parametrize(
    "path",
    [
        "tests/unit/test_pricing.py",
        "orders/tests/test_x.py",
        "conftest.py",
        "pyproject.toml",
        ".github/workflows/ci.yml",
        "policies/edit.rego",
        "harness/run",
        "sandbox/spec.py",
        "Dockerfile",
        "config/secrets.yaml",
        "../etc/passwd",
        "/etc/passwd",
    ],
)
def test_a_patch_touching_the_path_is_refused(path):
    with pytest.raises(PatchRejected) as e:
        refuse_protected({"files": [{"path": "config/app.yaml"}, {"path": path}]})
    assert e.value.code == ("bad_path" if path.endswith("etc/passwd") else "protected_path")


def test_a_deploy_edit_is_refused_by_the_launcher_without_a_reverted_commit():
    with pytest.raises(PatchRejected) as e:
        refuse_protected(
            {"action_kind": "config_revert", "files": [{"path": "deploy/production.env"}]}
        )
    assert e.value.code == "protected_path" and "nothing was started" in e.value.detail


# ---------------------------------------------------------------- checkout (T2.2)
def git(repo: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "HOME": str(repo),
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    }
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "a.txt").write_text("one\n")
    git(r, "add", "a.txt")
    git(r, "commit", "-q", "--no-gpg-sign", "-m", "one")
    git(r, "tag", "first")
    (r / "a.txt").write_text("two\n")
    git(r, "commit", "-q", "--no-gpg-sign", "-am", "two")
    return r


def members(tar: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(tar)) as t:
        return {m.name: t.extractfile(m).read() for m in t.getmembers() if m.isfile()}


def test_the_checkout_is_the_exact_commit_without_git(repo):
    commit, tar = checkout(repo, "first")
    assert commit == git(repo, "rev-parse", "first") and len(commit) == 40
    files = members(tar)
    assert files == {"a.txt": b"one\n"} and not any(n.startswith(".git") for n in files)


@pytest.mark.parametrize("rev", ["--output=/tmp/x", "-h", "first;rm", "no-such-rev", ""])
def test_a_bad_revision_is_refused(repo, rev):
    with pytest.raises((SpecRejected, subprocess.CalledProcessError)):
        checkout(repo, rev)


def test_the_payload_carries_run_patch_and_checkout(repo):
    spec = sandbox_spec("test", "runc", IMAGE)
    commit, tar = checkout(repo, "main")
    payload = build_payload(spec, "r1", commit, tar, b'{"files":[]}', HARNESS_PLANS["orders-api"])
    files = members(payload)
    run = json.loads(files["run.json"])
    assert set(files) == {"run.json", "patch.json", "checkout.tar"}
    assert files["patch.json"] == b'{"files":[]}' and files["checkout.tar"] == tar
    assert run["base_revision"] == commit and run["image_digest"] == IMAGE
    assert run["protected"] == PROTECTED and run["per_test_s"] == spec.per_test_s
    assert run["restored"] == {}
    with pytest.raises(SpecRejected, match="fit"):
        build_payload(SandboxSpec(workspace_mb=0, image=IMAGE), "r1", commit, tar, b"{}", {})


# ---------------------------------------------------------------- the run (T2.5)
def recorder():
    calls = []

    def run(args, **kw):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    return calls, run


def test_a_cancelled_wait_starts_nothing():
    limiter, cancel = SandboxLimiter(1), threading.Event()
    assert limiter.acquire(0)
    calls, run = recorder()
    threading.Timer(0.1, cancel.set).start()
    with pytest.raises(SandboxCancelled):
        run_sandbox(
            SandboxSpec(),
            "r1",
            "0" * 64,
            "c" * 40,
            run=run,
            limiter=limiter,
            budget_s=600,
            cancel=cancel,
        )
    assert calls == []


def test_a_queued_run_gets_only_what_is_left_of_its_budget():
    limiter = SandboxLimiter(1)
    assert limiter.acquire(0)
    threading.Timer(0.3, limiter.release).start()
    seen = {}

    class Proc:
        returncode = 0

        def communicate(self, timeout=None, input=None):
            seen["timeout"], seen["input"] = timeout, input
            return b"", b""

    _, run = recorder()
    run_sandbox(
        SandboxSpec(wall_clock_s=900),
        "r1",
        "0" * 64,
        "c" * 40,
        run=run,
        popen=lambda *a, **k: Proc(),
        limiter=limiter,
        budget_s=100,
        payload=b"tar",
    )
    assert 99.9 > seen["timeout"] > 90 and seen["input"] == b"tar"


# ---------------------------------------------------------------- the one protected rule (T2.10)
@pytest.fixture
def deploy_repo(tmp_path):
    """deploy/app.env with two keys; commit `change` sets A=2. Then `later` edits B only."""
    r = tmp_path / "deploy-repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "deploy").mkdir()
    (r / "deploy/app.env").write_text("A=1\nB=1\n")
    (r / "deploy/other.env").write_text("C=1\n")
    git(r, "add", ".")
    git(r, "commit", "-q", "--no-gpg-sign", "-m", "root")
    (r / "deploy/app.env").write_text("A=2\nB=1\n")
    git(r, "commit", "-q", "--no-gpg-sign", "-am", "change")
    git(r, "tag", "change")
    return r


def sha(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()


def revert_of(commit: str, path: str, old: str, new: str) -> dict:
    return {
        "action_kind": "config_revert",
        "reverted_commit": commit,
        "files": [{"path": path, "old_sha256": sha(old), "new_sha256": sha(new)}],
    }


def test_revert_facts_read_what_the_commit_changed_from_the_mirror(deploy_repo):
    facts = revert_facts(deploy_repo, "change")
    assert facts.commit == git(deploy_repo, "rev-parse", "change")
    assert dict(facts.files) == {
        "deploy/app.env": type(facts.files["deploy/app.env"])(sha("A=1\nB=1\n"), sha("A=2\nB=1\n"))
    }
    root = revert_facts(deploy_repo, git(deploy_repo, "rev-parse", "change~1"))
    assert all(f.before_sha256 is None for f in root.files.values())  # nothing to restore to


def test_the_launcher_allows_the_exact_restoration_and_passes_it_to_the_harness(deploy_repo):
    facts = revert_facts(deploy_repo, "change")
    patch = revert_of(facts.commit, "deploy/app.env", "A=2\nB=1\n", "A=1\nB=1\n")
    assert refuse_protected(patch, facts) == {"deploy/app.env": sha("A=1\nB=1\n")}


@pytest.mark.parametrize(
    ("path", "old", "new", "code"),
    [
        ("deploy/app.env", "A=2\nB=1\n", "A=1\nB=2\n", "not_a_restoration"),  # another key
        ("deploy/app.env", "A=2\nB=1\n", "A=3\nB=1\n", "not_a_restoration"),  # another value
        ("deploy/other.env", "C=1\n", "C=2\n", "outside_commit"),  # a file it didn't change
        ("deploy/app.env", "A=2\nB=9\n", "A=1\nB=9\n", "changed_since"),  # edited after it
    ],
)
def test_the_launcher_refuses_any_other_deploy_edit(deploy_repo, path, old, new, code):
    facts = revert_facts(deploy_repo, "change")
    with pytest.raises(PatchRejected) as e:
        refuse_protected(revert_of(facts.commit, path, old, new), facts)
    assert e.value.code == code


def test_a_restoration_is_refused_for_another_kind_or_without_the_commit_facts(deploy_repo):
    facts = revert_facts(deploy_repo, "change")
    patch = revert_of(facts.commit, "deploy/app.env", "A=2\nB=1\n", "A=1\nB=1\n")
    for kind in ("commit_revert", "release_rollback"):
        with pytest.raises(PatchRejected, match="protected"):
            refuse_protected({**patch, "action_kind": kind}, facts)
    with pytest.raises(PatchRejected, match="protected"):
        refuse_protected(patch, None)  # the mirror couldn't say what the commit changed
    with pytest.raises(PatchRejected, match="protected"):
        refuse_protected({**patch, "reverted_commit": "0" * 40}, facts)  # facts of another commit
