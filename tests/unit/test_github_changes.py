"""Artifact 9.4, 13.5 (M5, ADR-75): `get_changes` from GitHub, against recorded responses.

tests/fixtures/recorded/github-changes.json is shaped after GitHub's documented REST API
(2022-11-28, cited there and in aisre.evidence.github_changes) and reconstructs the reference
incident's release 2.14.1 as a deployment. It shows the mapping, not that the adapter works
against a real repository: that is the pilot's first-day check (docs/onboarding.md).
"""

from __future__ import annotations

import copy
import logging
import urllib.request
from datetime import UTC, datetime, timedelta

import pytest
from recorded_http import Recorded, load

from aisre.agent.tool_registry import ChangeQuery
from aisre.evidence import sources
from aisre.evidence.collector import redact
from aisre.evidence.facts import change_info
from aisre.evidence.sources import SourceSet, SourceUnavailable

TOKEN = "ghchanges-not-a-real-token-5d1e"
VAR = "AISRE_CHANGES_GITHUB_TOKEN"
REPO = "example-org/orders-api"
HEAD = "7d3e91a4c2b85f0e1d6a93b7c4e2f1a0b9d8c7e6"
WINDOW = {
    "start": datetime(2026, 10, 20, 13, 30, tzinfo=UTC),
    "end": datetime(2026, 10, 20, 14, 20, tzinfo=UTC),
}


@pytest.fixture
def github(monkeypatch):
    monkeypatch.setenv(VAR, TOKEN)

    def use(exchanges=None) -> Recorded:
        r = Recorded(exchanges if exchanges is not None else load("github-changes"))
        monkeypatch.setattr(urllib.request, "urlopen", r)
        return r

    return use


def source(**kw):
    spec = {
        "adapter": "github_changes",
        "repository": "example-org/$service",
        "token": {"env": VAR},
        "config_paths": ["deploy/**"],
    }
    loaded = SourceSet.from_config({"sources": {"get_changes": spec | kw}}, production=True)
    return loaded.for_tool("get_changes")


def changes(service="orders-api", **kw) -> ChangeQuery:
    return ChangeQuery(service=service, **(WINDOW | kw))


def test_registered_not_local_only_and_serves_get_changes_only():
    assert "github_changes" in sources.ADAPTERS
    assert "github_changes" not in sources.LOCAL_ONLY
    assert source().serves() == {"get_changes"}


def test_the_reference_incidents_release_reconstructed_from_a_deployment(github):
    r = github()
    result = source().fetch("get_changes", changes(), "production")
    assert result.source_system == "github-changes:changes"
    assert result.source_link == f"https://github.com/{REPO}/deployments"
    assert not result.partial
    (rec,) = result.records
    assert rec == {
        "change_id": "deployment 9142",
        "version": "2.14.1",
        "previous_version": "2.14.0",
        "code_changed": False,
        "files": ["deploy/production.env"],
        "commits": [
            {"sha": HEAD, "message": "Update tax rate config to match finance sheet (8.25%)"}
        ],
        "config_changes": [{"key": "ORDER_TAX_RATE_BPS", "old": "825", "new": "8.25"}],
        "config_revision": None,
        "stages": [
            {"step": "deployment_created", "at": "2026-10-20T13:58:00Z"},
            {"step": "deployment_queued", "at": "2026-10-20T13:58:05Z"},
            {"step": "deployment_in_progress", "at": "2026-10-20T14:02:00Z"},
        ],
        "rollout": [],
        "diff": rec["diff"],
    }
    assert rec["diff"].startswith("diff --git a/deploy/production.env b/deploy/production.env\n")
    assert "-ORDER_TAX_RATE_BPS=825\n+ORDER_TAX_RATE_BPS=8.25\n" in rec["diff"]
    # The fact rules see what they see from the fake: the key, the file, and the change's time.
    info = change_info(rec)
    assert info.keys == ("ORDER_TAX_RATE_BPS",) and info.files == ("deploy/production.env",)
    assert info.changed_at == datetime(2026, 10, 20, 14, 2, tzinfo=UTC)
    # Read-only, versioned, by bearer token; staging's deployment was never asked for (FR-44).
    assert {s.method for s in r.sent} == {"GET"}
    for s in r.sent:
        assert s.headers["authorization"] == f"Bearer {TOKEN}"
        assert s.headers["x-github-api-version"] == "2022-11-28"
    assert "environment=production" in r.to("GET", "/deployments")[0].query
    assert [s.path for s in r.sent] == [
        f"/repos/{REPO}/deployments",
        f"/repos/{REPO}/deployments/9142/statuses",
        f"/repos/{REPO}/compare/3f1c2aa90b7e6d5c4b3a29180f7e6d5c4b3a2918...{HEAD}",
    ]


def test_workflow_runs_on_the_default_branch_not_already_deployed(github):
    r = github()
    result = source(workflows=["release.yml"]).fetch("get_changes", changes(), "production")
    run, deployment = result.records
    assert deployment["change_id"] == "deployment 9142"
    assert run == {
        "change_id": "workflow run 556 (release.yml)",
        "version": "c0ffee0",
        "previous_version": None,
        "code_changed": None,
        "files": [],
        "commits": [{"sha": "c0ffee00" * 5, "message": "Bump request timeout\n\nlonger body"}],
        "config_changes": [],
        "config_revision": None,
        "stages": [
            {"step": "workflow_started", "at": "2026-10-20T13:44:02Z"},
            {"step": "workflow_failure", "at": "2026-10-20T13:49:30Z"},
        ],
        "rollout": [],
        "diff": None,
    }  # run 555 deployed 2.14.1 and is that deployment already
    (sent,) = r.to("GET", "/actions/workflows/release.yml/runs")
    assert "branch=main" in sent.query and "status=completed" in sent.query
    assert "created=2026-10-20T13:30:00Z..2026-10-20T14:20:00Z" in sent.query


