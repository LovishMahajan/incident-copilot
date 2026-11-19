"""Artifact 11.2, 11.7, 13.5 (M5, ADR-75): GitHub as the review system.

GitHub REST API, version 2022-11-28 (`X-GitHub-Api-Version`), on github.com
(`https://api.github.com`) or GitHub Enterprise Server (`https://HOST/api/v3`). References:
repositories https://docs.github.com/en/rest/repos/repos, commits
https://docs.github.com/en/rest/commits/commits, contents
https://docs.github.com/en/rest/repos/contents, Git database https://docs.github.com/en/rest/git,
pull requests https://docs.github.com/en/rest/pulls/pulls. Tested against
recorded responses only (tests/unit/test_github.py), never a real account.

Each client may write only what its job needs, checked in code before any request: anything
but GET must match its `writes` list, and a merge (`/pulls/{n}/merge`, `/merges`) is refused
whatever the token could do. The token's own scope should be as narrow (below).

`GitHubReviewClient` (AISRE_REVIEW_ADAPTER=github, allowed in production: Release A drafts pull
requests, ADR-44, M2). It reads the repository the way the fake review system does (default
branch, recent commits with each file's SHA-256 before and after, file content) and opens DRAFT
pull requests only: a branch `aisre/<action_id>` with one commit, built from the patch through
the Git database API against the patch's base revision, after checking every file's hash there
and after applying it, then a pull request with `draft: true`. The same action finds its own
pull request or branch again, so a retried activity opens nothing twice. It can't merge, push to
an existing branch, or write a file through the contents API. Token: a fine-grained token (or a
GitHub App's) with Contents read and write (for the branch) and Pull requests read and write,
on the pilot's repositories only, in AISRE_REVIEW_TOKEN; branch protection on the default branch
stays the barrier to merging. Repositories are named per service in AISRE_REVIEW_REPOS (FR-48:
the service's own repository). Limits: each file's mode is written as 100644 (the edit policy
allows configuration and source files, none executable); a commit's `refs` are not read (GitHub
has no cheap "refs at a commit"); history is GitHub's commit list, not first-parent only.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote, urlencode

from aisre.remediation.patch import FileChangeFacts
from aisre.remediation.review import (
    CommitInfo,
    DraftPullRequest,
    PullRequest,
    ReviewUnavailable,
)
from pydantic import SecretStr
from temporalio.exceptions import ApplicationError

API_VERSION = "2022-11-28"
TIMEOUT_S = 10.0
MAX_CONTENT_BYTES = (
    1_000_000  # a file larger than this isn't read (the proposer offers 8 000 chars)
)
MAX_FILES_PER_COMMIT = 20  # hashed per commit in the history; past that, its files go unlisted
JSON = "application/vnd.github+json"
_MERGE = re.compile(r"/pulls/\d+/merge$|/merges$")


class WriteRefused(RuntimeError):
    """A write this client is not allowed to make: a merge, or anything off its list."""


class GitHubApi:
    """One token, one base URL, and the writes allowed. No call is made when it is built."""

    def __init__(
        self,
        url: str,
        token: SecretStr,
        writes: Sequence[str],
        what: str,
        timeout_s: float = TIMEOUT_S,
    ) -> None:
        if not url.startswith("https://"):
            raise ValueError(f"{what}: the GitHub API URL must be https")
        self.url, self.token, self.what, self.timeout_s = url.rstrip("/"), token, what, timeout_s
        self.writes = [re.compile(w) for w in writes]

    def call(self, method: str, path: str, body: Any = None, accept: str = JSON) -> tuple[int, Any]:
        """(status, parsed JSON, or bytes for a raw or sha media type). 5xx and no answer raise
        ConnectionError; 4xx come back to the caller."""
        bare = path.split("?", 1)[0]
        if _MERGE.search(bare):
            raise WriteRefused(f"{self.what} never merges (ADR-44, FR-18)")
        if method != "GET" and not any(w.fullmatch(f"{method} {bare}") for w in self.writes):
            raise WriteRefused(f"{self.what} may not {method} {bare}")
        req = urllib.request.Request(
            self.url + path,
            data=None if body is None else json.dumps(body).encode(),
            method=method,
            headers={
                "Authorization": f"Bearer {self.token.get_secret_value()}",
                "Accept": accept,
                "X-GitHub-Api-Version": API_VERSION,
                "Content-Type": "application/json",
                "User-Agent": "aisre",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                status, raw = int(resp.status), resp.read(MAX_CONTENT_BYTES + 1)
        except urllib.error.HTTPError as e:
            if e.code >= 500:
                raise ConnectionError(f"{self.what}: HTTP {e.code}") from None
            status, raw = e.code, e.read(MAX_CONTENT_BYTES + 1)
        except (urllib.error.URLError, OSError) as e:
            raise ConnectionError(f"{self.what}: {type(e).__name__}") from None
        if len(raw) > MAX_CONTENT_BYTES:
            raise ConnectionError(f"{self.what}: answer over {MAX_CONTENT_BYTES} bytes")
        if accept != JSON and status == 200:
            return status, raw
        try:
            return status, json.loads(raw) if raw else None
        except ValueError:
            return status, None


# ---------------------------------------------------------------------------
# The review system
# ---------------------------------------------------------------------------
_REPO = r"/repos/[\w.-]+/[\w.-]+"


def apply_hunks(text: str, hunks: Sequence[Mapping[str, Any]]) -> str:
    """The Test Runner harness's rule (ops/sandbox/harness/run), as the fake review system
    applies it: context and removed lines must match exactly."""
    old = text.splitlines()
    out: list[str] = []
    at = 0
    for h in hunks:
        start = h["old_start"] - 1 if h["old_lines"] else h["old_start"]
        if start < at:
            raise ValueError("hunks overlap")
        out += old[at:start]
        at = start
        for line in h["lines"]:
            tag, body = line[:1], line[1:]
            if tag in (" ", "-"):
                if at >= len(old) or old[at] != body:
                    raise ValueError(f"hunk at line {h['old_start']} does not match")
                at += 1
                if tag == " ":
                    out.append(body)
            elif tag == "+":
                out.append(body)
            else:
                raise ValueError("malformed hunk line")
    out += old[at:]
    return "\n".join(out) + ("\n" if text.endswith("\n") or not text else "")


def _refused(message: str) -> ApplicationError:
    return ApplicationError(message, type="PullRequestRefused", non_retryable=True)


class GitHubReviewClient:
    """review.ReviewSystem on GitHub: reads, and draft pull requests only."""

    WRITES = (
        f"POST {_REPO}/git/trees",
        f"POST {_REPO}/git/commits",
        f"POST {_REPO}/git/refs",
        f"POST {_REPO}/pulls",
        f"POST {_REPO}/issues/\\d+/labels",
    )

    def __init__(
        self, url: str, token: SecretStr, repos: Mapping[str, str], timeout_s: float = TIMEOUT_S
    ) -> None:
        bad = [r for r in repos.values() if not re.fullmatch(r"[\w.-]+/[\w.-]+", r)]
        if bad:
            raise ValueError("AISRE_REVIEW_REPOS: each service names owner/repository")
        self.api = GitHubApi(url, token, self.WRITES, "GitHub review", timeout_s)
        self.repos = dict(repos)

    def _full(self, repo: str) -> str:
        if repo not in self.repos:
            raise ReviewUnavailable(f"no GitHub repository is configured for {repo}")
        return f"/repos/{self.repos[repo]}"

    def _get(self, path: str, accept: str = JSON) -> Any:
        try:
            status, body = self.api.call("GET", path, accept=accept)
        except ConnectionError as e:
            raise ReviewUnavailable(str(e)) from None
        if status in (404, 409, 422):  # missing; empty repository; not a commit
            return None
        if status != 200:
            raise ReviewUnavailable(f"GitHub review: HTTP {status} on GET")
        return body

    def _raw(self, full: str, ref: str, path: str) -> bytes | None:
        found = self._get(
            f"{full}/contents/{quote(path)}?{urlencode({'ref': ref})}",
            "application/vnd.github.raw+json",
        )
        return found if isinstance(found, bytes) else None

    def default_branch(self, repo: str) -> tuple[str, str]:
        found = self._get(self._full(repo))
        branch = (found or {}).get("default_branch")
        head = self.resolve(repo, str(branch)) if branch else None
        if not branch or head is None:
            raise ReviewUnavailable(f"no repository {repo}")
        return str(branch), head

    def resolve(self, repo: str, ref: str) -> str | None:
        found = self._get(f"{self._full(repo)}/commits/{quote(ref, safe='')}")
        return str(found["sha"]) if isinstance(found, dict) and found.get("sha") else None

    def history(self, repo: str, ref: str, limit: int) -> list[CommitInfo]:
        full = self._full(repo)
        listed = self._get(f"{full}/commits?{urlencode({'sha': ref, 'per_page': limit})}") or []
        return [self._commit(full, str(c["sha"])) for c in listed[:limit]]

    def _commit(self, full: str, sha: str) -> CommitInfo:
        c = self._get(f"{full}/commits/{sha}")
        if not isinstance(c, dict):
            raise ReviewUnavailable(f"commit {sha[:12]} unreadable")
        parents = [str(p["sha"]) for p in c.get("parents") or []]
        files: dict[str, FileChangeFacts] = {}
        for f in (c.get("files") or [])[:MAX_FILES_PER_COMMIT]:
            path, status = str(f["filename"]), f.get("status")
            after = None if status == "removed" else self._raw(full, sha, path)
            before = None if status == "added" or not parents else self._raw(full, parents[0], path)
            files[path] = FileChangeFacts(
                hashlib.sha256(before).hexdigest() if before is not None else None,
                hashlib.sha256(after).hexdigest() if after is not None else None,
            )
        meta = c.get("commit") or {}
        return CommitInfo(
            sha=str(c["sha"]),
            parents=parents,
            message=str(meta.get("message") or "").split("\n", 1)[0],
            refs=[],
            files=files,
            author=str((meta.get("author") or {}).get("name") or ""),
        )

    def content(self, repo: str, ref: str, path: str) -> str | None:
        raw = self._raw(self._full(repo), ref, path)
        try:
            return raw.decode() if raw is not None else None
        except UnicodeDecodeError:
            return None

    def pull(self, repo: str, number: int) -> PullRequest | None:
        found = self._get(f"{self._full(repo)}/pulls/{number}")
        return _gh_pull(found) if isinstance(found, dict) else None

    def merge(self, repo: str, number: int) -> None:
        raise WriteRefused("the platform never merges (ADR-44, FR-18)")

    def _write(self, path: str, body: Any) -> tuple[int, Any]:
        try:
            return self.api.call("POST", path, body)
        except ConnectionError as e:
            raise ReviewUnavailable(str(e)) from None

    def _existing(self, full: str, head: str) -> PullRequest | None:
        owner = full.split("/")[2]
        found = self._get(f"{full}/pulls?{urlencode({'head': f'{owner}:{head}', 'state': 'all'})}")
        return _gh_pull(found[0]) if found else None

    def open_draft(self, repo: str, pr: DraftPullRequest, key: str) -> PullRequest:
        """A draft pull request for the patch, or the one this action opened already. `key` is
        the activity's; the branch, named after the action, is what makes a retry find its own."""
        full = self._full(repo)
        if (earlier := self._existing(full, pr.head)) is not None:
            return earlier
        entries = []
        for f in pr.patch.get("files") or []:
            raw = self._raw(full, pr.base_revision, str(f["path"]))
            if raw is None or hashlib.sha256(raw).hexdigest() != f["old_sha256"]:
                raise _refused(f"{f['path']} at {pr.base_revision[:12]} is not the patch's base")
            try:
                new = apply_hunks(raw.decode(), f["hunks"])
            except (ValueError, KeyError, UnicodeDecodeError) as e:
                raise _refused(f"the patch does not apply: {e}") from None
            if hashlib.sha256(new.encode()).hexdigest() != f["new_sha256"]:
                raise _refused(f"{f['path']} after the patch is not the patch's result")
            entries.append({"path": f["path"], "mode": "100644", "type": "blob", "content": new})
        if not entries:
            raise _refused("a pull request needs a patch that changes files")
        base = self._get(f"{full}/git/commits/{pr.base_revision}")
        if not isinstance(base, dict):
            raise _refused("base_revision is not a commit here")
        status, tree = self._write(
            f"{full}/git/trees", {"base_tree": base["tree"]["sha"], "tree": entries}
        )
        if status != 201:
            raise ReviewUnavailable(f"GitHub review: HTTP {status} writing the tree")
        status, commit = self._write(
            f"{full}/git/commits",
            {"message": pr.title, "tree": tree["sha"], "parents": [pr.base_revision]},
        )
        if status != 201:
            raise ReviewUnavailable(f"GitHub review: HTTP {status} writing the commit")
        status, _ = self._write(
            f"{full}/git/refs", {"ref": f"refs/heads/{pr.head}", "sha": commit["sha"]}
        )
        if status == 422:  # the branch exists: ours from an earlier try, or refused
            ref = self._get(f"{full}/git/ref/heads/{quote(pr.head)}")
            sha = ((ref or {}).get("object") or {}).get("sha")
            there = self._get(f"{full}/git/commits/{sha}") if sha else None
            ours = (
                isinstance(there, dict)
                and there.get("tree", {}).get("sha") == tree["sha"]
                and [p.get("sha") for p in there.get("parents", [])] == [pr.base_revision]
            )
            if not ours:
                raise _refused(f"branch {pr.head} exists and holds something else")
        elif status != 201:
            raise ReviewUnavailable(f"GitHub review: HTTP {status} creating the branch")
        status, out = self._write(
            f"{full}/pulls",
            {
                "title": pr.title,
                "body": pr.body,
                "head": pr.head,
                "base": pr.base,
                "draft": True,  # always: the platform never opens a pull request for merging
                "maintainer_can_modify": False,
            },
        )
        if status == 422 and (earlier := self._existing(full, pr.head)) is not None:
            return earlier
        if status in (403, 409, 422):
            message = (out or {}).get("message") if isinstance(out, dict) else None
            raise _refused(f"GitHub refused the pull request: {message}")
        if status != 201 or not isinstance(out, dict):
            raise ReviewUnavailable(f"GitHub review: HTTP {status} opening a pull request")
        opened = _gh_pull(out)
        if pr.labels:  # labels help reviewers find it; a refusal leaves the pull request as is
            self._write(f"{full}/issues/{opened.number}/labels", {"labels": pr.labels})
        return opened


def _gh_pull(p: Mapping[str, Any]) -> PullRequest:
    return PullRequest(
        number=int(p["number"]),
        url=str(p["html_url"]),
        draft=bool(p.get("draft")),
        state=str(p["state"]),
        merged=bool(p.get("merged") or p.get("merged_at")),
        required_reviewers=[str(u["login"]) for u in p.get("requested_reviewers") or []]
        + [f"team:{t['slug']}" for t in p.get("requested_teams") or []],
    )
