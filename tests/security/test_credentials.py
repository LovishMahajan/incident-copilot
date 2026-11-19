"""ST-16 to ST-18: what credentials each part of the platform can reach."""

from __future__ import annotations

import ast
import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from monitoring_scenarios import copy_reference
from skill_rig import collected, investigate
from support import T0, FakeProvider, draft_json, gateway, provider_reply, request

from aisre.agent.tool_registry import (
    REGISTRY,
    SKILL_TOOLS,
    ToolGate,
    ToolKind,
    ToolRefused,
    agent_tools,
    tool_schema_for_model,
)
from aisre.remediation import dispatcher
from aisre.remediation.action import MATRIX, Perm, Role
from aisre.remediation.patch import _protected
from aisre.remediation.sandbox import (
    SandboxSpec,
    SpecRejected,
    launch_args,
    run_sandbox,
    validate_args,
)

ROOT = Path(__file__).resolve().parents[2]


def spliced(*extra):
    a = launch_args(SandboxSpec(), "run1", "ws")
    return a[:-2] + list(extra) + a[-2:]


# ---------------------------------------------------------------- ST-16
def test_st16_the_only_variable_in_the_sandbox_is_the_marker():
    a = launch_args(SandboxSpec(), "run1", "ws")
    assert [x for x in a if x.startswith(("--env", "-e"))] == ["--env=AISRE_SANDBOX=1"]
    assert not [x for x in a if x.startswith(("--env-file", "--secret", "-v", "--volume"))]


def test_st16_parent_environment_does_not_reach_the_launch_arguments(monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "canary-prod-credential")
    seen = {}

    class Proc:
        returncode = 0

        def communicate(self, timeout=None):
            return "", ""

    def popen(args, **kw):
        seen["args"] = args
        return Proc()

    def run(args, **kw):
        return subprocess.CompletedProcess(args, 0, "", "")

    run_sandbox(SandboxSpec(), "r1", "0" * 64, "a1b2c3d4", popen=popen, run=run)
    assert "canary-prod-credential" not in " ".join(seen["args"])


@pytest.mark.parametrize(
    "env",
    [
        "--env=DATABASE_URL=postgres://prod",
        "--env=API_TOKEN=x",
        "--env=DB_PASSWORD=x",
        "--env=AWS_ACCESS_KEY_ID=x",
        "--env=MY_SECRET=x",
        "--env=SIGNING_KEY=x",
    ],
)
def test_st16_credential_variables_are_refused(env):
    with pytest.raises(SpecRejected):
        validate_args(spliced(env))


@pytest.mark.parametrize(
    "extra",
    [
        ("-e", "DATABASE_URL=postgres://prod"),
        ("--env", "AWS_SECRET_ACCESS_KEY=x"),
        ("--env-file=/run/secrets/prod.env",),
    ],
)
def test_st16_credential_variables_are_refused_in_every_spelling(extra):
    with pytest.raises(SpecRejected):
        validate_args(spliced(*extra))


def test_st16_generated_code_cannot_edit_secret_files_or_the_launcher():
    assert all(
        _protected(p)
        for p in ("deploy/.env", "secrets.yaml", "sandbox/launcher.py", "harness/run.py")
    )


def test_st16_real_read_of_env_files_and_mounts_needs_a_sandbox():
    pytest.skip(
        "Needs a running gVisor sandbox: runsc is not installed, and only generated code running inside one "
        "can show that it finds no credentials and reaches no production host"
    )


# ---------------------------------------------------------------- ST-17
def test_st17_no_tool_hands_a_credential_to_the_model():
    for skill in SKILL_TOOLS:
        text = json.dumps(tool_schema_for_model(skill), default=str).lower()
        assert "credential" not in text
    words = ("secret", "credential", "token", "password", "vault", "key")
    assert not [n for n in REGISTRY if any(w in n for w in words)]


@pytest.mark.parametrize("tool", ["get_secret", "get_credentials", "read_env", "vault_read"])
def test_st17_asking_for_a_credential_by_tool_is_refused(tool):
    gate = ToolGate("inc-1", "checkout", T0, T0)
    with pytest.raises(ToolRefused) as e:
        gate.check(tool, {"service": "checkout"}, "collect_evidence")
    assert e.value.code == "unknown_tool"


