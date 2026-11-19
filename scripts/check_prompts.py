"""ADR-63 (T1.13): CI refuses an edit to a merged prompt version.

    uv run python scripts/check_prompts.py --base <git ref>

A prompt version is `prompts/<skill>/v<N>.md`, immutable once merged: a change is a new file,
`v<N+1>.md`. Against `--base` (the pull request's target, or the commit before a push), this
fails if any version that exists there was modified, deleted or renamed. It also loads every
version with the PromptRegistry, so a header the loader can't read fails here, not in a worker.
Without `--base`, only the load check runs.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = "prompts"
VERSION_FILE = re.compile(r"^prompts/[^/]+/v\d+\.md$")
REQUIRED = ("output_schema_name", "model_alias", "evaluated_on")


def edited_versions(name_status: str) -> list[str]:
    """From `git diff --name-status` output: each merged version changed, removed or moved."""
    found = []
    for line in name_status.splitlines():
        status, *paths = line.split("\t")
        if status.startswith("A"):
            continue  # a new version is how a prompt changes
        old = paths[0]  # M, D, T: the path; R, C: the old path first
        if VERSION_FILE.match(old) and not status.startswith("C"):
            found.append(f"{status[0]} {' -> '.join(paths)}")
    return found


def load_errors(root: Path) -> list[str]:
    """Every version must have the header the loader expects (aisre.retrieval.context)."""
    from aisre.retrieval.context import PromptRegistry

    errors = []
    for path in sorted(root.glob(f"{PROMPTS}/*/v*.md")):
        head, sep, text = path.read_text(encoding="utf-8").partition("\n---\n")
        lines = [ln for ln in head.splitlines() if ln.strip()]
        keys = {ln.split(": ", 1)[0] for ln in lines if ": " in ln}
        if not sep or not text.strip() or len(keys) != len(lines) or set(REQUIRED) - keys:
            errors.append(f"{path.relative_to(root)}: needs {', '.join(REQUIRED)}, then ---")
    if not errors:
        PromptRegistry(root / PROMPTS)  # raises on anything else it can't read
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", help="git ref the merged prompt versions are read from")
    args = parser.parse_args(argv)
    problems = load_errors(ROOT)
    if args.base:
        diff = subprocess.run(
            ["git", "diff", "--name-status", "-M", f"{args.base}...HEAD", "--", PROMPTS],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        problems += [
            f"merged prompt version changed ({e}): add a new version instead (ADR-63)"
            for e in edited_versions(diff)
        ]
    for p in problems:
        print(p, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
