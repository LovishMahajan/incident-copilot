"""T1.13 (ADR-63): the first prompt versions, and the check that refuses edits to merged ones."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

from aisre.agent import agent_contracts
from aisre.gateway.gateway_contracts import TASK_MODEL_ALIAS
from aisre.retrieval.context import PromptRegistry

ROOT = Path(__file__).parents[2]
spec = importlib.util.spec_from_file_location("check_prompts", ROOT / "scripts/check_prompts.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)

SCHEMAS = {
    "hypothesise": "HypothesesOut",
    "diagnose": "DiagnosisDraft",
    "critique": "CriticVerdict",
    "draft_patch": "PatchProposal",
}


def test_each_task_has_a_first_version_the_loader_reads():
    reg = PromptRegistry(ROOT / "prompts", activations={t: [1] for t in TASK_MODEL_ALIAS})
    for task, alias in TASK_MODEL_ALIAS.items():
        p = reg.current(task)
        assert (p.ref, p.output_schema_name, p.model_alias) == (f"{task}@1", SCHEMAS[task], alias)
        assert hasattr(agent_contracts, p.output_schema_name)
        assert "data" in p.text and "JSON" in p.text and len(p.sha256) == 64


def test_the_load_check_passes_on_the_repository():
    assert check.load_errors(ROOT) == []
    assert check.main([]) == 0


def test_a_header_the_loader_cannot_read_fails(tmp_path):
    (tmp_path / "prompts/diagnose").mkdir(parents=True)
    (tmp_path / "prompts/diagnose/v1.md").write_text("model_alias: default\n---\nDiagnose.\n")
    (tmp_path / "prompts/critique").mkdir()
    (tmp_path / "prompts/critique/v1.md").write_text("Just text, no header.\n")
    assert [e.split(":")[0] for e in check.load_errors(tmp_path)] == [
        "prompts/critique/v1.md",
        "prompts/diagnose/v1.md",
    ]


@pytest.mark.parametrize(
    ("diff", "edited"),
    [
        ("A\tprompts/diagnose/v2.md", []),
        ("M\tprompts/diagnose/v1.md", ["M prompts/diagnose/v1.md"]),
        ("D\tprompts/critique/v3.md", ["D prompts/critique/v3.md"]),
        (
            "R100\tprompts/diagnose/v1.md\tprompts/diagnose/v2.md",
            ["R prompts/diagnose/v1.md -> prompts/diagnose/v2.md"],
        ),
        ("C100\tprompts/diagnose/v1.md\tprompts/diagnose/v2.md", []),
        ("M\tprompts/README.md", []),
    ],
)
def test_only_new_versions_pass(diff, edited):
    assert check.edited_versions(diff) == edited


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_against_a_base_commit_an_edit_fails_and_a_new_version_passes(tmp_path, monkeypatch):
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    shutil.copytree(ROOT / "prompts", tmp_path / "prompts")
    git("init", "-q", "-b", "main")
    git("-c", "user.name=t", "-c", "user.email=t@example.test", "commit", "-qam", "x", "--allow-empty")  # fmt: skip
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@example.test", "commit", "-qm", "v1")
    monkeypatch.setattr(check, "ROOT", tmp_path)

    v1 = tmp_path / "prompts/diagnose/v1.md"
    (tmp_path / "prompts/diagnose/v2.md").write_text(v1.read_text() + "\nOne more rule.\n")
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@example.test", "commit", "-qm", "v2")
    assert check.main(["--base", "HEAD~1"]) == 0

    v1.write_text(v1.read_text().replace("plain sentences", "short sentences"))
    git("-c", "user.name=t", "-c", "user.email=t@example.test", "commit", "-qam", "edit v1")
    assert check.main(["--base", "HEAD~1"]) == 1
