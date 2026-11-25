"""Artifact 11.1, 11.2, 11.7, 13.5 T2.8 (FR-18, ADR-72): the review system, as the platform uses it.

The Patch Proposer reads the repository at its base revision here, and the worker opens the draft
pull request here once the fix has passed its test, with its draft-pull-request credential and
nothing more (11.7: propose, never merge or deploy). Patches reach the code only as draft pull
requests that people review and merge (FR-18); this client has no merge, push or commit call.

`ReviewClient` speaks the review system's HTTP API: for now Compose's fake
(`AISRE_REVIEW_ADAPTER=fake_review`, ops/fakes/review.py), which Settings refuses in production,
as it does every fake. A real code host gets its own adapter behind the same methods.

`ReviewActivities.open_draft_pull_request` is the workflow's activity: it opens the pull request
for an action whose test passed (never before), with the patch, the diagnosis's hypothesis, the
evidence, the expected effect, risk and undo plan (FR-19), and the test result with its isolation
label (ADR-71), then records the pull request on the patch (`patches.review_ref`, written once)
and `aisre.action.review_opened.v1`, in one transaction. The same key gives the same pull
request: the review system dedupes on Idempotency-Key and the record on `review_ref`.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import quote, urlencode
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import Engine, text
from temporalio import activity
from temporalio.exceptions import ApplicationError

from aisre.config import Settings
from aisre.contracts import ActionReviewOpenedData
from aisre.record.incidents import append_event
from aisre.remediation.patch import FileChangeFacts, Patch, RevertFacts
from aisre.workflow import activities as act

TIMEOUT_S = 10.0
SOURCE = "/aisre/worker/review"
ACTOR = "system:patch-proposer"


class ReviewUnavailable(Exception):
    """The review system didn't answer, or answered with an error."""


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    parents: list[str]
    message: str
    refs: list[str]
    files: dict[str, FileChangeFacts]
    author: str = ""  # the commit's author, as the review system names them (ADR-49)

    def revert_facts(self) -> RevertFacts:
        return RevertFacts(self.sha, self.files)


@dataclass(frozen=True)
class DraftPullRequest:
    title: str
    body: str
    base: str  # the branch it targets
    base_revision: str  # the commit the patch was generated and tested against
    head: str  # the branch name the review system creates for it
    patch: dict[str, Any]  # Patch.canonical()
    labels: list[str] = field(default_factory=list)
    links: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str
    draft: bool
    state: str
    merged: bool
    required_reviewers: list[str]


class RepoReader(Protocol):
    def default_branch(self, repo: str) -> tuple[str, str]: ...
    def history(self, repo: str, ref: str, limit: int) -> list[CommitInfo]: ...
    def resolve(self, repo: str, ref: str) -> str | None: ...
    def content(self, repo: str, ref: str, path: str) -> str | None: ...


