"""Artifact 11.1, 11.2, 13.5 T2.8 (FR-17 to FR-20, FR-40, FR-47, FR-48, ADR-69, ADR-72): the
`draft_patch` activity, the Patch Proposer (ADR-51).

One candidate per call, each its own action. The workflow drafts candidate 1, and the next only
when the previous one's test failed, up to MAX_PATCH_CANDIDATES (ADR-69). Candidates follow the
smallest reversal first (FR-47): configuration revert, commit revert, release rollback, starting
from the kind the diagnosis proposed. A release rollback changes no files and isn't drafted as a
patch here: a person selects the release (`no_allowed_fix`).

Code decides everything but the edit. Only an established diagnosis with a proposed action gets
a patch (FR-17). The repository is the incident's service's own, named after it, so a patch can't
reach another service (FR-48). The base revision is the head of its default branch in the review
system, the revision the release process deploys; the recent changes there, and the current
content of the files they changed that the kind may edit, are given to the model as data. The
model's answer (PatchProposal, through the Model Gateway with the prompt's `standard` alias,
budget reserved per attempt) is checked by code, and a failed check is fed back within the step's
three attempts (ADR-27):

- the service and kind are the incident's and the candidate's (FR-40, FR-48);
- `target_ref` is the revision just before one of the recent changes, which is the commit it
  reverts; each edited file is one that commit changed;
- every evidence ID is one of the incident's records;
- code builds the structured diff from the content (build_patch_from_contents) and checks it with
  check_boundaries, the one protected-path rule (ADR-72), with what that commit changed read from
  the review system: a deployment file only as an exact restoration, which for the reference
  incident means the diff changes exactly the configuration value the commit changed (FR-20).

The gateway has scanned the answer (ADR-59; file content for secrets only). Once diffed, the
lines the patch adds are scanned for links and commands too, except a verified restoration's
(validation.check_added_lines, T2.10), and unsafe output stops the step with no retry. The patch
is then stored with its action, artifacts and `aisre.action.proposed.v1` (aisre.record.patches),
with the hypothesis, evidence, diff, expected effect, risk and undo plan it carries (FR-19).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Optional, Protocol
from uuid import UUID, uuid5

from sqlalchemy import Connection, Engine
from sqlalchemy.exc import DBAPIError
from temporalio import activity

from aisre.agent.agent_contracts import (
    DiagnosisDraft,
    PatchProposal,
    SkillFailure,
    SkillResult,
    SkillUsage,
)
from aisre.agent.skills import reasoning as r
from aisre.agent.skills.activities import ModelSkills
from aisre.agent.validation import UnsafeOutput, check_added_lines
from aisre.evidence.snapshots import SnapshotStore
from aisre.record import patches as rec
from aisre.remediation.action import ApprovalPolicy
from aisre.remediation.patch import (
    DEFAULT_POLICY,
    EditPolicy,
    FileChange,
    Patch,
    PatchRejected,
    build_patch_from_contents,
    check_boundaries,
    may_edit,
)
from aisre.remediation.review import CommitInfo, RepoReader, ReviewUnavailable
from aisre.workflow import activities as act

PATCH_NAMESPACE = UUID("3b9e2f61-0c4d-5a87-b1e2-7f5d9c3a6e04")
CANDIDATE_KINDS = ("config_revert", "commit_revert", "release_rollback")  # smallest first
HISTORY = 5  # recent changes on the default branch shown to the model
MAX_FILES = 3  # as the edit policy's max_files
MAX_FILE_CHARS = 8_000  # a larger file isn't offered: the model can't return it whole
# What "recovered" means, fixed before execution (FR-30, ADR-66). Provisional, for HTTP 500s.
RECOVERY = [
    {
        "metric": "http_5xx_rate",
        "comparison": "<=",
        "threshold": 0.01,
        "baseline_note": "provisional: the service's error rate back under 1% (ADR-66)",
    }
]


def action_id_for(incident_id: str, idempotency_key: str) -> UUID:
    """The same step gives the same action, so a retried activity stores nothing twice."""
    return uuid5(PATCH_NAMESPACE, f"{incident_id}:{idempotency_key}")


def candidate_kind(proposed: str, candidate_no: int) -> str | None:
    """FR-47: from the diagnosis's proposed kind, one step larger per candidate."""
    if proposed not in CANDIDATE_KINDS:
        return None
    n = CANDIDATE_KINDS.index(proposed) + candidate_no - 1
    return CANDIDATE_KINDS[n] if 0 <= n < len(CANDIDATE_KINDS) else None


