"""Artifact 11.1, 11.2: the patch schema, editable-file boundaries, and the diff and hashing strategy.

A patch is a structured diff against an exact source revision, with a rationale per hunk.
Its identity is a SHA-256 digest over a canonical form: same content against a different
revision is a different patch. Protected paths (tests, policies, the sandbox launcher, CI)
can never be edited by a generated patch, and a commit revert touches only what the commit changed.

`check_paths` is the one protected-path rule (ADR-72), used by `check_boundaries` when a patch is
drafted and by the Test Runner's launcher (sandbox.refuse_protected) before anything starts.
Deployment files (`deploy/**`) are protected with one exception: a `config_revert` that names
the commit it reverts may put a file that commit changed back exactly as it was before the commit,
provided nothing changed it since. That is "a commit revert touches only what the commit changed"
(ADR-48) at file level; any other edit under `deploy/`, a key or a file the commit didn't change,
or a value it didn't have, is refused. What the commit changed is read by code from the
repository (`RevertFacts`), never from the patch or the model.
"""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# Editable boundaries (Q11.4, Q11.6, Q11.34)
# ---------------------------------------------------------------------------
PROTECTED = [  # never editable by a generated patch, whatever the policy says
    "tests/**",
    "**/tests/**",
    "**/test_*.py",
    "**/*_test.py",
    "conftest.py",
    "**/conftest.py",
    "pytest.ini",
    "tox.ini",
    "pyproject.toml",
    "setup.cfg",
    ".github/**",
    ".gitlab-ci.yml",
    "ci/**",
    "Makefile",
    "security/**",
    "policies/**",
    "**/*.rego",
    "CODEOWNERS",
    ".pre-commit-config.yaml",
    "sandbox/**",
    "harness/**",
    "deploy/**",
    "Dockerfile",
    "**/Dockerfile",
    "**/*.lock",
    "**/.env",
    "**/secrets*",
    "**/*.pem",
    "**/*.key",
]
# Protected, except that a config_revert may restore a file here to its content before the commit
# it reverts (check_paths). A path that also matches any other protected pattern stays refused.
RESTORABLE = ["deploy/**"]


class EditPolicy(Strict):
    """Per action kind, which paths a patch may touch. Owned by the security lead; versioned."""

    policy_version: str
    allowed: dict[str, list[str]]  # action_kind -> glob patterns
    max_files: int = 3
    max_changed_lines: int = 40


DEFAULT_POLICY = EditPolicy(
    policy_version="edit-policy@2",  # @2 (ADR-72): deploy/*.env, as restorations only
    allowed={
        "config_revert": [
            "config/*.yaml",
            "config/*.yml",
            "config/*.toml",
            "config/*.json",
            "deploy/*.env",
        ],
        "commit_revert": ["src/*.py", "app/*.py", "config/*"],
        "release_rollback": [],  # a rollback changes no files: it selects a previous release
    },
)


def _editable(path: str, patterns: list[str]) -> bool:
    """Anchored at the repository root. fnmatch's `*` crosses `/`, and `**` means nothing special."""
    return any(fnmatch.fnmatchcase(path, p) for p in patterns)


def _protected(path: str, patterns: Iterable[str] = PROTECTED) -> bool:
    """Unanchored, at any depth; the leading "/" lets `**/x` reach a file at the root too."""
    rooted = "/" + path
    return any(
        fnmatch.fnmatchcase(path, p)
        or fnmatch.fnmatchcase(rooted, p)
        or fnmatch.fnmatchcase(rooted, "*/" + p)
        for p in patterns
    )


_ALWAYS = [p for p in PROTECTED if p not in RESTORABLE]


