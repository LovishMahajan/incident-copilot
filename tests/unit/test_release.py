"""Artifact 13.5 T3.1, ADR-73: the fake release process, the live metrics that follow it, and the
release adapter. Everything runs in-process on free ports; the reference repository is built
in a temporary directory, as fake-review builds it."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from aisre.config import Settings
from aisre.remediation.release import (
    FakeReleaseClient,
    ReleaseRefused,
    release_from_settings,
    remediation_enabled,
)

ROOT = Path(__file__).parents[2]
TOKEN = "fake-release-token"
CONFIG = "deploy/production.env"


def load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(f"fake_{name}_t31", ROOT / f"ops/fakes/{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release_fake, monitoring = load("release"), load("monitoring")


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    dest = tmp_path_factory.mktemp("release") / "orders-api"
    release_fake.review.build(dest)
    return release_fake.review.Repo(dest, "main")


@pytest.fixture
def stack(repo):
    release = release_fake.Release(repo, "main")
    server = release_fake.serve(release, TOKEN)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    mon = monitoring.serve(release_url=url)
    yield release, url, f"http://127.0.0.1:{mon.server_address[1]}"
    server.shutdown()
    mon.shutdown()


def revert_patch(release) -> bytes:
    """The reference config revert, as Patch.canonical_bytes() would give it."""
    old = release.files[CONFIG]
    lines = old.splitlines()
    n = next(i for i, ln in enumerate(lines) if ln.startswith("ORDER_TAX_RATE_BPS=")) + 1
    new = old.replace("ORDER_TAX_RATE_BPS=8.25", "ORDER_TAX_RATE_BPS=825")
    hunk = {
        "old_start": n,
        "old_lines": 1,
        "new_start": n,
        "new_lines": 1,
        "lines": ["-ORDER_TAX_RATE_BPS=8.25", "+ORDER_TAX_RATE_BPS=825"],
    }
    canonical = {
        "files": [
            {
                "path": CONFIG,
                "op": "modify",
                "old_sha256": hashlib.sha256(old.encode()).hexdigest(),
                "new_sha256": hashlib.sha256(new.encode()).hexdigest(),
                "hunks": [hunk],
            }
        ]
    }
    return json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()


def call(url: str, body: Any = None, token: str | None = TOKEN) -> tuple[int, Any]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def rate(mon_url: str) -> float:
    status, rows = call(f"{mon_url}/v1/live/orders-api-production/metrics", token=None)
    assert status == 200 and rows
    return float(rows[-1]["rate_500_total"])


def client(url: str, release) -> FakeReleaseClient:
    content = {hashlib.sha256(b).hexdigest(): b for b in [revert_patch(release)]}
    return FakeReleaseClient(url, SecretStr(TOKEN), content.__getitem__)


def test_it_starts_at_main_with_the_bad_config_and_monitoring_sees_the_failure(stack, repo):
    release, url, mon = stack
    state = asyncio.run(client(url, release).current_state("orders-api-production"))
    assert state.revision == repo.resolve("main") and state.in_progress_key is None
    assert rate(mon) > 0.5


def test_applying_the_config_revert_brings_the_error_rate_down_once(stack, repo):
    release, url, mon = stack
    c = client(url, release)
    patch = revert_patch(release)
    sha = hashlib.sha256(patch).hexdigest()
    base = repo.resolve("main")
    after = f"{base}+patch:{sha[:16]}"

    async def go() -> tuple[str, str, Any, str | None]:
        first = await c.apply("orders-api-production", "act-1", base, sha, after)
        again = await c.apply("orders-api-production", "act-1", base, sha, after)
        return (
            first,
            again,
            await c.current_state("orders-api-production"),
            await c.lookup("orders-api-production", "act-1"),
        )

    first, again, state, found = asyncio.run(go())
    assert (first, again, state.revision, found) == ("applied", "applied", after, "applied")
    assert len(release.history) == 2  # the repeat changed nothing (ADR-47)
    assert rate(mon) < 0.01


def test_undo_restores_the_earlier_revision_and_the_failure_returns(stack, repo):
    release, url, mon = stack
    c = client(url, release)
    patch = revert_patch(release)
    sha = hashlib.sha256(patch).hexdigest()
    base = repo.resolve("main")
    after = f"{base}+patch:{sha[:16]}"

    async def go() -> list[Any]:
        await c.apply("orders-api-production", "fix", base, sha, after)
        undo = await c.apply("orders-api-production", "rollback", after, None, base)
        return [undo, (await c.current_state("orders-api-production")).revision]

    assert asyncio.run(go()) == ["applied", base]
    assert rate(mon) > 0.5


def test_a_bad_change_brings_the_error_rate_up(stack):
    release, url, mon = stack
    release.files[CONFIG] = release.files[CONFIG].replace("8.25", "825")
    release._record(None)
    assert rate(mon) < 0.01
    status, _ = call(
        f"{url}/_fake/deployments/orders-api-production/config",
        {"set": {"ORDER_TAX_RATE_BPS": "eight"}},
        token=None,
    )
    assert status == 200 and rate(mon) > 0.5


@pytest.mark.parametrize(
    "change, why",
    [
        ({"base_revision": "0" * 40}, "deployment runs"),
        ({"patch_sha256": "f" * 64}, "do not hash"),
        ({"patch": '{"files":[{"path":"deploy/production.env"}]}'}, "do not hash"),
    ],
)
def test_a_change_that_does_not_fit_is_rejected_and_looked_up_as_failed(stack, repo, change, why):
    release, url, _ = stack
    patch = revert_patch(release).decode()
    body = {
        "execution_key": "k-bad",
        "base_revision": repo.resolve("main"),
        "patch_sha256": hashlib.sha256(patch.encode()).hexdigest(),
        "to_revision": "next",
        "patch": patch,
    } | change
    status, answer = call(f"{url}/v1/deployments/orders-api-production/apply", body)
    assert status == 200 and answer["result"] == "rejected" and why in answer["reason"]
    assert call(f"{url}/v1/deployments/orders-api-production/changes/k-bad")[1] == {
        "status": "failed"
    }
    assert release.revision == repo.resolve("main")


def test_writes_and_lookups_need_the_release_credential_reads_do_not(stack):
    _, url, _ = stack
    for token in (None, "wrong"):
        assert call(f"{url}/v1/deployments/orders-api-production/apply", {}, token)[0] == 401
        assert call(f"{url}/v1/deployments/orders-api-production/changes/x", None, token)[0] == 401
    assert call(f"{url}/v1/deployments/orders-api-production", token=None)[0] == 200


def test_settings_refuse_the_fake_release_in_production():
    with pytest.raises(ValidationError, match="development and test"):
        Settings(env="production", release_adapter="fake_release")
    with pytest.raises(ValidationError, match="go together"):
        Settings(release_adapter="fake_release", release_url="http://fake-release:8080")


def test_no_production_write_exists():
    """ADR-44, ADR-73: in production no adapter can be built, so the dispatcher can't start."""
    production = Settings(env="production")
    with pytest.raises(ReleaseRefused, match="ADR-44"):
        release_from_settings(production, lambda sha: b"")
    with pytest.raises(ReleaseRefused):
        release_from_settings(Settings(env="development"), lambda sha: b"")
    assert not remediation_enabled(production)


def test_the_approval_path_needs_the_fake_release_in_development_or_test():
    assert remediation_enabled(Settings(env="test", release_adapter="fake_release"))
    assert not remediation_enabled(Settings(env="test"))
