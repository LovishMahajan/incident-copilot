"""Artifact 13.5 T2.3: build the runnable orders-api as a git repository, deterministically.

    python ops/reference-app/build_repo.py <dest>

Creates <dest> as a git repository with the reference incident's history (tests/fixtures/
reference-incident: release.json, config-diff.txt):

- `cfg-41` (also `v2.14.0`): the source in orders-api/, with ORDER_TAX_RATE_BPS=825. Tests pass.
- `cfg-42` (also `v2.14.1`), on `main`: "Update tax rate config to match finance sheet (8.25%)",
  changing only deploy/production.env to ORDER_TAX_RATE_BPS=8.25. Tests fail with the ValueError.

Author, committer and dates are fixed, so the same source gives the same commit IDs on every
machine. They are not the fixture's `7d3e91a`: a commit ID is a hash of its content and can't be
chosen. Prints `<tag> <commit>` for each tag. Standard library and `git` only.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parent / "orders-api"
CONFIG = "deploy/production.env"
GOOD, BAD = "ORDER_TAX_RATE_BPS=825\n", "ORDER_TAX_RATE_BPS=8.25\n"
IDENTITY = {
    "GIT_AUTHOR_NAME": "synthetic-engineer-a",
    "GIT_AUTHOR_EMAIL": "synthetic-engineer-a@example.invalid",
    "GIT_COMMITTER_NAME": "synthetic-engineer-a",
    "GIT_COMMITTER_EMAIL": "synthetic-engineer-a@example.invalid",
}
HISTORY = [  # (date, message, tags)
    ("2026-10-01T09:00:00+00:00", "orders-api 2.14.0", ["v2.14.0", "cfg-41"]),
    (
        "2026-10-20T13:38:40+00:00",
        "Update tax rate config to match finance sheet (8.25%)",
        ["v2.14.1", "cfg-42"],
    ),
]


def git(dest: Path, *args: str, date: str | None = None) -> str:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(dest),  # no user or system git configuration
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        **IDENTITY,
    }
    if date:
        env |= {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    return subprocess.run(
        ["git", "-C", str(dest), *args], check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


def build(dest: Path) -> dict[str, str]:
    if dest.exists():
        raise SystemExit(f"{dest} exists; remove it first")
    shutil.copytree(SOURCE, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    config = dest / CONFIG
    if GOOD not in config.read_text():
        raise SystemExit(f"{CONFIG} must hold {GOOD.strip()}")
    git(dest, "init", "--quiet", "--initial-branch=main")
    git(dest, "config", "core.autocrlf", "false")
    tags: dict[str, str] = {}
    for n, (date, message, names) in enumerate(HISTORY):
        if n == 1:
            config.write_text(config.read_text().replace(GOOD, BAD))
        git(dest, "add", "--all")
        git(dest, "commit", "--quiet", "--no-gpg-sign", "-m", message, date=date)
        for name in names:
            git(dest, "tag", name)
            tags[name] = git(dest, "rev-parse", "HEAD")
    return tags


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    for tag, commit in build(Path(argv[0])).items():
        print(tag, commit)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
