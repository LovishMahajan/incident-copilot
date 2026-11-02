"""Tests for contracts.py (Artifact 7.3, 7.5): it must not drift from schema.sql.

The enum, column and view checks parse schema.sql itself, and the event list parses
Artifact 7.5, so a change to either fails here until contracts.py follows.
"""

from __future__ import annotations

import enum
import inspect
import re
import sys
import types
import typing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

import contracts as c

HERE = Path(__file__).parent
LAYERS = HERE.parents[1]
for layer in ("layer-08-workflows-state-machines", "layer-09-agent-architecture",
              "layer-10-inference-retrieval"):
    sys.path.append(str(LAYERS / layer / "files"))

SCHEMA = re.sub(r"--.*$", "", (HERE / "schema.sql").read_text(), flags=re.M)
ARTIFACT = (HERE.parent / "artifact-07-data-and-interface-specification.md").read_text()

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
SHA = "a" * 64


# ---------------------------------------------------------------------------
# Schema parsers
# ---------------------------------------------------------------------------
def schema_enums() -> dict[str, list[str]]:
    found = re.findall(r"CREATE TYPE (\w+) AS ENUM \((.*?)\);", SCHEMA, flags=re.S)
    return {name: re.findall(r"'(\w+)'", body) for name, body in found}


def table_columns(table: str) -> dict[str, bool]:
    """Column name to whether it is nullable."""
    body = re.search(rf"CREATE TABLE {table} \((.*?)\n\);", SCHEMA, flags=re.S).group(1)
    columns = {}
    for line in body.splitlines():
        match = re.match(r"  (\w+)\s", line)
        if match and match.group(1) not in {"UNIQUE", "FOREIGN", "CHECK", "PRIMARY"}:
            columns[match.group(1)] = "NOT NULL" not in line and "PRIMARY KEY" not in line
    return columns


def contract_enums() -> dict[str, type[enum.Enum]]:
    return {name: obj for name, obj in vars(c).items()
            if inspect.isclass(obj) and issubclass(obj, enum.Enum) and obj.__module__ == c.__name__}


def type_name(name: str) -> str:
    return "".join(word.title() for word in name.split("_"))


def nullable(field) -> bool:
    annotation = field.annotation
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        return type(None) in typing.get_args(annotation)
    return False


# ---------------------------------------------------------------------------
# Enums: schema.sql, then the sources it follows
# ---------------------------------------------------------------------------
def test_the_parser_reads_the_schema_and_ignores_comments():
    enums = schema_enums()
    assert len(enums) == 19
    assert enums["action_kind"][-1] == "rollback"     # its line has a quoted comment
    assert enums["incident_status"][4] == "testing"


@pytest.mark.parametrize("name", sorted(schema_enums()))
def test_every_schema_enum_has_a_contract_with_the_same_values_in_order(name):
    assert [m.value for m in contract_enums()[type_name(name)]] == schema_enums()[name]


def test_no_contract_enum_is_missing_from_the_schema():
    assert set(contract_enums()) == {type_name(name) for name in schema_enums()}


def test_states_match_the_authoritative_machine():
    import states

    assert {s.value for s in c.IncidentStatus} == {s.value for s in states.IncidentState}
    assert {s.value for s in c.ActionStatus} == {s.value for s in states.ActionState}
    assert len(c.IncidentStatus) == len(c.ActionStatus) == 14


def test_model_enums_match_the_gateway():
    import gateway_contracts as gateway

    assert {s.value for s in c.ModelAttemptStatus} == {s.value for s in gateway.ResultStatus}
    assert {s.value for s in c.ModelStopReason} == {s.value for s in gateway.StopReason}


def test_claims_and_categories_match_layer_9():
    import agent_contracts as agent

    categories = typing.get_args(agent.HypothesisDraft.model_fields["category"].annotation)
    assert {x.value for x in c.HypothesisCategory} == set(categories)
    assert set(c.Claim.model_fields) == set(agent.Claim.model_fields)