class PatchStore(Protocol):
    async def drafted(self, action_id: UUID) -> rec.Stored | None: ...
    async def save(self, patch: rec.NewPatch) -> rec.Stored: ...


class PgPatchStore:
    """The record over PostgreSQL, and the two objects in the object store, written first."""

    def __init__(self, engine: Engine, blobs: SnapshotStore) -> None:
        self.engine, self.blobs = engine, blobs

    async def _tx(self, fn: Callable[[Connection], Any]) -> Any:
        def run() -> Any:
            try:
                with self.engine.begin() as conn:
                    return fn(conn)
            except DBAPIError as e:
                raise ConnectionError(f"database: {type(e.orig).__name__}") from e

        return await asyncio.to_thread(run)

    async def drafted(self, action_id: UUID) -> rec.Stored | None:
        found: rec.Stored | None = await self._tx(lambda c: rec.drafted(c, action_id))
        return found

    async def save(self, patch: rec.NewPatch) -> rec.Stored:
        canonical_key, review_key = rec.patch_keys(patch.incident_id, patch.action_id)
        await asyncio.to_thread(self.blobs.put, canonical_key, patch.canonical, "application/json")
        await asyncio.to_thread(
            self.blobs.put, review_key, patch.review, "application/vnd.aisre.patch+json"
        )
        now = datetime.now(UTC)
        stored: rec.Stored = await self._tx(lambda c: rec.store_patch(c, patch, now))
        return stored


@dataclass(frozen=True)
class Plan:
    """What code read from the review system before asking: the base and what may be edited."""

    repository: str
    branch: str
    base: str
    history: list[CommitInfo]
    files: dict[str, str]  # path -> content at the base revision


@dataclass
class Built:
    patch: Patch
    commit: CommitInfo
    target_ref: str
    target: str
    restored: dict[str, str] = field(default_factory=dict)
    novel: list[str] = field(default_factory=list)  # added lines no earlier revision had


def plan_for(
    reader: RepoReader, service: str, kind: str, policy: EditPolicy = DEFAULT_POLICY
) -> Plan:
    repository = service  # FR-48: the service's own repository, never one the model names
    branch, base = reader.default_branch(repository)
    history = reader.history(repository, base, HISTORY)
    files: dict[str, str] = {}
    for c in history:
        if not c.parents:
            continue  # a root commit: nothing before it to return to
        for path in sorted(c.files):
            if path in files or len(files) >= MAX_FILES or not may_edit(path, kind, policy):
                continue
            content = reader.content(repository, base, path)
            if content is not None and len(content) <= MAX_FILE_CHARS:
                files[path] = content
    return Plan(repository, branch, base, history, files)


def _item(ref: str, kind: str, value: Any) -> dict[str, Any]:
    return {"ref": ref, "kind": kind, "text": json.dumps(value, sort_keys=True), "refs": [ref]}


def context_items(plan: Plan, kind: str, diagnosis: DiagnosisDraft) -> list[dict[str, Any]]:
    items = [
        _item("candidate:kind", "candidate_kind", {"candidate_kind": kind}),
        r.data_item("diagnosis:current", "diagnosis", diagnosis),
    ]
    for c in plan.history:
        items.append(
            _item(
                f"change:{c.sha[:12]}",
                "recent_change",
                {
                    "commit": c.sha,
                    "parents": c.parents,
                    "refs": c.refs,
                    "message": c.message,
                    "files_changed": sorted(c.files),
                },
            )
        )
    for path, content in plan.files.items():
        items.append(
            _item(
                f"file:{path}",
                "file_at_base",
                {"path": path, "revision": plan.base, "content": content},
            )
        )
    return items


def lines_changed(p: Patch) -> tuple[int, int]:
    added = sum(1 for f in p.files for h in f.hunks for line in h.lines if line[:1] == "+")
    removed = sum(1 for f in p.files for h in f.hunks for line in h.lines if line[:1] == "-")
    return added, removed


