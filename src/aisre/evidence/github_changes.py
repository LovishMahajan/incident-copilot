"""Artifact 9.4, 13.5 (M5, ADR-75): change history from GitHub, read-only, for `get_changes`.

For services whose monitoring holds no change history (the Prometheus stack, CloudWatch) and
whose releases run from GitHub Actions. GitHub REST API, version 2022-11-28
(`X-GitHub-Api-Version`), on github.com (`https://api.github.com`) or GitHub Enterprise Server
(`https://HOST/api/v3`):

- deployments to the configured environment(s), newest first, paged:
  `GET /repos/{owner}/{repo}/deployments?environment=` and each one's statuses,
  `GET /repos/{owner}/{repo}/deployments/{id}/statuses`
  (https://docs.github.com/en/rest/deployments/deployments,
  https://docs.github.com/en/rest/deployments/statuses);
- what each deployed against the deployment before it in the same environment:
  `GET /repos/{owner}/{repo}/compare/{base}...{head}` (https://docs.github.com/en/rest/commits/commits#compare-two-commits),
  its commits and changed files; files matching `config_paths` (globs, `deploy/**`) are
  configuration, their patches are the record's `diff` (at most MAX_DIFF_CHARS), and `KEY=value`
  or `key: value` lines changed in them are its `config_changes`, so a change like
  ORDER_TAX_RATE_BPS 825 -> 8.25 reaches the fact rules. A compare whose head is behind its base
  is a redeploy of older code: a `rollback` stage;
- with `workflows` configured, completed runs of those workflow files on the default branch
  (`GET /repos/{owner}/{repo}` for it), `GET /repos/{owner}/{repo}/actions/workflows/{file}/runs`
  (https://docs.github.com/en/rest/actions/workflow-runs#list-workflow-runs-for-a-workflow),
  one record each, unless a deployment above already covers its commit. A run holds no diff.

Records are the platform's change shape (aisre.evidence.sources): `version` is the
deployment's `payload.version` if it sets one, else its `ref`; stages are `deployment_created`
and each status up to the window's end but `inactive` (GitHub sets that when a later deployment
replaces it), so the change's time is its last status in the window. GitHub holds no traffic shares (`rollout` is
empty) and no configuration revision.

Configuration, per service: `repository` (`owner/name`, may use `$service`), `url` (the API
base, default github.com), `environments` (`{platform environment: [GitHub environment names]}`;
default the call's environment by the same name; an environment not in the map is a gap, never
another's deployments: FR-44), `workflows` (file names), `config_paths` (globs).

Token, by reference only (SecretRef), read when a call needs it: a fine-grained token (or a
GitHub App's) on the service's repositories with **read-only** Contents, Deployments, Actions
and Metadata, nothing else; a classic token would need `repo`, which writes, so don't. Name it
anything but `AISRE_RELEASE_*` (that prefix is the release write credential and refused here and
by `aisre pilot check`, ADR-44); it may be the review token's secret only if the operator
chooses to point both at the same one.

Bounds (Artifact 9.4): the call's window; at most `max_pages` pages of 100 deployments per
environment and `max_deployments` deployments a call (newest), each one statuses and one
compare request; 50 commits and 300 files a compare (GitHub's own caps lower than ours mark
`partial`); 4 MB a response; each request at most `timeout_s` and the whole call `budget_s`,
inside the collector's per-call time. A rate limit (403 or 429 with `retry-after` or
`x-ratelimit-remaining: 0`) is a gap naming when GitHub says to retry, never a sleep. 401, 403
and 404 are gaps with their reason. No deployments in the window is an empty answer. Retries,
the breaker, redaction of secrets in diffs and messages (telemetry.redact_text, applied by the
collector to every string) and the output cap stay the collector's. Tested against recorded
responses shaped after the documented API only (tests/unit/test_github_changes.py).
"""

from __future__ import annotations

import fnmatch
import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from pydantic import BaseModel

from aisre.evidence.sources import (
    MAX_RESPONSE_BYTES,
    Record,
    SecretRef,
    SourceResult,
    SourceUnavailable,
    parse_time,
    served,
    substitute,
    utc_text,
)

API_VERSION = "2022-11-28"
PER_PAGE = 100
MAX_COMMITS = 50
MAX_FILES = 300  # GitHub's own cap on a compare's files
MAX_DIFF_CHARS = 16_000
_REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_WORKFLOW = re.compile(r"^[A-Za-z0-9_.-]{1,100}\.ya?ml$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_SETTING = re.compile(r"^([-+])\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_.-]*)\s*[:=]\s*(.*?)\s*$")