# ---------------------------------------------------------------------------
# Naming: views against tables, CloudEvent against its view
# ---------------------------------------------------------------------------
VIEWS = [
    (c.IncidentView, "incidents", {"service", "environment", "current_diagnosis_id",
                                   "open_action_ids", "budget"}),
    (c.BudgetView, "incident_budgets", set()),
    (c.EvidenceRecord, "evidence", set()),
    (c.Hypothesis, "hypotheses", {"evidence"}),
    (c.Diagnosis, "diagnoses", {"hypotheses"}),
    (c.ActionView, "actions", {"target_service", "target_environment", "patch", "latest_test"}),
    (c.PatchView, "patches", set()),
    (c.TestRunView, "test_runs", set()),
    (c.ApprovalDecisionView, "approval_decisions", {"target_service", "target_environment"}),
    (c.ArtifactView, "artifacts", {"content_url"}),
]


@pytest.mark.parametrize("model,table,derived", VIEWS, ids=[v[1] for v in VIEWS])
def test_view_fields_are_schema_columns_with_the_same_nullability(model, table, derived):
    columns = table_columns(table)
    assert set(model.model_fields) - derived <= set(columns)
    for name, field in model.model_fields.items():
        if name not in derived:
            assert nullable(field) == columns[name], name


def test_cloudevent_fields_are_the_columns_of_the_mapping_view():
    select = re.search(r"CREATE VIEW timeline_cloudevents.*?AS\s+SELECT (.*?)\s+FROM",
                       SCHEMA, flags=re.S).group(1)
    names = {re.split(r"\s+AS\s+|\.", part.strip())[-1].strip('"') for part in select.split(",")}
    assert names == set(c.CloudEvent.model_fields)


def test_event_type_pattern_is_the_one_the_schema_checks():
    pattern = c.CloudEvent.model_fields["type"].metadata[0].pattern
    assert f"type ~ '{pattern}'" in SCHEMA


def test_docstring_names_the_canonical_schema_and_the_mapping():
    assert "`schema.sql`" in c.__doc__ and "timeline_cloudevents" in c.__doc__


# ---------------------------------------------------------------------------
# Events: one payload per type in Artifact 7.5
# ---------------------------------------------------------------------------
def artifact_event_types() -> set[str]:
    section = ARTIFACT.split("**Event types (all `.v1`):**")[1].split("Each type's `data`")[0]
    found = set()
    for token in re.findall(r"`([a-z_.]+)`", section):
        if token.startswith("aisre."):
            group = token.split(".")[1]
            found.add(token + ".v1")
        else:
            found.add(f"aisre.{group}.{token}.v1")
    return found


# In schema.sql and contracts.py, and owed to the 7.5 table. Empty this once 7.5 lists them.
NOT_YET_IN_ARTIFACT = {"aisre.evidence.content_removed.v1", "aisre.execution.outcome_resolved.v1"}


def test_every_event_type_in_the_artifact_has_a_payload_model():
    assert len(c.EVENT_DATA) == 41
    assert set(c.EVENT_DATA) == artifact_event_types() | NOT_YET_IN_ARTIFACT


def test_every_payload_model_is_registered_once():
    models = {obj for name, obj in vars(c).items() if name.endswith("Data") and inspect.isclass(obj)}
    assert models == set(c.EVENT_DATA.values())
    assert len(c.EVENT_DATA) == len(models)


def event(type_, data):
    incident_id = uuid4()
    return dict(id=uuid4(), source="/aisre/api/approval-service", type=type_,
                subject=f"incidents/{incident_id}", time=NOW, incidentid=incident_id,
                sequence=1, orgid=uuid4(), actor="user:priya@org",
                correlationid=incident_id, data=data)


def granted(**overrides):
    return {"approval_id": str(uuid4()), "action_id": str(uuid4()), "action_digest": SHA,
            "patch_sha256": SHA, "decided_by": "user:priya@org",
            "valid_until": "2026-11-01T09:57:13Z", **overrides}