class PatchSkill:
    """The `draft_patch` activity, for `Worker(activities=...)`: `.all()`."""

    def __init__(
        self,
        skills: ModelSkills,
        reader: RepoReader,
        store: PatchStore,
        policy: EditPolicy = DEFAULT_POLICY,
    ) -> None:
        self.skills, self.reader, self.store, self.policy = skills, reader, store, policy

    def all(self) -> list[Callable[..., Any]]:
        return [self.draft_patch]

    @activity.defn(name="draft_patch")
    async def draft_patch(self, inp: act.SkillIn) -> SkillResult[act.PatchOut]:
        action_id = action_id_for(inp.incident_id, inp.idempotency_key)
        earlier = await self.store.drafted(action_id)
        if earlier is not None:
            return SkillResult(output=_out(earlier), usage=SkillUsage())
        d = inp.diagnosis
        if d is None or inp.diagnosis_id is None:
            return _failure("store_failed", "incident or diagnosis not found")
        if d.outcome != "established" or d.proposed_action is None or d.leading_index is None:
            return _failure(
                "no_allowed_fix", "no patch from a diagnosis that isn't established (FR-17)"
            )
        kind = candidate_kind(d.proposed_action, inp.candidate_no)
        if kind is None or kind == "release_rollback":
            return _failure(
                "no_allowed_fix",
                f"candidate {inp.candidate_no}: a release rollback changes no files, so a person"
                " selects the release; no patch is drafted",
            )
        inputs = await self.skills.store.inputs(inp.incident_id, inp.evidence_ids)
        if inputs is None:
            return _failure("store_failed", "incident or input not found")
        service = inputs.context.service
        try:
            plan = await asyncio.to_thread(plan_for, self.reader, service, kind, self.policy)
        except ReviewUnavailable as e:
            return _failure("store_failed", f"repository unreadable: {e}")
        if not plan.files:
            return _failure(
                "no_allowed_fix",
                f"no recent change on {plan.branch} touches a file a {kind} may edit",
            )
        lead = d.hypotheses[d.leading_index]
        must = {f"evidence:{e}" for c in lead.claims for e in c.evidence_ids if e in inputs.by_id}
        holder: dict[str, Built] = {}

        def check(proposal: PatchProposal) -> Optional[str]:
            built = self._build(
                proposal, plan, kind, inp, str(inputs.context.environment), set(inputs.by_id)
            )
            if isinstance(built, str):
                return built
            holder["built"] = built
            return None

        asked = await self.skills._ask(
            "draft_patch",
            PatchProposal,
            inputs,
            inp.idempotency_key,
            check,
            context_items(plan, kind, d),
            must,
        )
        if isinstance(asked, SkillResult):
            return asked
        proposal, usage = asked
        checked = act.checked_result("draft_patch", proposal, usage, inp.incident_id)
        if checked.failure is not None:
            return checked
        built = holder["built"]
        try:
            check_added_lines(built.novel)
        except UnsafeOutput as e:
            return act.unsafe_result(
                "draft_patch", e.code, proposal.model_dump_json(), usage, inp.incident_id
            )
        stored = await self.store.save(
            self._record(
                action_id,
                inp,
                built,
                proposal,
                lead.statement,
                service,
                str(inputs.context.environment),
            )
        )
        return SkillResult(output=_out(stored), usage=usage)

    def _build(
        self,
        proposal: PatchProposal,
        plan: Plan,
        kind: str,
        inp: act.SkillIn,
        environment: str,
        known: set[UUID],
    ) -> Built | str:
        """The checked patch, or what to tell the model."""
        p = proposal.params
        if p.service != plan.repository:
            return f"params.service must be {plan.repository}: a patch changes only the incident's service"
        if p.action_kind != kind:
            return f"params.action_kind must be {kind}, this candidate's kind"
        target = self.reader.resolve(plan.repository, p.target_ref)
        commit = next((c for c in plan.history if c.parents and c.parents[0] == target), None)
        if target is None or commit is None:
            return (
                f"target_ref {p.target_ref!r} must be the revision just before one of the recent"
                " changes, as a commit ID or tag"
            )
        changed: list[FileChange] = []
        for edit in proposal.files:
            if edit.path not in commit.files:
                return f"{commit.sha[:12]} didn't change {edit.path}; edit only what it changed"
            if edit.path not in plan.files:
                return f"{edit.path} is not one of the files given at the base revision"
            unknown = [e for e in edit.evidence_ids if e not in known]
            if unknown:
                return f"evidence IDs not in the data blocks: {', '.join(map(str, unknown[:3]))}"
            if edit.new_content == plan.files[edit.path]:
                return f"the edit to {edit.path} changes nothing"
            one = build_patch_from_contents(
                old={edit.path: plan.files[edit.path]},
                new={edit.path: edit.new_content},
                rationale=edit.rationale,
                evidence=[str(e) for e in edit.evidence_ids],
                **self._meta(inp, plan, kind, environment, proposal.summary, commit.sha),
            )
            changed += one.files
        if not changed:
            return "the patch changes no file"
        patch = Patch(
            files=changed, **self._meta(inp, plan, kind, environment, proposal.summary, commit.sha)
        )
        try:
            restored = check_boundaries(patch, self.policy, revert=commit.revert_facts())
        except PatchRejected as e:
            return f"refused, {e.code}: {e.detail}"
        novel = [
            line[1:]
            for f in patch.files
            if f.path not in restored
            for h in f.hunks
            for line in h.lines
            if line[:1] == "+"
        ]
        return Built(patch, commit, p.target_ref, target, restored, novel)

    @staticmethod
    def _meta(
        inp: act.SkillIn, plan: Plan, kind: str, environment: str, summary: str, reverted: str
    ) -> dict[str, Any]:
        return {
            "incident_id": inp.incident_id,
            "diagnosis_id": str(inp.diagnosis_id),
            "candidate_no": inp.candidate_no,
            "action_kind": kind,
            "repository": plan.repository,
            "base_revision": plan.base,
            "target_service": plan.repository,
            "target_environment": environment,
            "summary": summary,
            "reverted_commit": reverted,
        }

    def _record(
        self,
        action_id: UUID,
        inp: act.SkillIn,
        b: Built,
        proposal: PatchProposal,
        hypothesis: str,
        service: str,
        environment: str,
    ) -> rec.NewPatch:
        """The action's text, FR-19: the hypothesis, the effect, the risk, the undo plan."""
        p, c = b.patch, b.commit
        added, removed = lines_changed(p)
        paths = ", ".join(f.path for f in p.files)
        what = "configuration" if p.action_kind == "config_revert" else "change"
        return rec.NewPatch(
            action_id=action_id,
            incident_id=UUID(inp.incident_id),
            diagnosis_id=UUID(str(inp.diagnosis_id)),
            kind=p.action_kind,
            target_deployment=f"{service}-{environment}",
            base_revision=p.base_revision,
            expected_after_revision=f"{p.base_revision}+patch:{p.digest[:16]}",
            description=proposal.summary[:500],
            expected_effect=(
                f"{service} in {environment} runs with {paths} as at {b.target_ref}"
                f" ({b.target[:12]}), before {c.sha[:12]}. The diagnosis says this ends the"
                f" failure: {hypothesis}"
            )[:500],
            risk=(
                f"Low and contained: a {what} revert of {c.sha[:12]} ('{c.message}'), {len(p.files)}"
                f" file(s), +{added} -{removed} lines, tested in the sandbox before it was proposed."
                f" Anything else {c.sha[:12]} meant to change in these files is undone with it."
            )[:500],
            undo_plan=(
                f"Revert this pull request's merge, which puts {paths} back as at {c.sha[:12]}."
                " The platform applies nothing in this release (ADR-44): people merge and deploy."
            )[:500],
            recovery_criteria=RECOVERY,
            policy_version=f"{self.policy.policy_version}|{ApprovalPolicy().version}",
            patch_sha256=p.digest,
            canonical=p.canonical_bytes(),
            review=p.model_dump_json().encode(),
            files_changed=len(p.files),
            lines_added=added,
            lines_removed=removed,
            change_author=c.author or None,
        )


def _out(s: rec.Stored) -> act.PatchOut:
    return act.PatchOut(str(s.action_id), s.patch_sha256, s.kind, s.hold_seconds, True)


def _failure(
    code: Literal["no_allowed_fix", "store_failed"], detail: str
) -> SkillResult[act.PatchOut]:
    failure = SkillFailure(skill="draft_patch", code=code, detail=detail[:500])
    return SkillResult(failure=failure, usage=SkillUsage())