class GitHubChangesSource:
    """`get_changes` from one repository's deployments, compares and workflow runs."""

    def __init__(
        self,
        repository: str,
        token: Any,
        url: str = "https://api.github.com",
        environments: Mapping[str, Any] | None = None,
        workflows: Sequence[str] = (),
        config_paths: Sequence[str] = (),
        max_deployments: int = 5,
        max_pages: int = 3,
        timeout_s: float = 10.0,
        budget_s: float = 18.0,
        max_bytes: int = MAX_RESPONSE_BYTES,
        name: str = "github-changes",
    ) -> None:
        self.name = name
        if not url.startswith("https://"):
            raise ValueError(f"{name}: url must be https (api.github.com or HOST/api/v3)")
        if not isinstance(repository, str) or not _REPO.match(repository.replace("$service", "x")):
            raise ValueError(f"{name}: repository is owner/name")
        self.token = SecretRef(token, f"{name} token")
        if self.token.kind == "env" and self.token.where.startswith("AISRE_RELEASE_"):
            raise ValueError(
                f"{name}: AISRE_RELEASE_* is the release write credential (ADR-44); "
                "give a read-only token its own name"
            )
        self.credentials = (self.token,)
        self.url, self.repository = url.rstrip("/"), repository
        self.web = (
            "https://github.com"
            if self.url == "https://api.github.com"
            else self.url.removesuffix("/api/v3")
        )
        self.environments: dict[str, list[str]] | None = None
        if environments is not None:
            if not isinstance(environments, Mapping) or not environments:
                raise ValueError(f"{name}: environments is {{environment: [GitHub names]}}")
            self.environments = {}
            for env, names in environments.items():
                names = [names] if isinstance(names, str) else names
                if not isinstance(names, list) or not names:
                    raise ValueError(f"{name}: environments is {{environment: [GitHub names]}}")
                if not all(isinstance(n, str) and n.strip() for n in names):
                    raise ValueError(f"{name}: environments is {{environment: [GitHub names]}}")
                self.environments[env] = list(names)
        if isinstance(workflows, str) or not all(
            isinstance(w, str) and _WORKFLOW.match(w) for w in workflows
        ):
            raise ValueError(f"{name}: workflows are file names, like release.yml")
        if isinstance(config_paths, str) or not all(isinstance(g, str) and g for g in config_paths):
            raise ValueError(f"{name}: config_paths are globs, like deploy/**")
        self.workflows, self.config_paths = list(workflows), list(config_paths)
        limits = (max_deployments, max_pages, timeout_s, budget_s, max_bytes)
        if not all(isinstance(v, int | float) and v > 0 for v in limits):
            raise ValueError(f"{name}: limits are positive numbers")
        self.max_deployments, self.max_pages = int(max_deployments), int(max_pages)
        self.timeout_s, self.budget_s, self.max_bytes = timeout_s, budget_s, max_bytes

    def serves(self) -> frozenset[str]:
        """The tools this configuration answers (aisre pilot check reads it)."""
        return served(get_changes=True)

    # -- HTTP -----------------------------------------------------------------------------------
    def _get(self, path: str, query: Mapping[str, str], deadline: float) -> Any:
        left = deadline - time.monotonic()
        if left <= 0:
            raise SourceUnavailable(f"{self.name}: out of time ({self.budget_s:g} s a call)")
        req = urllib.request.Request(
            self.url + path + (f"?{urlencode(query)}" if query else ""),
            headers={
                "Authorization": f"Bearer {self.token.value()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "aisre",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=min(self.timeout_s, left)) as resp:
                raw = resp.read(self.max_bytes + 1)
        except urllib.error.HTTPError as e:
            raise SourceUnavailable(self._refusal(e.code, e.headers)) from None
        except TimeoutError:
            raise SourceUnavailable(f"{self.name}: no answer in time") from None
        except (urllib.error.URLError, OSError) as e:
            raise SourceUnavailable(f"{self.name}: {type(e).__name__}") from None
        if len(raw) > self.max_bytes:
            raise SourceUnavailable(f"{self.name}: answer over {self.max_bytes} bytes")
        try:
            return json.loads(raw)
        except ValueError:
            raise SourceUnavailable(f"{self.name}: unreadable answer") from None

    def _refusal(self, code: int, headers: Any) -> str:
        """The gap's reason. Never the URL or a header's value but GitHub's retry time."""

        def header(key: str) -> str:
            return str(headers.get(key) or "") if headers is not None else ""

        if code in (403, 429):
            if header("retry-after").isdigit():
                return f"{self.name}: rate limited (HTTP {code}); retry after {header('retry-after')} s"
            if header("x-ratelimit-remaining") == "0" and header("x-ratelimit-reset").isdigit():
                reset = datetime.fromtimestamp(int(header("x-ratelimit-reset")), UTC)
                return f"{self.name}: rate limit used up (HTTP {code}); resets {utc_text(reset)}"
        reasons = {
            401: "the token was refused",
            403: "the token may not read this (needs Contents, Deployments, Actions, Metadata read)",
            404: "repository, workflow or commit not found, or not visible to the token",
            429: "rate limited",
        }
        return f"{self.name}: HTTP {code}" + (f", {reasons[code]}" if code in reasons else "")

    # -- get_changes ----------------------------------------------------------------------------
    def fetch(self, tool: str, params: BaseModel, environment: str) -> SourceResult:
        if tool != "get_changes":
            raise SourceUnavailable(f"{self.name} has no {tool}")
        p: Any = params
        repo = substitute(self.repository, str, service=p.service)
        if not _REPO.match(repo):
            raise SourceUnavailable(f"{self.name}: no repository for service {p.service!r}")
        names = (
            self.environments.get(environment) if self.environments is not None else [environment]
        )
        if not names:
            raise SourceUnavailable(f"{self.name}: no GitHub environment for {environment}")
        deadline = time.monotonic() + self.budget_s
        start, end = p.start, p.end
        records: list[Record] = []
        partial = False
        for env_name in names:
            got, more = self._deployments(repo, env_name, start, end, deadline)
            records += got
            partial = partial or more
        deployed = {str(r.pop("_sha")) for r in records}
        records.sort(key=lambda r: str(r["stages"][0]["at"]))
        if len(records) > self.max_deployments:
            records, partial = records[-self.max_deployments :], True
        if self.workflows:
            runs, more = self._runs(repo, start, end, deadline, deployed)
            records = sorted(records + runs, key=lambda r: str(r["stages"][0]["at"]))
            partial = partial or more
        link = f"{self.web}/{repo}/deployments"
        return SourceResult(
            f"{self.name}:changes",
            link,
            records[-p.limit :],
            partial=partial or len(records) > p.limit,
        )

    def _deployments(
        self, repo: str, env_name: str, start: datetime, end: datetime, deadline: float
    ) -> tuple[list[Record], bool]:
        """The deployments to `env_name` created in the window, each with the one before it
        (its `change_id` names the deployment; its first stage is its creation)."""
        listed: list[dict[str, Any]] = []
        more = False
        for page in range(1, self.max_pages + 1):
            got = self._get(
                f"/repos/{repo}/deployments",
                {"environment": env_name, "per_page": str(PER_PAGE), "page": str(page)},
                deadline,
            )
            if not isinstance(got, list):
                raise SourceUnavailable(f"{self.name}: not a deployment list")
            listed += [d for d in got if d.get("environment") == env_name]
            if len(got) < PER_PAGE or any(_at(d) < start for d in got):
                break  # the list is newest first: one before the window gives the base
            more = page == self.max_pages
        listed = sorted({d.get("id"): d for d in listed}.values(), key=_at, reverse=True)
        in_window = [i for i, dep in enumerate(listed) if start <= _at(dep) <= end]
        out = [
            self._deployment(
                repo, listed[i], listed[i + 1] if i + 1 < len(listed) else None, end, deadline
            )
            for i in in_window[: self.max_deployments]  # the newest: each costs two requests
        ]
        return out, more or len(in_window) > self.max_deployments

    def _deployment(
        self,
        repo: str,
        dep: Mapping[str, Any],
        previous: Mapping[str, Any] | None,
        end: datetime,
        deadline: float,
    ) -> Record:
        statuses = self._get(
            f"/repos/{repo}/deployments/{int(dep['id'])}/statuses",
            {"per_page": str(PER_PAGE)},
            deadline,
        )
        if not isinstance(statuses, list):
            raise SourceUnavailable(f"{self.name}: not a status list")
        stages = [{"step": "deployment_created", "at": dep.get("created_at")}] + [
            {"step": f"deployment_{s.get('state')}", "at": s.get("created_at")}
            for s in sorted(statuses, key=lambda s: str(s.get("created_at")))
            if s.get("state") != "inactive"
            and isinstance(s.get("created_at"), str)
            and parse_time(s["created_at"]) <= end
        ]
        head, base = str(dep.get("sha") or ""), str((previous or {}).get("sha") or "")
        record: Record = {
            "change_id": f"deployment {dep.get('id')}",
            "version": _version(dep),
            "previous_version": _version(previous) if previous else None,
            "code_changed": None,
            "files": [],
            "commits": [{"sha": head, "message": None}],
            "config_changes": [],
            "config_revision": None,
            "stages": stages,
            "rollout": [],
            "diff": None,
            "_sha": head,  # taken off in fetch: a run of this commit is this deployment
        }
        if not (_SHA.match(head) and _SHA.match(base)) or head == base:
            return record
        cmp = self._get(f"/repos/{repo}/compare/{base}...{head}", {}, deadline)
        if not isinstance(cmp, dict):
            raise SourceUnavailable(f"{self.name}: not a comparison")
        if cmp.get("status") == "behind":
            record["stages"].append({"step": "rollback", "at": dep.get("created_at")})
        commits = [
            {"sha": c.get("sha"), "message": str((c.get("commit") or {}).get("message") or "")}
            for c in (cmp.get("commits") or [])[-MAX_COMMITS:]
        ]
        files = [f for f in (cmp.get("files") or [])[:MAX_FILES] if f.get("filename")]
        config = [f for f in files if self._is_config(f["filename"])]
        record.update(
            files=sorted({f["filename"] for f in files}),
            commits=commits or record["commits"],
            code_changed=any(not self._is_config(f["filename"]) for f in files),
            config_changes=_settings_changed(config),
            diff=_diff(config),
        )
        return record

    def _is_config(self, path: str) -> bool:
        return any(fnmatch.fnmatchcase(path, g) for g in self.config_paths)

    def _runs(
        self, repo: str, start: datetime, end: datetime, deadline: float, deployed: set[str]
    ) -> tuple[list[Record], bool]:
        about = self._get(f"/repos/{repo}", {}, deadline)
        branch = str(about.get("default_branch") or "") if isinstance(about, dict) else ""
        if not branch:
            raise SourceUnavailable(f"{self.name}: no default branch")
        out: list[Record] = []
        more = False
        for workflow in self.workflows:
            got = self._get(
                f"/repos/{repo}/actions/workflows/{workflow}/runs",
                {
                    "branch": branch,
                    "status": "completed",
                    "created": f"{utc_text(start)}..{utc_text(end)}",
                    "per_page": str(PER_PAGE),
                },
                deadline,
            )
            if not isinstance(got, dict):
                raise SourceUnavailable(f"{self.name}: not a run list")
            runs = got.get("workflow_runs") or []
            more = more or int(got.get("total_count") or 0) > len(runs)
            for run in runs:
                started = run.get("run_started_at") or run.get("created_at")
                if not started or not start <= parse_time(started) <= end:
                    continue
                if run.get("head_sha") in deployed or run.get("head_branch") != branch:
                    continue
                message = str((run.get("head_commit") or {}).get("message") or "")
                out.append(
                    {
                        "change_id": f"workflow run {run.get('id')} ({workflow})",
                        "version": str(run.get("head_sha") or "")[:7] or None,
                        "previous_version": None,
                        "code_changed": None,
                        "files": [],
                        "commits": [{"sha": run.get("head_sha"), "message": message}],
                        "config_changes": [],
                        "config_revision": None,
                        "stages": [
                            {"step": "workflow_started", "at": started},
                            {
                                "step": f"workflow_{run.get('conclusion')}",
                                "at": run.get("updated_at"),
                            },
                        ],
                        "rollout": [],
                        "diff": None,
                    }
                )
        return out, more


def _at(dep: Mapping[str, Any]) -> datetime:
    stamp = dep.get("created_at")
    return parse_time(stamp) if isinstance(stamp, str) else datetime.min.replace(tzinfo=UTC)


def _version(dep: Mapping[str, Any]) -> str | None:
    payload = dep.get("payload")
    if isinstance(payload, Mapping) and isinstance(payload.get("version"), str):
        return str(payload["version"])
    return str(dep.get("ref")) if dep.get("ref") else None


def _settings_changed(files: Sequence[Mapping[str, Any]]) -> list[Record]:
    """`KEY=value` and `key: value` lines a patch removes and adds, as {key, old, new}."""
    old: dict[str, str] = {}
    new: dict[str, str] = {}
    order: list[str] = []
    for f in files:
        for line in str(f.get("patch") or "").splitlines():
            if line.startswith(("---", "+++")):
                continue
            m = _SETTING.match(line)
            if not m:
                continue
            sign, key, value = m.groups()
            (old if sign == "-" else new)[key] = value
            if key not in order:
                order.append(key)
    return [
        {"key": k, "old": old.get(k), "new": new.get(k)} for k in order if old.get(k) != new.get(k)
    ]


def _diff(files: Sequence[Mapping[str, Any]]) -> str | None:
    """The configuration files' patches as one unified diff, at most MAX_DIFF_CHARS."""
    if not files:
        return None
    parts = []
    for f in files:
        name = f["filename"]
        patch = f.get("patch")
        body = patch if patch else "[no patch: GitHub omits it for large or binary files]"
        parts.append(f"diff --git a/{name} b/{name}\n--- a/{name}\n+++ b/{name}\n{body}\n")
    text = "".join(parts)
    return text if len(text) <= MAX_DIFF_CHARS else text[:MAX_DIFF_CHARS] + "\n[diff truncated]\n"