def test_st17_platform_credentials_are_not_in_any_prompt(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-canary-provider-key-000000")
    monkeypatch.setenv("DATABASE_URL", "postgres://canary:canary@prod/aisre")
    provider = FakeProvider(provider_reply(draft_json()))
    asyncio.run(gateway(provider).call(request()))
    assert len(provider.bodies) == 1
    sent = json.dumps(provider.bodies[0])
    assert "canary" not in sent and "headers" not in provider.bodies[0]


def test_st17_no_credential_reaches_any_prompt_of_a_whole_investigation(monkeypatch, tmp_path):
    """The real collector reads a log line holding a key; the real skills run the whole loop
    (hypothesise, diagnose, critique) with the process environment full of credentials. Neither
    reaches any prompt the provider is sent, nor any stored extract."""
    for var, value in {
        "AISRE_DATABASE_URL": "postgresql+psycopg://aisre_app:canary-db-pass@prod/aisre",
        "AISRE_PROVIDER_API_KEY": "canary-provider-key",
        "AISRE_OBJECT_STORE_SECRET_KEY": "canary-store-secret",
        "ANTHROPIC_API_KEY": "canary-anthropic-key",
    }.items():
        monkeypatch.setenv(var, value)
    root = copy_reference(tmp_path)
    logs = root / "reference-incident/logs.ndjson"
    logs.write_text(
        logs.read_text().replace(
            "payment gateway did not answer within 3000 ms",
            "payment gateway did not answer within 3000 ms (api_key=canary-tool-key)",
        )
    )
    inputs = collected(root)
    assert "within 3000 ms" in json.dumps([e.summary for e in inputs.evidence])
    s, _, transport = investigate(inputs)
    assert s.stop_reason == "critic accepted" and len(transport.bodies) == 3
    sent = json.dumps(transport.bodies) + json.dumps([e.summary for e in inputs.evidence])
    assert "canary" not in sent


# ---------------------------------------------------------------- ST-18
def test_st18_only_the_dispatcher_identity_may_execute():
    assert [r for r, perms in MATRIX.items() if Perm.EXECUTE in perms] == [Role.SYSTEM_DISPATCHER]


@pytest.mark.parametrize("role", [Role.SYSTEM_WORKFLOW, Role.SYSTEM_DISPATCHER])
def test_st18_platform_identities_hold_no_approval_or_key_rights(role):
    assert not MATRIX[role] & {
        Perm.APPROVE,
        Perm.REJECT,
        Perm.REVOKE_OWN,
        Perm.MANAGE_KEYS,
        Perm.EDIT_POLICY,
    }


@pytest.mark.parametrize("skill", sorted(SKILL_TOOLS))
def test_st18_worker_tools_cannot_call_apply(skill):
    assert not [t for t in agent_tools(skill) if t.kind == ToolKind.WRITE]
    with pytest.raises(ToolRefused) as e:
        ToolGate("inc-1", "checkout", T0, T0).check("apply_change", {}, skill)
    assert e.value.code == "write_forbidden"


def test_st18_the_dispatcher_module_cannot_reach_the_signing_key():
    tree = ast.parse(Path(dispatcher.__file__).read_text())
    imported = {
        a.name
        for n in ast.walk(tree)
        if isinstance(n, (ast.Import, ast.ImportFrom))
        for a in n.names
    }
    assert not imported & {"Signer", "Ed25519PrivateKey", "record_decision"}


def _fake(name: str):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        f"fake_{name}_st18", ROOT / f"ops/fakes/{name}.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _call(url: str, method: str, body: dict | None = None, token: str | None = None) -> int:
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        url,
        data=json.dumps(body or {}).encode(),
        method=method,
        headers={"Authorization": f"Bearer {token}"} if token else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return int(r.status)
    except urllib.error.HTTPError as e:
        return int(e.code)


def _service_env(compose: dict, name: str) -> dict:
    return compose["services"][name].get("environment") or {}


def test_st18_only_the_dispatcher_holds_the_release_credential_and_it_has_no_docker():
    """T3.12 on the stand-ins (ADR-51, ADR-73): in compose.yaml the release URL and token reach the
    dispatcher only; the worker carries the adapter name alone, and the API, the worker and the
    dispatcher mount no Docker socket (only the Test Runner, on the host, has Docker)."""
    import yaml

    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    holders = {
        name
        for name in compose["services"]
        if {"AISRE_RELEASE_URL", "AISRE_RELEASE_TOKEN"} & set(_service_env(compose, name))
    }
    assert holders == {"dispatcher"}
    assert set(_service_env(compose, "worker")) & {"AISRE_RELEASE_ADAPTER"}
    for name in ("api", "worker", "dispatcher"):
        volumes = " ".join(str(v) for v in compose["services"][name].get("volumes") or [])
        assert "docker.sock" not in volumes, name
    signing = {
        name
        for name in compose["services"]
        if "AISRE_APPROVAL_SIGNING_KEY" in _service_env(compose, name)
    }
    assert signing <= {
        "api"
    }  # the Approval Service signs; the dispatcher holds the public key only


def test_st18_without_the_release_credential_nothing_is_applied(tmp_path):
    """A caller without the dispatcher's token (the worker holds none) is refused by the fake
    release process, and the deployment is unchanged; the token works, as a control."""
    release_fake = _fake("release")
    repo = tmp_path / "orders-api"
    release_fake.review.build(repo)
    release = release_fake.Release(release_fake.review.Repo(repo, "main"), "main")
    server = release_fake.serve(release, "the-dispatchers-token")
    url = f"http://127.0.0.1:{server.server_address[1]}/v1/deployments/orders-api-production"
    try:
        body = {"execution_key": "k1", "base_revision": release.revision, "to_revision": "r1"}
        assert _call(f"{url}/undo", "POST", body) == 401
        assert _call(f"{url}/undo", "POST", body, token="a-worker-guess") == 401
        assert _call(f"{url}/changes/k1", "GET", token="a-worker-guess") == 401
        assert [h["kind"] for h in release.history] == ["start"]
        control = {**body, "to_revision": release.revision}
        assert _call(f"{url}/undo", "POST", control, token="the-dispatchers-token") == 200
    finally:
        server.shutdown()


def test_st18_the_draft_pull_request_credential_cannot_merge(tmp_path):
    """The worker's review credential opens draft pull requests only: the fake review system
    refuses a merge, as a branch protection rule would. A real review system must enforce it."""
    review_fake = _fake("review")
    repo = tmp_path / "orders-api"
    review_fake.build(repo)
    review = review_fake.Review(review_fake.Repo(repo, "main"))
    server = review_fake.serve(review)
    base = f"http://127.0.0.1:{server.server_address[1]}/v1/repos/orders-api/pulls/1/merge"
    try:
        assert _call(base, "PUT", token="the-workers-review-token") == 403
    finally:
        server.shutdown()


def test_st18_the_secrets_manager_and_the_real_systems_need_choosing():
    pytest.skip(
        "No secrets manager, identity system or real release and review systems are chosen (ADR-73): the "
        "signing key is a development key from settings, refused outside development and test. The stand-ins "
        "are checked above; database-role rights against PostgreSQL in tests/integration/test_live_roles.py"
    )
