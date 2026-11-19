"""Artifact 13.7: CI's blocking strict mypy step checks exactly the modules pyproject.toml's
strict override names, so neither list can gain or lose a module without the other."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[2]


def strict_modules() -> set[str]:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    (strict,) = [o for o in config["tool"]["mypy"]["overrides"] if o.get("disallow_untyped_defs")]
    return set(strict["module"])


def ci_paths() -> list[str]:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    step = ci.split("- name: Types, strict and blocking", 1)[1].split("- name:", 1)[0]
    command = step.split("--follow-imports=silent", 1)[1]
    return re.findall(r"src/aisre\S*", command)


def as_modules(path: str) -> set[str]:
    module = path.removeprefix("src/").removesuffix(".py").replace("/", ".")
    return {module, f"{module}.*"} if (ROOT / path).is_dir() else {module}


def test_ci_strict_paths_exist():
    assert [p for p in ci_paths() if not (ROOT / p).exists()] == []


def test_ci_strict_paths_match_the_pyproject_override():
    from_ci = set().union(*(as_modules(p) for p in ci_paths()))
    assert from_ci == strict_modules()