def test_cloudevent_validates_data_against_its_type():
    c.CloudEvent(**event("aisre.approval.granted.v1", granted()))
    with pytest.raises(ValidationError):
        c.CloudEvent(**event("aisre.approval.granted.v1", {"action_id": str(uuid4())}))


def test_cloudevent_refuses_an_unknown_type():
    with pytest.raises(ValidationError, match="unknown event type"):
        c.CloudEvent(**event("aisre.made.up.v1", {}))


def test_granted_needs_the_action_digest_and_allows_an_action_without_a_patch():
    with pytest.raises(ValidationError):
        c.ApprovalGrantedData(**{k: v for k, v in granted().items() if k != "action_digest"})
    assert c.ApprovalGrantedData(**granted(patch_sha256=None)).patch_sha256 is None


@pytest.mark.parametrize("result", ["verified", "failed", "inconclusive"])
def test_verification_completed_uses_the_layer_11_results(result):
    data = c.VerificationCompletedData(verification_id=uuid4(), action_id=uuid4(),
                                       result=result, hold_seconds=600)
    assert data.result.value == result


@pytest.mark.parametrize("result", ["recovered", "not_recovered"])
def test_verification_completed_refuses_the_old_results(result):
    with pytest.raises(ValidationError):
        c.VerificationCompletedData(verification_id=uuid4(), action_id=uuid4(),
                                    result=result, hold_seconds=600)


def test_execution_completed_cannot_report_started():
    with pytest.raises(ValidationError):
        c.ExecutionCompletedData(attempt_id=uuid4(), action_id=uuid4(), execution_key="k",
                                 attempt_no=1, outcome="started", external_ref=None)


def test_a_refused_execution_has_no_attempt():
    assert "attempt_id" not in c.ExecutionRefusedData.model_fields


def test_a_refused_approval_has_no_approval_id():
    assert "approval_id" not in c.ApprovalRefusedData.model_fields


def test_content_removed_payload_is_what_purge_document_returns():
    data = {"doc_id": "rb-1", "reason": "legal request"}
    c.CloudEvent(**event("aisre.evidence.content_removed.v1", data))
    with pytest.raises(ValidationError):
        c.CloudEvent(**event("aisre.evidence.content_removed.v1", {"doc_id": "rb-1"}))


def outcome_resolved(**overrides):
    return dict(attempt_id=uuid4(), action_id=uuid4(), execution_key="k", outcome="not_applied",
                observed_revision="r1", reason="Checked the target", **overrides)


def test_outcome_resolved_is_an_execution_event_so_the_dispatcher_may_write_it():
    assert c.ExecutionOutcomeResolvedData(**outcome_resolved()).outcome.value == "not_applied"
    assert "'aisre.execution.%'" in SCHEMA
    assert c.EVENT_DATA["aisre.execution.outcome_resolved.v1"] is c.ExecutionOutcomeResolvedData


@pytest.mark.parametrize("outcome", ["started", "unknown", "failed"])
def test_outcome_resolved_is_applied_or_not_applied(outcome):
    fields = outcome_resolved()
    with pytest.raises(ValidationError):
        c.ExecutionOutcomeResolvedData(**{**fields, "outcome": outcome})


def test_an_outcome_resolution_has_columns_to_land_in():
    columns = table_columns("execution_attempts")
    assert {"outcome", "observed_revision", "resolved_by", "resolution_reason"} <= set(columns)
    assert set(c.OutcomeResolutionCreate.model_fields) == {"outcome", "observed_revision", "reason"}


def test_notification_failed_payload():
    data = c.NotificationFailedData(event_id=uuid4(), channel="slack", error="503")
    assert c.EVENT_DATA["aisre.notification.failed.v1"] is type(data)


# ---------------------------------------------------------------------------
# Domain records
# ---------------------------------------------------------------------------
def test_action_view_requires_the_expected_revision():
    assert not nullable(c.ActionView.model_fields["expected_after_revision"])
    assert not table_columns("actions")["expected_after_revision"]


