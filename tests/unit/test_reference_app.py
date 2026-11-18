"""Artifact 13.5 T2.3: the runnable orders-api fails at the bad revision and passes at the good one."""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "ops" / "reference-app" / "build_repo.py"


def build_repo():
    spec = importlib.util.spec_from_file_location("build_repo", BUILD)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    dest = tmp_path_factory.mktemp("ref") / "orders-api"
    return dest, build_repo().build(dest)


def checkout(repo: Path, rev: str, into: Path) -> Path:
    tar = subprocess.run(["git", "-C", str(repo), "archive", rev], check=True, capture_output=True)
    with tarfile.open(fileobj=io.BytesIO(tar.stdout)) as t:
        t.extractall(into, filter="data")
    return into


def run_suite(tree: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-t", "."],
        cwd=tree,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_the_history_matches_the_incident(repo):
    path, tags = repo
    assert tags["cfg-41"] == tags["v2.14.0"] and tags["cfg-42"] == tags["v2.14.1"]
    changed = subprocess.run(
        ["git", "-C", str(path), "diff", "--name-only", "cfg-41", "cfg-42"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert changed == ["deploy/production.env"]
    assert build_repo().build(path.parent / "again") == tags  # deterministic commit IDs


def test_fails_at_the_bad_revision_passes_at_the_good_one(repo, tmp_path):
    path, _ = repo
    bad = run_suite(checkout(path, "cfg-42", tmp_path / "bad"))
    good = run_suite(checkout(path, "cfg-41", tmp_path / "good"))
    assert bad.returncode != 0
    assert "ValueError: invalid literal for int() with base 10: '8.25'" in bad.stderr
    assert good.returncode == 0, good.stderr