def test_no_deployments_in_the_window_is_an_empty_answer_not_a_gap(github):
    github()
    result = source().fetch("get_changes", changes("quiet-api"), "production")
    assert result.records == [] and not result.partial


@pytest.mark.parametrize(
    "service, reason",
    [
        ("locked-api", "HTTP 401, the token was refused"),
        ("forbidden-api", "HTTP 403, the token may not read this"),
        ("gone-api", "HTTP 404, repository, workflow or commit not found"),
        ("busy-api", "rate limit used up (HTTP 403); resets 2026-10-20T12:40:00Z"),
        ("hurried-api", "rate limited (HTTP 429); retry after 60 s"),
    ],
)
def test_refusals_and_rate_limits_are_gaps_with_their_reason(github, service, reason, caplog):
    github()
    caplog.set_level(logging.DEBUG)
    with pytest.raises(SourceUnavailable, match=r"^github-changes: ") as e:
        source().fetch("get_changes", changes(service), "production")
    assert reason in str(e.value)
    assert TOKEN not in str(e.value) and "api.github.com" not in str(e.value)
    assert TOKEN not in caplog.text


def test_a_rate_limit_is_never_waited_out(github, monkeypatch):
    github()
    monkeypatch.setattr("time.sleep", lambda s: pytest.fail("slept on a rate limit"))
    with pytest.raises(SourceUnavailable, match="retry after 60 s"):
        source().fetch("get_changes", changes("hurried-api"), "production")


def test_pagination_stops_at_its_cap_and_says_partial(github):
    base = load("github-changes")[0]["body"][0]
    page = []
    for i in range(100):
        d = copy.deepcopy(base)
        d.update(id=10_000 + i, created_at=(WINDOW["end"] - timedelta(seconds=i + 1)).isoformat())
        page.append(d)
    deployments = {"method": "GET", "path": "/repos/example-org/busy-deploys/deployments"}
    statuses = {"method": "GET", "path": "/repos/example-org/busy-deploys/deployments/10000/statuses"}  # fmt: skip
    r = github(
        [
            deployments | {"when": ["page=1"], "status": 200, "body": page},
            deployments | {"when": ["page=2"], "status": 200, "body": page},
            statuses | {"status": 200, "body": []},
        ]
    )
    result = source(max_pages=2, max_deployments=1).fetch(
        "get_changes", changes("busy-deploys"), "production"
    )
    assert result.partial
    pages = [s.query for s in r.sent if s.path.endswith("/deployments")]
    assert len(pages) == 2 and "page=2" in pages[1]  # two pages, then the cap
    assert [rec["change_id"] for rec in result.records] == ["deployment 10000"]
    # Each deployment read costs requests: only the newest `max_deployments` are read.
    assert len(r.to("GET", "/statuses")) == 1


def test_github_enterprise_base_url(github):
    exchanges = [ex | {"path": "/api/v3" + ex["path"]} for ex in load("github-changes")]
    r = github(exchanges)
    src = source(url="https://ghe.example.com/api/v3")
    result = src.fetch("get_changes", changes(), "production")
    assert result.source_link == f"https://ghe.example.com/{REPO}/deployments"
    assert [rec["version"] for rec in result.records] == ["2.14.1"]
    assert all(s.url.startswith("https://ghe.example.com/api/v3/repos/") for s in r.sent)


def test_environments_map_the_platforms_to_githubs_and_stay_in_scope(github):
    github()
    src = source(environments={"production": ["production"], "staging": ["staging"]})
    (staged,) = src.fetch("get_changes", changes(), "staging").records
    assert staged["change_id"] == "deployment 9140" and staged["previous_version"] is None
    with pytest.raises(SourceUnavailable, match="no GitHub environment for qa"):
        src.fetch("get_changes", changes(), "qa")


def test_secrets_in_a_config_diff_are_redacted_by_the_collectors_path(github):
    exchanges = load("github-changes")
    (cmp,) = [ex for ex in exchanges if "/compare/" in ex["path"]]
    cmp["body"]["files"][0]["patch"] += "\n-PAYMENTS_API_KEY=sk-live0123456789abcdefXYZ\n+PAYMENTS_API_KEY=sk-live9876543210fedcbaXYZ"  # fmt: skip
    github(exchanges)
    (rec,) = source().fetch("get_changes", changes(), "production").records
    stored = str(redact(rec))
    assert "sk-live" not in stored and "[REDACTED]" in stored
    assert "ORDER_TAX_RATE_BPS=8.25" in stored


def test_building_reaches_nothing_and_a_missing_token_is_a_gap(monkeypatch):
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *a, **k: pytest.fail("reached the network")
    )
    monkeypatch.delenv(VAR, raising=False)
    src = source()
    with pytest.raises(SourceUnavailable, match=f"environment variable {VAR}"):
        src.fetch("get_changes", changes(), "production")
    with pytest.raises(SourceUnavailable, match="has no get_logs"):
        src.fetch("get_logs", changes(), "production")


@pytest.mark.parametrize(
    "extra, error",
    [
        ({"token": TOKEN}, "by reference only"),
        ({"token": {"env": "AISRE_RELEASE_TOKEN"}}, "release write credential"),
        ({"repository": "orders-api"}, "owner/name"),
        ({"url": "http://api.github.com"}, "https"),
        ({"workflows": ["../release.yml"]}, "file names"),
        ({"workflows": "release.yml"}, "file names"),
        ({"environments": {"production": []}}, "GitHub names"),
        ({"max_pages": 0}, "positive"),
    ],
)
def test_bad_configuration_is_refused_at_start(extra, error):
    with pytest.raises(ValueError, match=error):
        source(**extra)