def evidence(**overrides):
    fields = dict(evidence_id=uuid4(), incident_id=uuid4(), kind="log", source_system="loki",
                  tool="logs.search", collected_by="evidence-collector", query=None,
                  window_start=None, window_end=None, collected_at=NOW, source_as_of=None,
                  source_link=None, summary="500s rising", attributes={}, artifact_id=None,
                  content_sha256=SHA, is_partial=False, flagged_injection=False,
                  gap_reason=None, strength_tier=3)
    return c.EvidenceRecord(**{**fields, **overrides})


def test_evidence_gap_reason_goes_with_gap_kind():
    assert evidence(kind="gap", gap_reason="source down", strength_tier=None).gap_reason
    with pytest.raises(ValidationError):
        evidence(kind="gap")
    with pytest.raises(ValidationError):
        evidence(gap_reason="not a gap")


@pytest.mark.parametrize("tier", [0, 9])
def test_evidence_strength_tier_is_1_to_8(tier):
    with pytest.raises(ValidationError):
        evidence(strength_tier=tier)


def test_evidence_window_cannot_run_backwards():
    with pytest.raises(ValidationError):
        evidence(window_start=NOW, window_end=NOW - timedelta(seconds=1))


def test_evidence_shows_content_removal_as_a_pair():
    assert evidence(content_removed_at=NOW, content_removed_reason="legal request").content_removed_at
    with pytest.raises(ValidationError):
        evidence(content_removed_at=NOW)
    with pytest.raises(ValidationError):
        evidence(content_removed_reason="legal request")


def hypothesis(**overrides):
    fields = dict(hypothesis_id=uuid4(), ordinal=1, statement="Bad config", category="deployment_config",
                  claims=[{"text": "Errors follow the change", "evidence_ids": [uuid4()]}],
                  confirm_test="Revert it", refute_test="Errors predate it", status="open",
                  confidence="medium", status_reason=None, evidence=[])
    return c.Hypothesis(**{**fields, **overrides})


def test_hypothesis_carries_category_and_claims():
    assert hypothesis().category is c.HypothesisCategory.deployment_config
    with pytest.raises(ValidationError):
        hypothesis(category="network")
    with pytest.raises(ValidationError):
        hypothesis(claims=[{"text": "No evidence", "evidence_ids": []}])


def test_hypothesis_ordinal_counts_within_the_incident_not_the_diagnosis():
    assert hypothesis(ordinal=6).ordinal == 6


def diagnosis(**overrides):
    first, second = hypothesis(), hypothesis(ordinal=2)
    fields = dict(diagnosis_id=uuid4(), incident_id=uuid4(), revision=1, outcome="established",
                  leading_hypothesis_id=first.hypothesis_id, category="deployment_config",
                  confidence="medium", summary="Config change broke checkout", claims=[],
                  hypotheses=[first, second], ruled_out=["Capacity is normal"],
                  missing_evidence=[], stop_reason=None, reviewed=True, review_notes=None,
                  produced_by="diagnosis-engine")
    return c.Diagnosis(**{**fields, **overrides})


def test_diagnosis_carries_the_stored_fields():
    d = diagnosis()
    assert (d.reviewed, d.produced_by, d.ruled_out) == (True, "diagnosis-engine", ["Capacity is normal"])


def test_established_diagnosis_needs_a_leading_hypothesis_it_lists():
    with pytest.raises(ValidationError):
        diagnosis(leading_hypothesis_id=None)
    with pytest.raises(ValidationError):
        diagnosis(leading_hypothesis_id=uuid4())
    with pytest.raises(ValidationError):
        diagnosis(outcome="inconclusive")


def test_low_confidence_is_never_established():
    with pytest.raises(ValidationError):
        diagnosis(confidence="low")
    assert diagnosis(outcome="inconclusive", leading_hypothesis_id=None, confidence="low")


