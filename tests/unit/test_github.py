"""Artifact 11.2, 11.7, 11.9, 13.5 (M5, ADR-75): GitHub as review system and release process,
against recorded responses (tests/fixtures/recorded/github.json, REST API 2022-11-28).

Review: reads as the fake review system does, opens draft pull requests only, finds its own
again, refuses a patch that doesn't apply, and can't merge. Release: lookup by key from
deployments and runs, never a second dispatch for a key it can see (ST-38: GitHub has no
idempotency key), refused without a write credential and in production. Nothing here shows
either works against a real GitHub account.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import urllib.request

import pytest
from pydantic import SecretStr, ValidationError
from recorded_http import Recorded, load
from temporalio.exceptions import ApplicationError

from aisre.config import Settings
from aisre.remediation import github
from aisre.remediation.github import GitHubActionsRelease, GitHubReviewClient, WriteRefused
from aisre.remediation.release import ReleaseRefused, release_from_settings
from aisre.remediation.review import DraftPullRequest, ReviewUnavailable, client_from_settings

HEAD = "9f1c2ab3d4e5f60718293a4b5c6d7e8f90a1b2c3"
C1 = "1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d"
TOKEN = SecretStr("github_pat_not_a_real_token")
API = "https://api.github.com"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def recorded(monkeypatch) -> Recorded:
    r = Recorded(load("github"))
    monkeypatch.setattr(urllib.request, "urlopen", r)
    return r


def no_pull_request_yet(recorded: Recorded) -> None:
    """The action has no pull request, however often it asks."""
    for e in recorded.exchanges:
        if e["method"] == "GET" and e["path"].endswith("/pulls"):
            e.update(body=[], once=False)


@pytest.fixture
def review() -> GitHubReviewClient:
    return GitHubReviewClient(API, TOKEN, {"orders-api": "example-org/orders-api"})


# ---------------------------------------------------------------------------
# Review: reads
# ---------------------------------------------------------------------------
def test_reads_the_repository_as_the_fake_review_system_does(recorded, review):
    assert review.default_branch("orders-api") == ("main", HEAD)
    history = review.history("orders-api", HEAD, 5)
    assert [c.sha for c in history] == [HEAD, C1]
    head = history[0]
    assert head.parents == [C1] and head.message == "Raise the pool size"
    assert head.author == "Dana Dev" and head.refs == []
    facts = head.files["config/app.yaml"]
    assert facts.before_sha256 == sha("pool: 10\nretries: 3\n")
    assert facts.after_sha256 == sha("pool: 50\nretries: 3\n")
    assert head.files["docs/new.md"].before_sha256 is None  # added: nothing before
    assert history[1].files["config/app.yaml"].before_sha256 is None  # a root commit
    assert review.content("orders-api", HEAD, "config/app.yaml") == "pool: 50\nretries: 3\n"
    assert review.content("orders-api", HEAD, "missing.txt") is None
    assert review.resolve("orders-api", "no-such-ref") is None
    sent = recorded.sent[0]
    assert sent.headers["authorization"] == "Bearer github_pat_not_a_real_token"
    assert sent.headers["x-github-api-version"] == "2022-11-28"
    assert all(s.method == "GET" for s in recorded.sent)


def test_a_service_with_no_configured_repository_is_unreadable(recorded, review):
    with pytest.raises(ReviewUnavailable, match="no GitHub repository"):
        review.default_branch("payments-api")
    assert recorded.sent == []


def test_a_server_error_is_unavailable_not_empty(monkeypatch, review):
    r = Recorded(
        [{"method": "GET", "path": "/repos/example-org/orders-api", "status": 502, "body": {}}]
    )
    monkeypatch.setattr(urllib.request, "urlopen", r)
    with pytest.raises(ReviewUnavailable, match="HTTP 502"):
        review.default_branch("orders-api")


# ---------------------------------------------------------------------------
# Review: draft pull requests only
# ---------------------------------------------------------------------------
def draft(old="pool: 50", new="pool: 10", old_sha=None) -> DraftPullRequest:
    before, after = f"{old}\nretries: 3\n", f"{new}\nretries: 3\n"
    patch = {
        "files": [
            {
                "path": "config/app.yaml",
                "op": "modify",
                "old_sha256": old_sha or sha(before),
                "new_sha256": sha(after),
                "hunks": [
                    {
                        "old_start": 1,
                        "old_lines": 2,
                        "new_start": 1,
                        "new_lines": 2,
                        "lines": [f"-{old}", f"+{new}", " retries: 3"],
                    }
                ],
            }
        ]
    }
    return DraftPullRequest(
        title="[aisre] config revert: orders-api: pool back to 10",
        body="Drafted by the incident platform.",
        base="main",
        base_revision=HEAD,
        head="aisre/act-1",
        patch=patch,
        labels=["aisre", "draft-recommendation"],
    )


def test_opens_a_draft_pull_request_built_from_the_patch(recorded, review):
    pr = review.open_draft("orders-api", draft(), "key-1")
    assert (pr.number, pr.draft, pr.merged, pr.state) == (42, True, False, "open")
    assert pr.url == "https://github.com/example-org/orders-api/pull/42"
    assert pr.required_reviewers == ["security-lead", "team:orders-owners"]
    tree = recorded.to("POST", "/git/trees")[0].body
    assert tree["base_tree"] == "7777777777777777777777777777777777777777"
    assert tree["tree"] == [
        {
            "path": "config/app.yaml",
            "mode": "100644",
            "type": "blob",
            "content": "pool: 10\nretries: 3\n",
        }
    ]
    assert recorded.to("POST", "/git/commits")[0].body["parents"] == [HEAD]
    assert recorded.to("POST", "/git/refs")[0].body["ref"] == "refs/heads/aisre/act-1"
    opened = recorded.to("POST", "/pulls")[0].body
    assert opened["draft"] is True and opened["head"] == "aisre/act-1" and opened["base"] == "main"
    assert recorded.to("POST", "/labels")[0].body == {"labels": ["aisre", "draft-recommendation"]}


def test_the_same_action_finds_its_own_pull_request_and_writes_nothing(recorded, review):
    first = review.open_draft("orders-api", draft(), "key-1")
    writes = len([s for s in recorded.sent if s.method != "GET"])
    again = review.open_draft("orders-api", draft(), "key-1")
    assert again == first
    assert len([s for s in recorded.sent if s.method != "GET"]) == writes


def test_a_retry_after_the_branch_was_made_reuses_it(recorded, review):
    recorded.exchanges.insert(
        0,
        {
            "method": "POST",
            "path": "/repos/example-org/orders-api/git/refs",
            "status": 422,
            "body": {"message": "Reference already exists"},
        },
    )
    recorded.exchanges.insert(
        0,
        {
            "method": "GET",
            "path": "/repos/example-org/orders-api/git/ref/heads/aisre/act-1",
            "status": 200,
            "body": {
                "ref": "refs/heads/aisre/act-1",
                "object": {"sha": "c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3"},
            },
        },
    )
    recorded.exchanges.insert(
        0,
        {
            "method": "GET",
            "path": "/repos/example-org/orders-api/git/commits/c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3",
            "status": 200,
            "body": {
                "sha": "c3",
                "tree": {"sha": "8888888888888888888888888888888888888888"},
                "parents": [{"sha": HEAD}],
            },
        },
    )
    assert review.open_draft("orders-api", draft(), "key-1").number == 42


def test_a_patch_that_is_not_the_files_base_is_refused_before_any_write(recorded, review):
    no_pull_request_yet(recorded)
    with pytest.raises(ApplicationError) as e:
        review.open_draft("orders-api", draft(old_sha="0" * 64), "key-1")
    assert e.value.non_retryable and e.value.type == "PullRequestRefused"
    base = sha("pool: 50\nretries: 3\n")
    with pytest.raises(ApplicationError, match="does not apply"):
        review.open_draft("orders-api", draft(old="pool: 99", old_sha=base), "key-1")
    assert [s for s in recorded.sent if s.method != "GET"] == []


def test_github_refusing_a_draft_is_a_non_retryable_refusal(recorded, review):
    no_pull_request_yet(recorded)
    recorded.exchanges.insert(
        0,
        {
            "method": "POST",
            "path": "/repos/example-org/orders-api/pulls",
            "status": 422,
            "body": {"message": "Draft pull requests are not supported in this repository."},
        },
    )
    with pytest.raises(ApplicationError, match="Draft pull requests are not supported"):
        review.open_draft("orders-api", draft(), "key-1")


def test_the_review_client_can_never_merge_or_write_elsewhere(recorded, review):
    with pytest.raises(WriteRefused, match="never merges"):
        review.merge("orders-api", 42)
    for method, path in (
        ("PUT", "/repos/example-org/orders-api/pulls/42/merge"),
        ("POST", "/repos/example-org/orders-api/merges"),
        ("PUT", "/repos/example-org/orders-api/contents/config/app.yaml"),
        ("PATCH", "/repos/example-org/orders-api/git/refs/heads/main"),
        ("DELETE", "/repos/example-org/orders-api/git/refs/heads/main"),
    ):
        with pytest.raises(WriteRefused):
            review.api.call(method, path, {})
    assert recorded.sent == []


def test_settings_allow_github_review_in_production_and_build_it_without_a_call(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("built with a network call")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    s = Settings(
        env="production",
        review_adapter="github",
        review_url="https://github.example.com/api/v3",
        review_token=TOKEN,
        review_repos=json.dumps({"orders-api": "example-org/orders-api"}),
    )
    assert isinstance(client_from_settings(s), GitHubReviewClient)
    with pytest.raises(ValidationError, match="https _URL and _REPOS"):
        Settings(review_adapter="github", review_url="https://api.github.com", review_token=TOKEN)
    with pytest.raises(ValueError, match="https"):
        GitHubReviewClient("http://api.github.com", TOKEN, {})


# ---------------------------------------------------------------------------
# Release: GitHub Actions and Deployments
# ---------------------------------------------------------------------------
PATCH = b'{"files":[]}'


@pytest.fixture
def release() -> GitHubActionsRelease:
    return GitHubActionsRelease(
        API,
        TOKEN,
        lambda digest: PATCH,
        repository="example-org/deploys",
        apply_workflow="aisre-apply.yml",
        undo_workflow="aisre-undo.yml",
    )


def run(coro):
    return asyncio.run(coro)


def test_current_state_is_the_newest_successful_deployment(recorded, release):
    state = run(release.current_state("orders-api-production"))
    assert state.revision == "r2r2r2r2" and state.in_progress_key == "act-2"


@pytest.mark.parametrize(
    "key, found",
    [
        ("act-2", "in_progress"),  # its deployment is running
        ("act-0", "applied"),  # inactive: superseded after it succeeded (payload as a string)
        ("act-9", "in_progress"),  # dispatched, its run queued, no deployment yet
        ("act-8", "failed"),  # its run failed before a deployment
        ("act-unknown", None),
    ],
)
def test_lookup_by_key_from_deployments_then_runs(recorded, release, key, found):
    assert run(release.lookup("orders-api-production", key)) == found
    assert run(release.lookup("orders-api-production", key)) == found  # safe to repeat
    assert all(s.method == "GET" for s in recorded.sent)


def test_a_run_that_succeeded_without_its_deployment_is_unknown_not_none(recorded, release):
    with pytest.raises(ConnectionError, match="a person must check"):
        run(release.lookup("orders-api-production", "act-7"))


def test_apply_dispatches_the_apply_workflow_with_the_key_and_the_patch(recorded, release):
    result = run(
        release.apply("orders-api-production", "act-new", "r2r2r2r2", "abc123", "r4r4r4r4")
    )
    assert result == "in_progress"
    (sent,) = recorded.to("POST", "/dispatches")
    assert sent.path.endswith("/aisre-apply.yml/dispatches")
    assert sent.body == {
        "ref": "main",
        "inputs": {
            "deployment": "orders-api-production",
            "execution_key": "act-new",
            "base_revision": "r2r2r2r2",
            "to_revision": "r4r4r4r4",
            "patch_sha256": "abc123",
            "patch": base64.b64encode(PATCH).decode(),
        },
    }
    assert "github_pat" not in json.dumps(sent.body)


def test_undo_dispatches_the_undo_workflow_without_a_patch(recorded, release):
    assert (
        run(release.apply("orders-api-production", "act-undo", "r2r2r2r2", None, "r1r1r1r1"))
        == "in_progress"
    )
    (sent,) = recorded.to("POST", "/dispatches")
    assert sent.path.endswith("/aisre-undo.yml/dispatches") and "patch" not in sent.body["inputs"]


@pytest.mark.parametrize(
    "key, answer",
    [
        ("act-2", "in_progress"),
        ("act-0", "applied"),
        ("act-9", "in_progress"),
        ("act-8", "in_progress"),
    ],
)
def test_a_key_already_seen_is_never_dispatched_again(recorded, release, key, answer):
    """ST-38's same-key resend: GitHub would run a second dispatch, so the adapter doesn't send
    one for a key it can see. A failed run is not "rejected": reconcile reads it."""
    assert run(release.apply("orders-api-production", key, "r2r2r2r2", "abc123", "r4")) == answer
    assert recorded.to("POST", "/dispatches") == []


def test_a_refused_dispatch_is_rejected_and_an_outage_is_unknown(recorded, release):
    assert (
        run(release.apply("orders-api-production", "act-refused", "r2", "abc", "r4")) == "rejected"
    )
    with pytest.raises(ConnectionError, match="HTTP 503"):
        run(release.apply("orders-api-production", "act-outage", "r2", "abc", "r4"))


def test_a_patch_too_large_for_the_inputs_is_rejected_unsent(recorded, monkeypatch, release):
    monkeypatch.setattr(github, "MAX_PATCH_INPUT", 4)
    assert run(release.apply("orders-api-production", "act-big", "r2", "abc", "r4")) == "rejected"
    assert recorded.to("POST", "/dispatches") == []


def test_history_names_each_deployment_and_its_key(recorded, release):
    changes = run(release.history("orders-api-production"))
    assert [(c.kind, c.revision, c.execution_key) for c in changes] == [
        ("aisre:apply", "r3r3r3r3", "act-2"),
        ("deploy", "r2r2r2r2", None),
        ("aisre:undo", "r1r1r1r1", "act-0"),
    ]


def test_the_release_client_writes_only_dispatches_and_never_merges(recorded, release):
    for method, path in (
        ("POST", "/repos/example-org/deploys/deployments"),
        ("POST", "/repos/example-org/other/actions/workflows/x.yml/dispatches"),
        ("PUT", "/repos/example-org/deploys/pulls/1/merge"),
    ):
        with pytest.raises(WriteRefused):
            release.api.call(method, path, {})
    assert recorded.sent == []


GH = {
    "repository": "example-org/deploys",
    "apply_workflow": "aisre-apply.yml",
    "undo_workflow": "aisre-undo.yml",
}


def test_built_only_outside_production_and_only_with_a_write_credential(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("built with a network call")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    with pytest.raises(ValidationError, match="HG-10"):
        Settings(env="production", release_adapter="github_actions", release_github=json.dumps(GH))
    with pytest.raises(ReleaseRefused, match="AISRE_RELEASE_URL"):
        release_from_settings(
            Settings(env="test", release_adapter="github_actions", release_github=json.dumps(GH)),
            lambda d: b"",
        )
    with pytest.raises(ValidationError, match="AISRE_RELEASE_GITHUB"):
        Settings(env="test", release_adapter="github_actions")
    built = release_from_settings(
        Settings(
            env="test",
            release_adapter="github_actions",
            release_url=API,
            release_token=TOKEN,
            release_github=json.dumps(GH),
        ),
        lambda d: b"",
    )
    assert isinstance(built, GitHubActionsRelease)
    with pytest.raises(ReleaseRefused, match="repository"):
        release_from_settings(
            Settings(
                env="test",
                release_adapter="github_actions",
                release_url=API,
                release_token=TOKEN,
                release_github=json.dumps({**GH, "repository": "no slash"}),
            ),
            lambda d: b"",
        )