class PatchRejected(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


def may_edit(path: str, action_kind: str, policy: EditPolicy | None = None) -> bool:
    """Whether `action_kind` could edit `path` at all: in its policy, and never on a path that is
    always protected. A restorable path still has to pass check_paths as an exact restoration."""
    if _protected(path, _ALWAYS) or (_protected(path) and action_kind != "config_revert"):
        return False
    return _editable(path, (policy or DEFAULT_POLICY).allowed.get(action_kind, []))


@dataclass(frozen=True)
class FileChangeFacts:
    """One file a commit changed: its SHA-256 before the commit (at the parent) and after it.
    None means it didn't exist there."""

    before_sha256: str | None
    after_sha256: str | None


@dataclass(frozen=True)
class RevertFacts:
    """What `commit` changed, read by code from the repository (git, or the review system's
    repository API), never from the patch or the model."""

    commit: str
    files: Mapping[str, FileChangeFacts]


def check_paths(
    action_kind: str,
    files: Iterable[tuple[str, str, str]],
    reverted_commit: str | None,
    revert: RevertFacts | None,
) -> dict[str, str]:
    """The one protected-path rule (ADR-72), for the draft (check_boundaries) and the launcher
    (sandbox.refuse_protected). `files` is (path, old_sha256, new_sha256) per file. Returns the
    protected files the patch restores, path -> new_sha256, which the harness allows and checks
    again. Raises PatchRejected for anything else on a protected path."""
    restored: dict[str, str] = {}
    for path, old, new in files:
        if not path or path.startswith("/") or ".." in path.split("/"):
            raise PatchRejected("bad_path", path or "(empty)")
        if not _protected(path):
            continue
        if _protected(path, _ALWAYS) or action_kind != "config_revert":
            raise PatchRejected(
                "protected_path",
                f"{path} is protected (tests, policies, sandbox, CI, deployment, secrets)",
            )
        if reverted_commit is None or revert is None or revert.commit != reverted_commit:
            raise PatchRejected(
                "protected_path",
                f"{path} is protected: only a revert of a named commit restores it",
            )
        facts = revert.files.get(path)
        if facts is None:
            raise PatchRejected("outside_commit", f"{reverted_commit} didn't change {path}")
        if old != facts.after_sha256:
            raise PatchRejected("changed_since", f"{path} changed after {reverted_commit}")
        if facts.before_sha256 is None or new != facts.before_sha256:
            raise PatchRejected(
                "not_a_restoration",
                f"{path} must return exactly to its content before {reverted_commit}",
            )
        restored[path] = new
    return restored


# ---------------------------------------------------------------------------
# Patch schema (Q11.1–Q11.3, Q11.7)
# ---------------------------------------------------------------------------
class Hunk(Strict):
    old_start: int = Field(ge=0)
    old_lines: int = Field(ge=0)
    new_start: int = Field(ge=0)
    new_lines: int = Field(ge=0)
    lines: list[str]  # unified-diff lines, each starting with ' ', '-' or '+'
    rationale: str = Field(min_length=10, max_length=600)  # why this hunk (Q11.7)
    evidence_ids: list[str] = Field(min_length=1)  # what justifies it


class FileChange(Strict):
    path: str
    op: Literal["modify"]  # first version: no create, delete or rename
    old_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")  # content hash at the base revision
    new_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")  # content hash after applying
    hunks: list[Hunk] = Field(min_length=1)


class Patch(Strict):
    """Q11.1. Never a whole replacement file: a structured diff, reviewable hunk by hunk."""

    incident_id: str
    diagnosis_id: str
    candidate_no: int = Field(ge=1, le=3)  # Q11.11: at most 3 candidates per diagnosis
    action_kind: Literal["config_revert", "commit_revert", "release_rollback"]
    repository: str
    base_revision: str = Field(min_length=7)  # the exact commit it was generated against (Q11.5)
    target_service: str
    target_environment: str
    files: list[FileChange] = Field(default_factory=list)
    target_release: str | None = None  # release_rollback only
    reverted_commit: str | None = None  # the commit it undoes: commit_revert, and a config_revert
    # that restores a protected deployment file (ADR-72)
    summary: str = Field(min_length=10, max_length=1000)

    @model_validator(mode="after")
    def shape(self) -> Patch:
        if self.action_kind == "release_rollback":
            if self.files or not self.target_release:
                raise ValueError("a release rollback names a target release and changes no files")
        elif not self.files:
            raise ValueError("a patch must change at least one file")
        if self.action_kind == "commit_revert" and self.reverted_commit is None:
            raise ValueError("a commit revert names the reverted commit")
        if self.action_kind == "release_rollback" and self.reverted_commit is not None:
            raise ValueError("a release rollback reverts no commit")
        return self

    # -- identity (Q11.8) --------------------------------------------------------
    def canonical(self) -> dict:
        """Everything that defines what the patch does, and nothing else. The summary and
        rationales are excluded so rewording doesn't change identity; the content does."""
        return {
            "repository": self.repository,
            "base_revision": self.base_revision,
            "target_service": self.target_service,
            "target_environment": self.target_environment,
            "action_kind": self.action_kind,
            "target_release": self.target_release,
            "reverted_commit": self.reverted_commit,
            "files": [
                {
                    "path": f.path,
                    "op": f.op,
                    "old_sha256": f.old_sha256,
                    "new_sha256": f.new_sha256,
                    "hunks": [
                        {
                            "old_start": h.old_start,
                            "old_lines": h.old_lines,
                            "new_start": h.new_start,
                            "new_lines": h.new_lines,
                            "lines": h.lines,
                        }
                        for h in f.hunks
                    ],
                }
                for f in sorted(self.files, key=lambda f: f.path)
            ],
        }

    def canonical_bytes(self) -> bytes:
        """What the artifact store holds, so the dispatcher can hash the bytes it reads (ST-13)."""
        return json.dumps(self.canonical(), sort_keys=True, separators=(",", ":")).encode()

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def unified_diff(self) -> str:
        """Q11.2: what reviewers see — standard unified diff, with the rationale above each hunk."""
        out = [f"# patch {self.digest[:12]} against {self.base_revision} ({self.action_kind})"]
        for f in self.files:
            out += [f"--- a/{f.path}", f"+++ b/{f.path}"]
            for h in f.hunks:
                out.append(f"# why: {h.rationale} (evidence: {', '.join(h.evidence_ids)})")
                out.append(f"@@ -{h.old_start},{h.old_lines} +{h.new_start},{h.new_lines} @@")
                out += h.lines
        return "\n".join(out)


# ---------------------------------------------------------------------------
# Validation against boundaries (Q11.4, Q11.6, Q11.34)
# ---------------------------------------------------------------------------
def check_boundaries(
    p: Patch,
    policy: EditPolicy = DEFAULT_POLICY,
    commit_paths: list[str] | None = None,
    revert: RevertFacts | None = None,
) -> dict[str, str]:
    """`commit_paths` is what `reverted_commit` changed, and `revert` the same with each file's
    content hashes, both read by code from the repository at the base revision, never from the
    patch or the model. None means it couldn't be read. Returns check_paths' restorations."""
    if commit_paths is None and revert is not None:
        commit_paths = list(revert.files)
    allowed = policy.allowed.get(p.action_kind, [])
    changed = 0
    if len(p.files) > policy.max_files:
        raise PatchRejected("too_many_files", f"{len(p.files)} > {policy.max_files}")
    if p.action_kind == "commit_revert" and commit_paths is None:
        raise PatchRejected(
            "commit_unreadable", f"can't read the files {p.reverted_commit} changed"
        )
    restored = check_paths(
        p.action_kind,
        [(f.path, f.old_sha256, f.new_sha256) for f in p.files],
        p.reverted_commit,
        revert,
    )
    for f in p.files:
        if not _editable(f.path, allowed):
            raise PatchRejected("outside_policy", f"{f.path} not editable for {p.action_kind}")
        if p.reverted_commit is not None and f.path not in (commit_paths or []):
            raise PatchRejected("outside_commit", f"{p.reverted_commit} didn't change {f.path}")
        for h in f.hunks:
            changed += sum(1 for l in h.lines if l[:1] in "+-")
            if any(l[:1] not in " +-" for l in h.lines):
                raise PatchRejected("malformed_hunk", f.path)
    if changed > policy.max_changed_lines:
        raise PatchRejected("too_large", f"{changed} changed lines > {policy.max_changed_lines}")
    return restored


def build_patch_from_contents(
    *, old: dict[str, str], new: dict[str, str], rationale: str, evidence: list[str], **meta
) -> Patch:
    """Turn before/after file contents (e.g. a model's proposed edit) into a structured diff.
    The model never supplies hashes or hunks directly; code computes them from content."""
    files = []
    for path in sorted(new):
        a, b = old[path].splitlines(), new[path].splitlines()
        hunks = []
        for grp in difflib.SequenceMatcher(None, a, b).get_grouped_opcodes(3):
            i1, i2, j1, j2 = grp[0][1], grp[-1][2], grp[0][3], grp[-1][4]
            lines = []
            for tag, a1, a2, b1, b2 in grp:
                if tag == "equal":
                    lines += [" " + l for l in a[a1:a2]]
                if tag in ("replace", "delete"):
                    lines += ["-" + l for l in a[a1:a2]]
                if tag in ("replace", "insert"):
                    lines += ["+" + l for l in b[b1:b2]]
            hunks.append(
                Hunk(
                    old_start=i1 + 1,
                    old_lines=i2 - i1,
                    new_start=j1 + 1,
                    new_lines=j2 - j1,
                    lines=lines,
                    rationale=rationale,
                    evidence_ids=evidence,
                )
            )
        files.append(
            FileChange(
                path=path,
                op="modify",
                old_sha256=hashlib.sha256(old[path].encode()).hexdigest(),
                new_sha256=hashlib.sha256(new[path].encode()).hexdigest(),
                hunks=hunks,
            )
        )
    return Patch(files=files, **meta)