def action(**overrides):
    fields = dict(action_id=uuid4(), incident_id=uuid4(), diagnosis_id=uuid4(), parent_action_id=None,
                  kind="config_revert", target_service="checkout", target_environment="production",
                  target_deployment="checkout-prod-eu1", base_revision="abc123",
                  expected_after_revision="def456", description="Revert",
                  expected_effect="Errors fall", risk="Low", undo_plan="Re-apply",
                  recovery_criteria=[{"metric": "http_5xx_rate", "comparison": "<=", "threshold": 0.01}],
                  hold_seconds=600, action_digest=SHA, status="awaiting_approval", version=2,
                  patch=None, latest_test=None)
    return c.ActionView(**{**fields, **overrides})


def test_action_status_is_typed():
    assert action().status is c.ActionStatus.awaiting_approval
    with pytest.raises(ValidationError):
        action(status="tested")


def test_parent_action_goes_with_rollback_kind():
    assert action(kind="rollback", parent_action_id=uuid4()).kind is c.ActionKind.rollback
    with pytest.raises(ValidationError):
        action(kind="rollback")
    with pytest.raises(ValidationError):
        action(parent_action_id=uuid4())


def test_action_digest_is_required_once_testing_ends():
    assert action(status="testing", action_digest=None).action_digest is None
    with pytest.raises(ValidationError):
        action(action_digest=None)


def test_action_keeps_slugs_not_ids():
    assert action().target_service == "checkout"
    with pytest.raises(ValidationError):
        action(target_service="Not A Slug")


def patch(**overrides):
    fields = dict(patch_id=uuid4(), base_revision="abc123", patch_sha256=SHA, files_changed=1,
                  lines_added=1, lines_removed=1, diff_artifact_id=uuid4(), review_ref=None)
    return c.PatchView(**{**fields, **overrides})


def test_patch_fingerprint_is_patch_sha256():
    assert patch().patch_sha256 == SHA
    with pytest.raises(ValidationError):
        patch(diff_sha256=SHA)


def test_run(outcome):
    return c.TestRunView(test_run_id=uuid4(), patch_sha256=SHA, reproduced=True, outcome=outcome,
                         output_artifact_id=None, started_at=NOW, finished_at=NOW)


test_run.__test__ = False


@pytest.mark.parametrize("outcome", ["passed", "failed", "not_reproduced", "inconclusive", "crashed", "timeout"])
def test_test_run_accepts_the_six_outcomes(outcome):
    assert test_run(outcome).outcome.value == outcome


def test_test_run_refuses_the_old_error_outcome():
    with pytest.raises(ValidationError):
        test_run("error")


def artifact(kind):
    return c.ArtifactView(artifact_id=uuid4(), incident_id=uuid4(), kind=kind, media_type="text/plain",
                          size_bytes=1, content_sha256=SHA, redacted=True, created_at=NOW,
                          content_url="/v1/artifacts/1/content")


def test_artifact_kind_is_typed():
    assert artifact("patch_diff").kind is c.ArtifactKind.patch_diff
    with pytest.raises(ValidationError):
        artifact("screenshot")


def test_model_output_contracts_moved_to_layer_9():
    assert not hasattr(c, "ModelDiagnosisOutput")
    assert not hasattr(c, "ModelHypothesis")


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------
def test_approval_names_the_action_digest():
    assert c.ApprovalCreate(action_digest=SHA).rollback_action_digest is None
    assert c.ApprovalCreate(action_digest=SHA, rollback_action_digest=SHA, reason="Looks right")
    with pytest.raises(ValidationError):
        c.ApprovalCreate()


@pytest.mark.parametrize("extra", ["patch_sha256", "expected_action_version"])
def test_approval_body_refuses_the_old_fields(extra):
    with pytest.raises(ValidationError):
        c.ApprovalCreate(action_digest=SHA, **{extra: SHA})


def test_rejection_names_the_digest_and_needs_a_reason():
    assert c.RejectionCreate(action_digest=SHA, reason="The patch touches payments")
    with pytest.raises(ValidationError):
        c.RejectionCreate(action_digest=SHA)
    with pytest.raises(ValidationError):
        c.RejectionCreate(patch_sha256=SHA, reason="The patch touches payments")


def test_revocation_names_the_approval_the_digest_and_a_reason():
    assert c.RevocationCreate(approval_id=uuid4(), action_digest=SHA, reason="Wrong deployment")
    with pytest.raises(ValidationError):
        c.RevocationCreate(action_digest=SHA, reason="Wrong deployment")