class ReviewClient:
    """The review system's API, with the worker's draft-pull-request token."""

    def __init__(self, url: str, token: SecretStr, timeout_s: float = TIMEOUT_S) -> None:
        self.url, self.token, self.timeout_s = url.rstrip("/"), token, timeout_s

    def _call(
        self, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None
    ) -> tuple[int, Any]:
        req = urllib.request.Request(
            self.url + path,
            data=None if body is None else json.dumps(body).encode(),
            method=method,
            headers={
                "Authorization": f"Bearer {self.token.get_secret_value()}",
                "Content-Type": "application/json",
                **(headers or {}),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"null")
            except ValueError:
                return e.code, None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise ReviewUnavailable(f"review system: {type(e).__name__}") from None

    def _get(self, path: str) -> Any:
        status, body = self._call("GET", path)
        if status == 404:
            return None
        if status != 200:
            raise ReviewUnavailable(f"review system: HTTP {status} on GET")
        return body

    @staticmethod
    def _repo(repo: str) -> str:
        return f"/v1/repos/{quote(repo, safe='')}"

    def default_branch(self, repo: str) -> tuple[str, str]:
        """The default branch and the commit at its head: what the release process deploys."""
        found = self._get(self._repo(repo))
        if not found or not found.get("head"):
            raise ReviewUnavailable(f"no repository {repo}")
        return str(found["default_branch"]), str(found["head"])

    def history(self, repo: str, ref: str, limit: int) -> list[CommitInfo]:
        found = self._get(f"{self._repo(repo)}/commits?{urlencode({'ref': ref, 'limit': limit})}")
        return [_commit(c) for c in (found or {}).get("commits", [])]

    def resolve(self, repo: str, ref: str) -> str | None:
        found = self._get(f"{self._repo(repo)}/commits/{quote(ref, safe='')}")
        return str(found["sha"]) if found else None

    def content(self, repo: str, ref: str, path: str) -> str | None:
        found = self._get(f"{self._repo(repo)}/contents/{quote(path)}?{urlencode({'ref': ref})}")
        return str(found["content"]) if found else None

    def open_draft(self, repo: str, pr: DraftPullRequest, key: str) -> PullRequest:
        body = {
            "title": pr.title,
            "body": pr.body,
            "base": pr.base,
            "base_revision": pr.base_revision,
            "head": pr.head,
            "draft": True,  # always: the platform never opens a pull request for merging
            "patch": pr.patch,
            "labels": pr.labels,
            "links": pr.links,
        }
        status, out = self._call(
            "POST", f"{self._repo(repo)}/pulls", body, {"Idempotency-Key": key}
        )
        if status in (409, 422):
            raise ApplicationError(
                f"review system refused the pull request: {(out or {}).get('message')}",
                type="PullRequestRefused",
                non_retryable=True,
            )
        if status not in (200, 201):
            raise ReviewUnavailable(f"review system: HTTP {status} opening a pull request")
        return _pull(out)

    def pull(self, repo: str, number: int) -> PullRequest | None:
        found = self._get(f"{self._repo(repo)}/pulls/{number}")
        return _pull(found) if found else None


def _commit(c: dict[str, Any]) -> CommitInfo:
    return CommitInfo(
        sha=str(c["sha"]),
        parents=[str(p) for p in c.get("parents", [])],
        message=str(c.get("message", "")),
        refs=[str(r) for r in c.get("refs", [])],
        author=str(c.get("author") or ""),
        files={
            str(f["path"]): FileChangeFacts(f.get("before_sha256"), f.get("after_sha256"))
            for f in c.get("files", [])
        },
    )


def _pull(p: dict[str, Any]) -> PullRequest:
    return PullRequest(
        number=int(p["number"]),
        url=str(p["url"]),
        draft=bool(p["draft"]),
        state=str(p["state"]),
        merged=bool(p.get("merged", False)),
        required_reviewers=[str(r) for r in p.get("required_reviewers", [])],
    )


def client_from_settings(settings: Settings) -> ReviewClient | None:
    """None when no review system is configured. Settings already refuses the fake in
    production; this checks again, as the worker does for scripted skills."""
    if settings.review_adapter is None or settings.review_url is None:
        return None
    if settings.review_adapter == "fake_review" and settings.env == "production":
        raise RuntimeError("the fake review system is refused when AISRE_ENV is production")
    assert settings.review_token is not None  # Settings: all three or none
    return ReviewClient(settings.review_url, settings.review_token)


# ---------------------------------------------------------------------------
# The pull request's text (FR-19)
# ---------------------------------------------------------------------------
def pull_request_text(
    patch: Patch,
    action: dict[str, Any],
    hypothesis: str,
    test: dict[str, Any],
) -> tuple[str, str]:
    """Title and body. Everything comes from the record; the diff is Patch.unified_diff(), with
    each hunk's rationale and evidence IDs above it."""
    title = f"[aisre] {action['kind'].replace('_', ' ')}: {patch.target_service}: {patch.summary}"
    criteria = "; ".join(
        f"{c['metric']} {c['comparison']} {c['threshold']}" for c in action["recovery_criteria"]
    )
    isolation = str(test["isolation"])
    proof = (
        "proves the flow, not isolation: it ran under runc on a development host (ADR-71)"
        if isolation == "development-runc"
        else "ran under gVisor on the test host"
    )
    lines = [
        "Drafted by the incident platform. A recommendation: people review, merge and deploy it;"
        " the platform never merges (ADR-44, FR-18).",
        "",
        f"Incident: {patch.incident_id}",
        f"Action: {action['action_id']} (diagnosis {patch.diagnosis_id}, candidate"
        f" {patch.candidate_no})",
        f"Hypothesis: {hypothesis}",
        f"Evidence: {', '.join(sorted({e for f in patch.files for h in f.hunks for e in h.evidence_ids}))}",
        "",
        f"Summary: {patch.summary}",
        f"Expected effect: {action['expected_effect']}",
        f"Risk: {action['risk']}",
        f"Undo plan: {action['undo_plan']}",
        f"Recovery criteria: {criteria}",
        "",
        f"Test: {test['outcome']}, failure reproduced first: {'yes' if test['reproduced'] else 'no'};"
        f" test run {test['test_run_id']}; patch {patch.digest}; isolation {isolation}, which"
        f" {proof}.",
        "",
        "```diff",
        patch.unified_diff(),
        "```",
    ]
    return title[:200], "\n".join(lines)


# ---------------------------------------------------------------------------
# The workflow's activity
# ---------------------------------------------------------------------------
class BlobReader(Protocol):
    def get(self, key: str) -> bytes: ...


class ReviewActivities:
    """`open_draft_pull_request`, over the record, the object store and the review system."""

    def __init__(self, engine: Engine, blobs: BlobReader, client: ReviewClient) -> None:
        self.engine, self.blobs, self.client = engine, blobs, client

    def all(self) -> list[Any]:
        return [self.open_draft_pull_request]

    @activity.defn(name="open_draft_pull_request")
    async def open_draft_pull_request(self, inp: act.ReviewIn) -> act.ReviewOut:
        return await asyncio.to_thread(self._open, inp)

    def _open(self, inp: act.ReviewIn) -> act.ReviewOut:
        loaded = self._load(inp)
        if isinstance(loaded, act.ReviewOut):
            return loaded  # opened and recorded already
        action, patch, test, hypothesis = loaded
        title, body = pull_request_text(patch, action, hypothesis, test)
        branch, _head = self.client.default_branch(patch.repository)
        pr = self.client.open_draft(
            patch.repository,
            DraftPullRequest(
                title=title,
                body=body,
                base=branch,
                base_revision=patch.base_revision,
                head=f"aisre/{action['action_id']}",
                patch=patch.canonical(),
                labels=["aisre", "draft-recommendation", str(test["isolation"])],
                links={"test_result": f"/v1/actions/{action['action_id']}"},
            ),
            inp.review_key,
        )
        if not pr.draft or pr.merged:
            raise ApplicationError("the review system did not keep it a draft", non_retryable=True)
        ref = f"{patch.repository}#{pr.number}"
        with self.engine.begin() as conn:
            conn.execute(
                text("SELECT 1 FROM incidents WHERE incident_id = :i FOR UPDATE"),
                {"i": patch.incident_id},
            )
            done = conn.execute(
                text("SELECT review_ref FROM patches WHERE action_id = :a"),
                {"a": action["action_id"]},
            ).scalar_one()
            if done is None:
                conn.execute(
                    text("UPDATE patches SET review_ref = :r WHERE action_id = :a"),
                    {"r": ref, "a": action["action_id"]},
                )
                append_event(
                    conn,
                    UUID(patch.incident_id),
                    "aisre.action.review_opened.v1",
                    ACTOR,
                    ActionReviewOpenedData(
                        action_id=action["action_id"],
                        patch_sha256=patch.digest,
                        review_ref=ref,
                        review_url=pr.url,
                        draft=True,
                        test_run_id=test["test_run_id"],
                        isolation=test["isolation"],
                        required_reviewers=pr.required_reviewers,
                    ),
                    occurred_at=datetime.now(UTC),
                    source=SOURCE,
                )
        return act.ReviewOut(ref, pr.url)

    def _load(
        self, inp: act.ReviewIn
    ) -> act.ReviewOut | tuple[dict[str, Any], Patch, dict[str, Any], str]:
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    text(
                        "SELECT a.action_id, a.incident_id, a.kind, a.status, a.expected_effect, "
                        "a.risk, a.undo_plan, a.recovery_criteria, p.patch_sha256, p.review_ref, "
                        "ar.storage_key, h.statement "
                        "FROM actions a JOIN patches p ON p.action_id = a.action_id "
                        "JOIN artifacts ar ON ar.artifact_id = p.diff_artifact_id "
                        "JOIN diagnoses d ON d.diagnosis_id = a.diagnosis_id "
                        "LEFT JOIN hypotheses h ON h.hypothesis_id = d.leading_hypothesis_id "
                        "WHERE a.action_id = :a"
                    ),
                    {"a": inp.action_id},
                )
                .mappings()
                .first()
            )
            if row is None:
                raise ApplicationError("no such action with a patch", non_retryable=True)
            test = (
                conn.execute(
                    text(
                        "SELECT test_run_id, outcome, reproduced, isolation FROM test_runs "
                        "WHERE test_run_id = :t AND action_id = :a"
                    ),
                    {"t": inp.test_run_id, "a": inp.action_id},
                )
                .mappings()
                .first()
            )
        if row["review_ref"] is not None:
            return act.ReviewOut(str(row["review_ref"]), "")
        # Only a fix whose failure reproduced and whose test passed is ever proposed.
        if test is None or str(test["outcome"]) != "passed" or not test["reproduced"]:
            raise ApplicationError(
                "no passing test for this action: no pull request",
                type="NotTested",
                non_retryable=True,
            )
        if str(row["status"]) != "testing":
            raise ApplicationError(f"action is {row['status']}", non_retryable=True)
        review_key = str(row["storage_key"]).removesuffix(".json") + ".review.json"
        patch = Patch.model_validate_json(self.blobs.get(review_key))
        if patch.digest != str(row["patch_sha256"]).strip():
            raise ApplicationError(
                "the stored patch changed", type="PatchChanged", non_retryable=True
            )
        action = {
            "action_id": str(row["action_id"]),
            "kind": str(row["kind"]),
            "expected_effect": str(row["expected_effect"]),
            "risk": str(row["risk"]),
            "undo_plan": str(row["undo_plan"]),
            "recovery_criteria": list(row["recovery_criteria"]),
        }
        test_d = {
            "test_run_id": str(test["test_run_id"]),
            "outcome": str(test["outcome"]),
            "reproduced": bool(test["reproduced"]),
            "isolation": str(test["isolation"]),
        }
        return action, patch, test_d, str(row["statement"] or "")
