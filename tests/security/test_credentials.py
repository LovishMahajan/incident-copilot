"""ST-16 to ST-18: what credentials each part of the platform can reach."""

from __future__ import annotations

import ast
import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from aisre.remediation import dispatcher
from aisre.remediation.action import MATRIX, Perm, Role
from aisre.remediation.patch import _protected
from aisre.remediation.sandbox import SandboxSpec, SpecRejected, launch_args, run_sandbox, validate_args
from support import T0, FakeProvider, draft_json, gateway, provider_reply, request
from aisre.agent.tool_registry import REGISTRY, SKILL_TOOLS, ToolGate, ToolKind, ToolRefused, agent_tools, tool_schema_for_model


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


@pytest.mark.parametrize("env", ["--env=DATABASE_URL=postgres://prod", "--env=API_TOKEN=x", "--env=DB_PASSWORD=x",
                                 "--env=AWS_ACCESS_KEY_ID=x", "--env=MY_SECRET=x", "--env=SIGNING_KEY=x"])
def test_st16_credential_variables_are_refused(env):
    with pytest.raises(SpecRejected):
        validate_args(spliced(env))


@pytest.mark.parametrize("extra", [("-e", "DATABASE_URL=postgres://prod"), ("--env", "AWS_SECRET_ACCESS_KEY=x"),
                                   ("--env-file=/run/secrets/prod.env",)])
def test_st16_credential_variables_are_refused_in_every_spelling(extra):
    with pytest.raises(SpecRejected):
        validate_args(spliced(*extra))


def test_st16_generated_code_cannot_edit_secret_files_or_the_launcher():
    assert all(_protected(p) for p in ("deploy/.env", "secrets.yaml", "sandbox/launcher.py", "harness/run.py"))


def test_st16_real_read_of_env_files_and_mounts_needs_a_sandbox():
    pytest.skip("Needs a running gVisor sandbox: runsc is not installed, and only generated code running inside one "
                "can show that it finds no credentials and reaches no production host")


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


def test_st17_prompts_across_a_real_investigation_need_the_evidence_adapters():
    pytest.skip("The evidence adapters that hold the credentials and redact extracts don't exist (collect_evidence is "
                "NotImplementedError), so tool results and whole-investigation prompts can't be captured and searched")


# ---------------------------------------------------------------- ST-18
def test_st18_only_the_dispatcher_identity_may_execute():
    assert [r for r, perms in MATRIX.items() if Perm.EXECUTE in perms] == [Role.SYSTEM_DISPATCHER]


@pytest.mark.parametrize("role", [Role.SYSTEM_WORKFLOW, Role.SYSTEM_DISPATCHER])
def test_st18_platform_identities_hold_no_approval_or_key_rights(role):
    assert not MATRIX[role] & {Perm.APPROVE, Perm.REJECT, Perm.REVOKE_OWN, Perm.MANAGE_KEYS, Perm.EDIT_POLICY}


@pytest.mark.parametrize("skill", sorted(SKILL_TOOLS))
def test_st18_worker_tools_cannot_call_apply(skill):
    assert not [t for t in agent_tools(skill) if t.kind == ToolKind.WRITE]
    with pytest.raises(ToolRefused) as e:
        ToolGate("inc-1", "checkout", T0, T0).check("apply_change", {}, skill)
    assert e.value.code == "write_forbidden"


def test_st18_the_dispatcher_module_cannot_reach_the_signing_key():
    tree = ast.parse(Path(dispatcher.__file__).read_text())
    imported = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
    assert not imported & {"Signer", "Ed25519PrivateKey", "record_decision"}


def test_st18_review_secrets_and_live_database_roles_need_real_systems():
    pytest.skip("Merging with the draft-pull-request credential, reading the secrets manager and calling the release "
                "process as a worker need those real systems. Database-role rights are proved by schema-tests.sql "
                "(see README), which needs PostgreSQL")