def test_outcome_resolution_settles_to_applied_or_not_applied_only():
    body = dict(observed_revision="abc123", reason="Checked the live revision")
    assert c.OutcomeResolutionCreate(outcome="not_applied", **body)
    for outcome in ("unknown", "started", "failed"):
        with pytest.raises(ValidationError):
            c.OutcomeResolutionCreate(outcome=outcome, **body)
    with pytest.raises(ValidationError):
        c.OutcomeResolutionCreate(outcome="applied", reason="Checked the live revision")


def decision(**overrides):
    fields = dict(approval_id=uuid4(), action_id=uuid4(), decision="approved", decided_by="user:priya",
                  decided_by_role="approver", action_digest=SHA, patch_sha256=None,
                  target_service="checkout", target_environment="production", decided_at=NOW,
                  valid_until=NOW + timedelta(minutes=15), reason=None, signing_key_id="k1",
                  revokes_approval_id=None, conditional=False, conditional_on_approval_id=None)
    return c.ApprovalDecisionView(**{**fields, **overrides})


def test_revoked_decision_points_at_the_approval_it_withdraws():
    revoked = decision(decision="revoked", valid_until=None, reason="Wrong target",
                       revokes_approval_id=uuid4())
    assert revoked.decision is c.ApprovalDecision.revoked
    with pytest.raises(ValidationError):
        decision(decision="revoked", valid_until=None, reason="Wrong target")
    with pytest.raises(ValidationError):
        decision(revokes_approval_id=uuid4())


def test_valid_until_and_reason_follow_the_decision():
    with pytest.raises(ValidationError):
        decision(valid_until=None)
    with pytest.raises(ValidationError):
        decision(signing_key_id=None)
    with pytest.raises(ValidationError):
        decision(decision="rejected", valid_until=None)


def test_conditional_approval_names_the_fix_approval():
    assert decision(conditional=True, conditional_on_approval_id=uuid4()).conditional
    with pytest.raises(ValidationError):
        decision(conditional=True)
    with pytest.raises(ValidationError):
        decision(conditional_on_approval_id=uuid4())
    with pytest.raises(ValidationError):
        decision(decision="rejected", valid_until=None, reason="No", conditional=True,
                 conditional_on_approval_id=uuid4())


# ---------------------------------------------------------------------------
# Other bodies and pages
# ---------------------------------------------------------------------------
def test_named_bodies():
    assert c.SeverityConfirmationCreate(severity="sev2").severity is c.Severity.sev2
    assert c.ClosureCreate().note is None
    assert c.ReinvestigationCreate(reason="The provider is back").reason
    assert c.NoteCreate(note="Paged the database team").note
    with pytest.raises(ValidationError):
        c.SeverityConfirmationCreate(severity="sev5")
    with pytest.raises(ValidationError):
        c.ReinvestigationCreate()
    with pytest.raises(ValidationError):
        c.NoteCreate(note="")


def test_no_body_carries_a_version():
    bodies = [c.ApprovalCreate, c.RejectionCreate, c.RevocationCreate, c.OutcomeResolutionCreate,
              c.SeverityConfirmationCreate, c.ClosureCreate, c.ReinvestigationCreate, c.NoteCreate]
    assert not [b for b in bodies for name in b.model_fields if "version" in name]


def test_page_is_typed_and_ends_with_a_null_cursor():
    page = c.Page[c.ApprovalDecisionView](items=[decision()], next_cursor="abc")
    assert page.items[0].decision is c.ApprovalDecision.approved
    assert c.Page[int](items=[]).next_cursor is None
    with pytest.raises(ValidationError):
        c.Page[c.ApprovalDecisionView](items=[{"not": "a decision"}])


def test_timeline_page_continues_from_a_sequence():
    page = c.Page[c.CloudEvent](items=[event("aisre.approval.granted.v1", granted())], next_after_seq=1)
    assert page.next_after_seq == 1
