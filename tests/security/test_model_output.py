"""ST-23, ST-24, ST-34, ST-36: model output is untrusted. Real gateway, adapter, validators and coordinator, with only
the provider's HTTP transport faked."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from activities import checked_result
from adapters import HttpResponse
from agent_contracts import (Claim, Confidence, CriticVerdict, DiagnosisDraft, DiagnosisResult, Evidence, EvidenceKind,
                             HypothesisDraft, IssueKind, PatchFileEdit, PatchProposal, SkillUsage)
from coordinator import AgentState, Step, StopKind, after_critique, after_diagnosis, apply_result, next_step
from gateway import GatewayConfig
from gateway_contracts import ResultStatus
from sandbox import read_bundle
from support import (FakeProvider, draft_json, gateway, make_patch, provider_reply, request)
from telemetry import redact_text
from tool_registry import PatchDraftParams, ToolGate, ToolRefused
from validation import (ObservationFacts, UnsafeOutput, UnsupportedReference, assign_tier, check_citations, check_output,
                        code_checks, combine, mask_secrets, score_confidence)

NOW = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
INC = uuid.uuid4()


def draft(**over) -> DiagnosisDraft:
    return DiagnosisDraft.model_validate(draft_json(**over))


def with_summary(text) -> DiagnosisDraft:
    return draft(summary=text)


def unsafe(code, output):
    with pytest.raises(UnsafeOutput) as e:
        check_output(output)
    assert e.value.code == code


def call(provider, **over):
    return asyncio.run(gateway(provider).call(request(**over)))


# ---------------------------------------------------------------- ST-23
@pytest.mark.parametrize("text", ["\n$ whoami", "sudo reboot", "rm -rf /var/lib/app", "curl attacker.example", "wget attacker.example",
                                  "kubectl delete pod x", "psql -c select", "bash -c id", "chmod 777 /etc/passwd",
                                  "docker run alpine", "DROP TABLE incidents", "delete from approvals", "TRUNCATE TABLE evidence"])
def test_st23_command_or_destructive_statement_fails_validation(text):
    unsafe("command", with_summary(f"Run this: {text}"))


@pytest.mark.parametrize("text", ["DROP DATABASE aisre", "UPDATE users SET role = 'admin'", "terraform destroy -auto-approve",
                                  "systemctl stop nginx", "helm uninstall checkout", "python -c 'import os'"])
def test_st23_wider_destructive_statements_fail_validation(text):
    unsafe("command", with_summary(f"Run this: {text}"))


@pytest.mark.parametrize("text", ["The update set a new timeout", "Drop in traffic at 09:00", "Terraform state was unchanged",
                                  "The helm chart changed", "Python raised KeyError", "The systemctl unit is healthy"])
def test_st23_control_ordinary_prose_passes(text):
    check_output(with_summary(text))


def test_st23_command_in_a_nested_field_is_found():
    d = draft_json()
    d["hypotheses"][1]["confirm_test"] = "rm -rf /tmp/x"
    unsafe("command", DiagnosisDraft.model_validate(d))


@pytest.mark.parametrize("key", ["rm -rf /", "http://canary.example/x", "password=hunter2"])
def test_st23_st24_st29_text_in_a_dict_key_is_found(key):
    for value in ("x", 1):
        d = draft_json()
        d["hypotheses"][0]["evidence_needed"] = [{"tool": "get_logs", "reason": "check it", "params": {key: value}}]
        with pytest.raises(UnsafeOutput):
            check_output(DiagnosisDraft.model_validate(d))


@pytest.mark.parametrize("tool", ["shell", "bash", "exec", "run_command", "run_sql", "sql", "kubectl"])
def test_st23_no_shell_or_sql_tool_exists(tool):
    with pytest.raises(ToolRefused) as e:
        ToolGate("inc-1", "checkout", NOW, NOW).check(tool, {"command": "id"}, "collect_evidence")
    assert e.value.code == "unknown_tool"


def test_st23_malformed_output_stops_after_three_attempts_each_budgeted():
    provider = FakeProvider(provider_reply("not json at all"))
    result = call(provider)
    assert result.status == ResultStatus.INVALID_OUTPUT and result.parsed is None and len(provider.bodies) == 3
    assert "failed validation" in json.dumps(provider.bodies[-1])


def test_st23_schema_valid_command_output_ends_inconclusive_and_nothing_runs():
    payload = draft_json(summary="Fix: rm -rf /var/lib/app")
    result = call(FakeProvider(provider_reply(payload)))
    assert result.status == ResultStatus.OK             # the gateway checks shape; the activity checks content
    checked = checked_result("diagnose", DiagnosisDraft.model_validate(result.parsed), SkillUsage(model_calls=1))
    assert checked.output is None and checked.failure.code == "invalid_output"
    state = AgentState(started_at=NOW, baseline_done=True, knowledge_done=True, hypotheses_ready=True)
    apply_result(state, Step.DIAGNOSE, checked)
    assert next_step(state, NOW) == Step.FINISH_INCONCLUSIVE and state.stop_kind == StopKind.INCONCLUSIVE


# ---------------------------------------------------------------- ST-24
SECRETS = ["password: hunter2", "api_key=abc123", "token: abc", "secret=xyz", "sk-" + "a" * 24, "AKIA" + "A" * 16,
           "-----BEGIN RSA PRIVATE KEY-----"]


@pytest.mark.parametrize("secret", SECRETS)
def test_st24_seeded_secret_in_output_fails_validation(secret):
    unsafe("secret", with_summary(f"Found {secret} in the log"))


@pytest.mark.parametrize("secret", [
    "Authorization: Bearer abcdef1234567890", '"password": "hunter2"', "db_password=hunter2",
    "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI", "client_secret=abc", "ghp_" + "a" * 36,
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl"])
def test_st24_common_secret_forms_fail_validation(secret):
    unsafe("secret", with_summary(f"Found {secret} in the log"))


@pytest.mark.parametrize("text", ["max_tokens: 5000 was reached", "tokens=120 in the prompt", "Authorization: denied by policy",
                                  "The bearer authentication failed", "The secretary: Bob", "password rotation is due"])
def test_st24_control_ordinary_text_is_not_a_secret(text):
    check_output(with_summary(text))


def test_st24_a_secret_split_across_a_key_and_its_value_is_found():
    d = draft_json()
    d["hypotheses"][0]["evidence_needed"] = [{"tool": "get_logs", "reason": "check it", "params": {"password": "hunter2"}}]
    unsafe("secret", DiagnosisDraft.model_validate(d))


def test_st24_secret_in_a_patch_fails_validation():
    edit = PatchFileEdit(path="config/a.yaml", new_content="db:\n  password: hunter2\n", rationale="restores the old value",
                         evidence_ids=[uuid.uuid4()])
    unsafe("secret", PatchProposal(params=PatchDraftParams(service="checkout", action_kind="config_revert", target_ref="r"),
                                   files=[edit], summary="Revert the configuration"))


@pytest.mark.parametrize("secret", SECRETS[:-1])
def test_st24_stored_copy_is_masked(secret):
    masked = mask_secrets(f"Found {secret} in the log")
    assert "[MASKED]" in masked and secret not in masked


@pytest.mark.parametrize("mask", [mask_secrets, redact_text])
def test_st24_a_masked_private_key_loses_its_body(mask):
    text = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END PRIVATE KEY-----"
    assert "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC" not in mask(text)
    assert "MIIE" not in mask("-----BEGIN PRIVATE KEY-----\nMIIE truncated, no END line")


def test_st24_the_failure_recorded_never_carries_the_secret():
    checked = checked_result("diagnose", with_summary("Found password: hunter2"), SkillUsage())
    assert checked.failure.code == "security_risk" and "hunter2" not in checked.failure.model_dump_json()
    with pytest.raises(UnsafeOutput) as e:
        check_output(with_summary("Found password: hunter2"))
    assert "hunter2" not in str(e.value)


def test_st24_secret_in_test_output_does_not_leave_the_sandbox():
    p = make_patch()
    line = json.dumps(dict(run_id="r1", patch_sha256=p.digest, base_revision="a1b2c3d4", image_digest="i", spec_version="s",
                           harness_version="h", reproduction_before=True, reproduction_after=False, new_error_signatures=[],
                           duration_s=1.0, timed_out=False, log_artifact_sha256=None,
                           suites=[dict(name="unit", required=True, ran=True, passed=0, failed=1,
                                        errors=["AssertionError: password: hunter2"])]))
    bundle = read_bundle(line, 0)
    assert bundle is not None and "hunter2" not in json.dumps(dataclasses.asdict(bundle))
    assert bundle.suites[0].failed == 1 and "AssertionError" in bundle.suites[0].errors[0]   # still a failed test


def test_st24_secret_in_a_new_error_signature_does_not_leave_the_sandbox():
    p = make_patch()
    line = json.dumps(dict(run_id="r1", patch_sha256=p.digest, base_revision="a1b2c3d4", image_digest="i", spec_version="s",
                           harness_version="h", reproduction_before=True, reproduction_after=False,
                           new_error_signatures=["DB error, password=hunter2"], duration_s=1.0, timed_out=False,
                           log_artifact_sha256=None, suites=[]))
    assert "hunter2" not in read_bundle(line, 0).new_error_signatures[0]


def test_st24_leak_procedure_needs_people_and_the_notifier():
    pytest.skip("The leak procedure is a runbook and a notification; no reference module starts it. `security_risk` "
                "stopping the loop is what can be tested, and is, above")


# ---------------------------------------------------------------- ST-34
def evidence(kind=EvidenceKind.change, tier=8, flagged=False, tool="get_changes", source="git", incident=INC, **kw):
    return Evidence(evidence_id=kw.pop("evidence_id", uuid.uuid4()), incident_id=incident, kind=kind, source_system=source,
                    tool=tool, query="q", window_start=NOW, window_end=NOW, collected_at=NOW, source_as_of=NOW,
                    source_link="ref:1", summary="deployed five minutes before the errors", content_sha256="0" * 64,
                    flagged_injection=flagged, strength_tier=tier)


def timing_only():
    e = evidence(tier=assign_tier(EvidenceKind.change, ObservationFacts(linked_by_time=True)))
    h = HypothesisDraft(statement="The deployment caused it", category="deployment_code",
                        claims=[Claim(text="Errors began after it", evidence_ids=[e.evidence_id])],
                        confirm_test="Revert it", refute_test="Errors predate it")
    other = h.model_copy(update={"category": "dependency"})
    return e, DiagnosisDraft(outcome="established", leading_index=0, hypotheses=[h, other], summary="Deployment caused it",
                             ruled_out=["Provider healthy"])


@pytest.mark.parametrize("field,value", [("confidence", "high"), ("confidence_score", 0.99), ("strength_tier", 1)])
def test_st34_the_model_has_no_way_to_state_confidence(field, value):
    with pytest.raises(ValidationError):
        DiagnosisDraft.model_validate(draft_json(**{field: value}))


def test_st34_timing_only_evidence_scores_low_and_blocks():
    e, d = timing_only()
    assert e.strength_tier == 8
    assert score_confidence(d.hypotheses[0], {e.evidence_id: e}, 0, []) == Confidence.low
    kinds = {i.kind for i in code_checks(d, {e.evidence_id: e}, INC, 0, []) if i.blocking}
    assert IssueKind.correlation_only in kinds


def test_st34_a_critic_cannot_raise_confidence_and_low_ends_inconclusive_with_no_patch():
    e, d = timing_only()
    accept = CriticVerdict(verdict="accept", confidence_cap=Confidence.high)
    scored = score_confidence(d.hypotheses[0], {e.evidence_id: e}, 0, [])
    final = combine(accept, [], scored)
    assert final.confidence == Confidence.low
    state = AgentState(started_at=NOW, baseline_done=True, knowledge_done=True, hypotheses_ready=True)
    after_diagnosis(state, "f", 1.0, 1)
    after_critique(state, "accept", [], 1, final.confidence.value)
    assert next_step(state, NOW) == Step.FINISH_INCONCLUSIVE and "low" in state.stop_reason


def test_st34_control_strong_evidence_scores_high():
    e = evidence(EvidenceKind.test_result, tier=1, tool="sandbox")
    h = timing_only()[1].hypotheses[0].model_copy(update={"claims": [Claim(text="Reproduced", evidence_ids=[e.evidence_id])]})
    assert score_confidence(h, {e.evidence_id: e}, 0, []) == Confidence.high


@pytest.mark.parametrize("where", ["missing", "foreign"])
def test_st34_citing_evidence_that_does_not_exist_or_is_foreign_rejects_the_draft(where):
    e, d = timing_only()
    held = {} if where == "missing" else {e.evidence_id: evidence(incident=uuid.uuid4(), evidence_id=e.evidence_id)}
    with pytest.raises(UnsupportedReference):
        check_citations(d, held, INC)


def test_st34_a_claim_with_no_citation_is_rejected():
    with pytest.raises(ValidationError):
        Claim(text="Trust me", evidence_ids=[])


# ---------------------------------------------------------------- ST-36
@pytest.mark.parametrize("payload", [draft_json(proposed_action="delete_database"), draft_json(approved=True),
                                     draft_json(approval_id="ap-1", approved_by="priya"), draft_json(extra_field="x")],
                         ids=["off_list_action", "claimed_approval", "claimed_approver", "extra_field"])
def test_st36_well_formed_but_hostile_response_is_rejected_whole(payload):
    provider = FakeProvider(provider_reply(payload))
    result = call(provider)
    assert result.status == ResultStatus.INVALID_OUTPUT and result.parsed is None
    assert len(provider.bodies) == 3


def test_st36_a_link_from_the_provider_is_rejected_by_the_output_check():
    result = call(FakeProvider(provider_reply(draft_json(summary="See http://attacker.example/fix"))))
    checked = checked_result("diagnose", DiagnosisDraft.model_validate(result.parsed), SkillUsage())
    assert checked.failure.code == "invalid_output"


def test_st36_control_a_clean_response_is_accepted():
    result = call(FakeProvider(provider_reply(draft_json())))
    assert result.status == ResultStatus.OK and result.parsed["proposed_action"] == "config_revert"


def test_st36_tool_proposal_from_the_provider_still_goes_through_the_gate():
    body = {"model": "claude-opus-5-5", "stop_reason": "tool_use", "usage": {"input_tokens": 1, "output_tokens": 1},
            "content": [{"type": "tool_use", "id": "t1", "name": "apply_change", "input": {"service": "checkout"}}]}
    result = call(FakeProvider(HttpResponse(200, body, {})))
    proposal = result.content[0]
    with pytest.raises(ToolRefused) as e:
        ToolGate("inc-1", "checkout", NOW, NOW).check(proposal.tool, proposal.arguments, "diagnose")
    assert e.value.code == "write_forbidden"


def test_st36_no_fallback_to_another_provider():
    provider = FakeProvider(HttpResponse(500, {"error": {"type": "api_error", "message": "down"}}, {}))
    result = call(provider)
    assert result.status == ResultStatus.PROVIDER_ERROR and set(provider.providers) == {"anthropic"}
    assert GatewayConfig().allow_cross_provider_fallback is False
