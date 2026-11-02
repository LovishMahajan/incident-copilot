"""Tests for the agent: coordinator policy, tool gate, contracts and validation.
Run: uv run pytest -q tests/unit/test_agent.py
"""

from __future__ import annotations

import ast
import copy
import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from aisre.agent import coordinator
from aisre.agent.agent_contracts import (Claim, CollectIn, Confidence, CriticIssue, CriticVerdict, DiagnosisDraft, DiagnosisResult,
                             Evidence, EvidenceKind, EvidenceRequest, HypothesesOut, HypothesisDraft,
                             IssueKind, KnowledgeIn, KnowledgeOut, PatchFileEdit, PatchIn, PatchProposal,
                             ReviewResult, SkillContext, SkillFailure, SkillResult, SkillUsage)
from aisre.agent.coordinator import (AgentState, Limits, Step, StopKind, after_collect, after_critique, after_diagnosis,
                         after_failure, after_hypotheses, after_knowledge, apply_result, next_step)
from aisre.agent.tool_registry import (REGISTRY, SKILL_TOOLS, PatchDraftParams, ToolGate, ToolRefused, agent_tools, search_knowledge,
                           tool_schema_for_model)
from aisre.agent.validation import (ObservationFacts, UnsafeOutput, UnsupportedReference, assign_tier, check_citations,
                        check_output, code_checks, combine, fingerprint, mask_secrets,
                        missing_required_sources, recheck_fingerprints, score_confidence)

T0 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
INC = uuid.uuid4()


def ev(kind, incident=INC, summary="x", tier=None, system="sys", tool="get_logs", flagged=False):
    return Evidence(evidence_id=uuid.uuid4(), incident_id=incident, kind=kind, source_system=system,
                    tool=tool, query="q", window_start=T0 - timedelta(hours=1), window_end=T0,
                    collected_at=T0, source_as_of=T0,
                    source_link=None if kind == EvidenceKind.gap else "https://source/1", summary=summary,
                    content_sha256=hashlib.sha256(summary.encode()).hexdigest(), strength_tier=tier,
                    flagged_injection=flagged, gap_reason="timeout" if kind == EvidenceKind.gap else None)


def store(*records):
    return {e.evidence_id: e for e in records}


def hyp(category, ids, against=(), stmt=None):
    return HypothesisDraft(statement=stmt or f"{category} cause", category=category,
                           claims=[Claim(text="observed", evidence_ids=list(ids))],
                           contradicting_evidence_ids=list(against), confirm_test="c", refute_test="r")


# ---------------------------------------------------------------- coordinator
def run(script, limits=Limits(), clock_step=timedelta(seconds=30), evidence_only=False):
    """Drive the coordinator with scripted skill results; returns the final state."""
    s = AgentState(started_at=T0, limits=limits, evidence_only=evidence_only)
    now = T0
    for _ in range(50):
        step = next_step(s, now)
        now += clock_step
        if step == Step.COLLECT_BASELINE:
            after_collect(s, [f"e{i}" for i in range(6)], gaps=0, tool_calls=6, targeted=False)
        elif step == Step.RETRIEVE_KNOWLEDGE:
            after_knowledge(s, ["k1"], gaps=0, tool_calls=2)
        elif step == Step.HYPOTHESISE:
            after_hypotheses(s, 1, script.get("hyp_requests", []))
        elif step == Step.COLLECT_TARGETED:
            new = script["targeted"].pop(0) if script.get("targeted") else []
            after_collect(s, new, gaps=script.get("gaps", 0), tool_calls=len(s.pending_requests), targeted=True)
        elif step == Step.DIAGNOSE:
            fp, ratio = script["diagnoses"].pop(0)
            after_diagnosis(s, fp, ratio, 1)
        elif step == Step.CRITIQUE:
            verdict, reqs = script["verdicts"].pop(0)
            after_critique(s, verdict, reqs, 1, script.get("confidence", "high"))
        else:
            return s, step
    raise AssertionError("loop did not terminate")


def test_reference_case_accepted_first_time():
    s, step = run({"diagnoses": [("fp1", 1.0)], "verdicts": [("accept", [])]})
    assert step == Step.FINISH_ESTABLISHED and s.model_calls == 3 and s.revisions == 0
    assert s.stop_kind is None


def test_revise_with_evidence_request_then_accept():
    s, step = run({"diagnoses": [("fp1", 0.6), ("fp2", 1.0)],
                   "verdicts": [("revise", [{"tool": "get_metric"}]), ("accept", [])],
                   "targeted": [["e-new"]]})
    assert step == Step.FINISH_ESTABLISHED and s.revisions == 1 and s.targeted_rounds == 1


def test_identical_revision_stops():
    s, step = run({"diagnoses": [("fp1", 0.6), ("fp1", 0.6)],
                   "verdicts": [("revise", [{"tool": "get_logs"}]), ("revise", [])],
                   "targeted": [["e-new"]]})
    assert step == Step.FINISH_INCONCLUSIVE and "same diagnosis" in s.stop_reason


def test_revision_limit():
    s, step = run({"diagnoses": [("a", 0.2), ("b", 0.4), ("c", 0.6)],
                   "verdicts": [("revise", [{"t": 1}]), ("revise", [{"t": 2}]), ("revise", [{"t": 3}])],
                   "targeted": [["n1"], ["n2"], ["n3"]]})
    assert step == Step.FINISH_INCONCLUSIVE and "revision limit" in s.stop_reason and s.diagnoses == 3


def test_requested_evidence_adds_nothing_stops():
    s, step = run({"diagnoses": [("a", 0.5), ("b", 0.5)],
                   "verdicts": [("revise", [{"t": 1}]), ("revise", [])],
                   "targeted": [[]]})
    assert step == Step.FINISH_INCONCLUSIVE and "added nothing new" in s.stop_reason and s.diagnoses == 1


def test_critic_invalid_twice_fails_closed():
    s, step = run({"diagnoses": [("a", 1.0)], "verdicts": [(None, []), (None, [])]})
    assert step == Step.FINISH_INCONCLUSIVE and "failing closed" in s.stop_reason


def test_escalate_stops():
    s, step = run({"diagnoses": [("a", 0.9)], "verdicts": [("escalate", [])]})
    assert step == Step.FINISH_INCONCLUSIVE and s.stop_reason == "critic escalated"


def test_time_limit_stops():
    s, step = run({"diagnoses": [("a", 0.5)] * 5, "verdicts": [("revise", [{"t": 1}])] * 5,
                   "targeted": [["n"]] * 5}, clock_step=timedelta(minutes=6))
    assert step == Step.FINISH_INCONCLUSIVE and "time limit" in s.stop_reason


def test_model_budget_stops():
    s, step = run({"diagnoses": [("a", 1.0)], "verdicts": [("accept", [])]},
                  limits=Limits(max_model_calls=2))
    assert step == Step.FINISH_INCONCLUSIVE and "model call budget" in s.stop_reason


def test_knowledge_runs_between_baseline_and_hypotheses():
    s, _ = run({"diagnoses": [("a", 1.0)], "verdicts": [("accept", [])]})
    order = [d.split(":")[0] for d in s.decisions]
    assert order[:3] == ["collect_baseline", "retrieve_knowledge", "hypothesise"]
    assert s.knowledge_done and "k1" in s.evidence_ids and s.tool_calls == 8


def test_zero_yield_round_stops_before_any_diagnosis():
    s, step = run({"hyp_requests": [[{"tool": "get_metric"}]], "targeted": [[]]})
    assert step == Step.FINISH_INCONCLUSIVE and "added nothing new" in s.stop_reason and s.diagnoses == 0


def test_a_round_of_only_gaps_adds_nothing():
    s, step = run({"hyp_requests": [[{"tool": "get_traces"}]], "targeted": [["gap-1"]], "gaps": 1})
    assert step == Step.FINISH_INCONCLUSIVE and "added nothing new" in s.stop_reason


def test_revision_checks_run_before_pending_requests():
    s, step = run({"diagnoses": [("a", 0.2), ("b", 0.4)],
                   "verdicts": [("revise", [{"t": 1}]), ("revise", [{"t": 2}])],
                   "targeted": [["n1"], ["n2"]]}, limits=Limits(max_revisions=1))
    assert step == Step.FINISH_INCONCLUSIVE and "revision limit" in s.stop_reason
    assert s.targeted_rounds == 1                            # no wasted round


def test_support_ratio_stop_with_no_new_evidence():
    s, step = run({"diagnoses": [("a", 0.5), ("b", 0.5)], "verdicts": [("revise", []), ("revise", [])]})
    assert step == Step.FINISH_INCONCLUSIVE and "did not improve" in s.stop_reason


def test_flat_support_is_allowed_when_evidence_arrived_since_the_previous_diagnosis():
    s, step = run({"diagnoses": [("a", 0.5), ("b", 0.5), ("c", 1.0)],
                   "verdicts": [("revise", [{"t": 1}]), ("revise", []), ("accept", [])],
                   "targeted": [["n1"]]})
    assert step == Step.FINISH_ESTABLISHED and s.diagnoses == 3


def test_diagnoser_inconclusive_skips_critic():
    s = AgentState(started_at=T0, baseline_done=True, knowledge_done=True, hypotheses_ready=True)
    after_diagnosis(s, "a", 0.5, 1, outcome="inconclusive")
    assert next_step(s, T0) == Step.FINISH_INCONCLUSIVE and "could not establish" in s.stop_reason


@pytest.mark.parametrize("field,value,reason", [
    ("input_tokens", 200_000, "token budget"), ("output_tokens", 20_000, "token budget"),
    ("cost_micros", 2_000_000, "incident spend"), ("daily_spend_exhausted", True, "daily spend")])
def test_token_and_spend_stops_go_through_inconclusive(field, value, reason):
    s = AgentState(started_at=T0, **{field: value})
    assert next_step(s, T0) == Step.FINISH_INCONCLUSIVE
    assert reason in s.stop_reason and s.stop_kind == StopKind.INCONCLUSIVE


def test_critic_failure_limit_reads_limits():
    s, step = run({"diagnoses": [("a", 1.0)], "verdicts": [(None, [])]}, limits=Limits(max_critic_failures=1))
    assert step == Step.FINISH_INCONCLUSIVE and "failing closed" in s.stop_reason


def test_limits_hold_the_decided_values():
    L = Limits()
    assert (L.max_input_tokens, L.max_output_tokens, L.max_cost_micros) == (200_000, 20_000, 2_000_000)
    assert (L.max_critic_failures, L.max_attempts_per_step, L.max_tool_calls) == (2, 3, 20)


def test_evidence_only_collects_then_stops_through_inconclusive():
    s, step = run({}, evidence_only=True)
    assert step == Step.FINISH_INCONCLUSIVE and s.stop_kind == StopKind.INCONCLUSIVE
    assert s.knowledge_done and s.model_calls == 0 and "evidence-only" in s.stop_reason


def test_daily_flag_does_not_cut_an_evidence_only_run_short():
    s = AgentState(started_at=T0, evidence_only=True, daily_spend_exhausted=True)
    assert next_step(s, T0) == Step.COLLECT_BASELINE


def test_running_investigation_stops_after_the_step_that_reports_the_daily_limit():
    s = AgentState(started_at=T0)
    apply_result(s, next_step(s, T0), SkillResult(output=Out(["e1"], 0), usage=SkillUsage(daily_spend_exhausted=True)))
    assert s.baseline_done and next_step(s, T0) == Step.FINISH_INCONCLUSIVE and "daily spend" in s.stop_reason


@pytest.mark.parametrize("code", ["invalid_output", "provider_unavailable", "budget_refused",
                                  "deadline_passed", "store_failed"])
def test_skill_failure_stops_through_inconclusive(code):
    s = AgentState(started_at=T0)
    after_failure(s, "diagnose", code, "details")
    assert next_step(s, T0) == Step.FINISH_INCONCLUSIVE
    assert code in s.stop_reason and "details" in s.stop_reason and s.stop_kind == StopKind.INCONCLUSIVE


def test_security_stop_goes_direct_and_outranks_everything():
    s = AgentState(started_at=T0, last_verdict="accept", confidence="high")
    after_failure(s, "collect_evidence", "security_risk", "credentials in logs")
    assert next_step(s, T0 + timedelta(hours=1)) == Step.FINISH_INCONCLUSIVE
    assert s.stop_kind == StopKind.SECURITY and "security_risk" in s.stop_reason


@pytest.mark.parametrize("confidence", ["low", None])
def test_accept_without_medium_confidence_is_inconclusive(confidence):
    s, step = run({"diagnoses": [("a", 1.0)], "verdicts": [("accept", [])], "confidence": confidence})
    assert step == Step.FINISH_INCONCLUSIVE and "final confidence is low" in s.stop_reason
    assert s.stop_kind == StopKind.INCONCLUSIVE


def test_hypothesis_requests_taken_in_turn_and_the_rest_logged():
    s = AgentState(started_at=T0)
    a, b, c = [{"tool": "a1"}, {"tool": "a2"}, {"tool": "a3"}], [{"tool": "b1"}], [{"tool": "c1"}, {"tool": "c2"}]
    after_hypotheses(s, 1, [a, b, c])
    assert [r["tool"] for r in s.pending_requests] == ["a1", "b1", "c1"]
    assert sum("not run" in d for d in s.decisions) == 3


def test_targeted_round_not_run_when_limits_are_used():
    s, step = run({"hyp_requests": [[{"tool": "get_logs"}]], "diagnoses": [("a", 1.0)], "verdicts": [("accept", [])]},
                  limits=Limits(max_tool_calls=8))
    assert step == Step.FINISH_ESTABLISHED and s.targeted_rounds == 0


@dataclass
class Out:                                   # stands in for activities.EvidenceOut (Layer 8)
    evidence_ids: list[str]
    gap_count: int


def test_apply_result_drives_a_whole_loop():
    e1 = uuid.uuid4()
    request = EvidenceRequest(tool="get_metric", reason="version split", params={"metric": "error_rate_by_version"})
    lead = HypothesisDraft(statement="config", category="deployment_config", confirm_test="c", refute_test="r",
                           claims=[Claim(text="t", evidence_ids=[e1])], evidence_needed=[request])
    draft = DiagnosisDraft(outcome="established", leading_index=0, summary="s", ruled_out=["r"],
                           proposed_action="config_revert", hypotheses=[lead, hyp("dependency", [e1])])
    usage = SkillUsage(model_calls=2, input_tokens=100, output_tokens=10, cost_micros=7)
    results = [
        SkillResult[Out](output=Out(["e1", "e2"], 1), usage=SkillUsage(tool_calls=8)),
        SkillResult[KnowledgeOut](output=KnowledgeOut(evidence_ids=["k1"], gap_count=0, index_revision="r1"),
                                  usage=SkillUsage(tool_calls=2)),
        SkillResult[HypothesesOut](output=HypothesesOut(hypotheses=[lead, hyp("dependency", [e1])]), usage=usage),
        SkillResult[Out](output=Out(["e3"], 0), usage=SkillUsage(tool_calls=1)),
        SkillResult[DiagnosisResult](output=DiagnosisResult(diagnosis_id=uuid.uuid4(), draft=draft, fingerprint="a" * 64,
                                                            supported_ratio=1.0), usage=usage),
        SkillResult[ReviewResult](output=ReviewResult(), usage=usage),       # invalid critic output
        SkillResult[ReviewResult](output=ReviewResult(verdict=CriticVerdict(verdict="accept", confidence_cap=Confidence.high),
                                                      confidence=Confidence.medium), usage=usage),
    ]
    s, steps = AgentState(started_at=T0), []
    for r in results:
        steps.append(next_step(s, T0))
        apply_result(s, steps[-1], r)
    assert steps == [Step.COLLECT_BASELINE, Step.RETRIEVE_KNOWLEDGE, Step.HYPOTHESISE, Step.COLLECT_TARGETED,
                     Step.DIAGNOSE, Step.CRITIQUE, Step.CRITIQUE]
    assert next_step(s, T0) == Step.FINISH_ESTABLISHED
    assert (s.model_calls, s.tool_calls, s.input_tokens, s.output_tokens, s.cost_micros) == (8, 11, 400, 40, 28)
    assert s.critic_failures == 1 and s.confidence == "medium" and s.gap_count == 1


def test_apply_result_records_a_typed_failure_and_its_usage():
    s = AgentState(started_at=T0)
    failed = SkillResult[HypothesesOut](failure=SkillFailure(skill="hypothesise", code="invalid_output", detail="3 attempts"),
                                        usage=SkillUsage(model_calls=3, cost_micros=9))
    apply_result(s, Step.HYPOTHESISE, failed)
    assert s.model_calls == 3 and s.cost_micros == 9 and s.failure["code"] == "invalid_output"
    assert not s.hypotheses_ready and next_step(s, T0) == Step.FINISH_INCONCLUSIVE


def test_coordinator_is_pure():
    tree = ast.parse(open(coordinator.__file__).read())
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Import)]
    assert {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} == {
        "__future__", "dataclasses", "datetime", "enum", "typing"}
    s = AgentState(started_at=T0)
    a, b = copy.deepcopy(s), copy.deepcopy(s)
    assert next_step(a, T0) == next_step(b, T0) and a.decisions == b.decisions


# ---------------------------------------------------------------- tool gate
SKILL = "collect_evidence"


def gate(**kw):
    return ToolGate(incident_id=str(INC), service="checkout", incident_start=T0, now=T0 + timedelta(minutes=5), **kw)


def test_write_tools_never_offered_or_callable():
    assert all(t.kind.value != "write" for skill in SKILL_TOOLS for t in agent_tools(skill))
    with pytest.raises(ToolRefused) as e:
        gate().check("apply_change", {"service": "checkout", "action_kind": "config_revert", "target_ref": "x"}, "draft_patch")
    assert e.value.code == "write_forbidden"


def test_unknown_tool_refused():
    with pytest.raises(ToolRefused) as e:
        gate().check("run_shell", {"cmd": "rm -rf /"}, SKILL)
    assert e.value.code == "unknown_tool"


def test_out_of_scope_service_refused():
    with pytest.raises(ToolRefused) as e:
        gate().check("get_logs", {"service": "payments"}, SKILL)
    assert e.value.code == "out_of_scope"


def test_month_of_logs_refused_and_default_window_applied():
    g = gate()
    with pytest.raises(ToolRefused) as e:
        g.check("get_logs", {"service": "checkout", "start": (T0 - timedelta(days=30)).isoformat(),
                             "end": T0.isoformat()}, SKILL)
    assert e.value.code == "window_too_large"
    _, p, _ = g.check("get_logs", {"service": "checkout"}, SKILL)
    assert p.end - p.start == timedelta(minutes=65)


def test_invalid_range_and_unbounded_limit_refused():
    g = gate()
    with pytest.raises(ToolRefused) as e:
        g.check("get_logs", {"service": "checkout", "start": T0.isoformat(), "end": (T0 - timedelta(hours=1)).isoformat()}, SKILL)
    assert e.value.code == "invalid_params"
    with pytest.raises(ToolRefused):
        g.check("get_logs", {"service": "checkout", "limit": 100000}, SKILL)


def test_repeated_identical_call_limited():
    g = gate()
    params = {"service": "checkout", "metric": "http_5xx_rate", "start": (T0 - timedelta(hours=1)).isoformat(),
              "end": T0.isoformat()}
    for _ in range(2):
        tool, p, h = g.check("get_metric", params, SKILL)
        g.record("get_metric", h, new_evidence=0)
    with pytest.raises(ToolRefused) as e:
        g.check("get_metric", params, SKILL)
    assert e.value.code == "repeated_call"


def test_per_tool_budget():
    g = gate()
    for i in range(4):
        _, _, h = g.check("get_changes", {"service": "checkout", "start": (T0 - timedelta(hours=i + 1)).isoformat(),
                                          "end": T0.isoformat()}, SKILL)
        g.record("get_changes", h, 1)
    with pytest.raises(ToolRefused) as e:
        g.check("get_changes", {"service": "checkout"}, SKILL)
    assert e.value.code == "tool_budget"


def test_total_call_budget_reads_limits():
    assert gate().max_total_calls == Limits().max_tool_calls
    g = gate(max_total_calls=1)
    _, _, h = g.check("get_logs", {"service": "checkout"}, SKILL)
    g.record("get_logs", h, 1)
    with pytest.raises(ToolRefused) as e:
        g.check("get_logs", {"service": "checkout", "level": "error"}, SKILL)
    assert e.value.code == "call_budget"


@pytest.mark.parametrize("skill,tool", [("diagnose", "get_logs"), ("retrieve_knowledge", "get_logs"),
                                        ("critique", "draft_patch"), ("hypothesise", "draft_patch"),
                                        ("collect_evidence", "draft_patch"), ("draft_patch", "get_logs"),
                                        ("no_such_skill", "get_logs")])
def test_tool_outside_the_skills_list_refused(skill, tool):
    with pytest.raises(ToolRefused) as e:
        gate().check(tool, {"service": "checkout"}, skill)
    assert e.value.code == "tool_not_allowed"


def test_each_skill_is_shown_only_its_tools():
    assert agent_tools("diagnose") == [] and tool_schema_for_model("diagnose") == []
    assert {t.name for t in agent_tools("draft_patch")} == {"draft_patch"}
    assert {t.name for t in agent_tools("retrieve_knowledge")} == {"search_runbooks", "search_past_incidents"}
    assert {t["name"] for t in tool_schema_for_model("critique")} == SKILL_TOOLS["critique"]
    assert all(REGISTRY[n].kind.value == "read" for n in SKILL_TOOLS["critique"])


def test_added_tools_validate_their_parameters():
    g = gate()
    assert g.check("get_alert", {"service": "checkout"}, SKILL)[1].alert_id is None
    assert g.check("get_error_groups", {"service": "checkout", "release": "v2"}, SKILL)[1].release == "v2"
    assert g.check("search_runbooks", {"service": "checkout", "query": "5xx"}, "retrieve_knowledge")[1].k == 5
    with pytest.raises(ToolRefused) as e:
        g.check("search_past_incidents", {"service": "checkout", "query": "x" * 201}, "retrieve_knowledge")
    assert e.value.code == "invalid_params"
    with pytest.raises(ToolRefused) as e:
        g.check("search_runbooks", {"service": "payments", "query": "x"}, "retrieve_knowledge")
    assert e.value.code == "out_of_scope"


def test_registry_values_from_the_specification():
    assert REGISTRY["get_dependency_health"].credential_scope == "catalog:read:{service}"
    assert REGISTRY["draft_patch"].max_calls_per_incident == 3


def test_point_in_time_is_checked_against_the_window_rules():
    g = gate()
    now = T0 + timedelta(minutes=5)
    params = lambda at: {"service": "checkout", "at": at.isoformat()}
    g.check("get_dependency_health", params(now + timedelta(seconds=59)), SKILL)      # clock allowance
    with pytest.raises(ToolRefused) as e:
        g.check("get_dependency_health", params(now + timedelta(minutes=2)), SKILL)
    assert e.value.code == "window_in_future"
    with pytest.raises(ToolRefused) as e:
        g.check("get_dependency_health", params(T0 - timedelta(hours=25)), SKILL)
    assert e.value.code == "window_too_old"


def test_time_without_a_zone_is_refused_not_a_crash():
    with pytest.raises(ToolRefused) as e:
        gate().check("get_dependency_health", {"service": "checkout", "at": "2026-11-01T09:00:00"}, SKILL)
    assert e.value.code == "invalid_params"


def test_cap_output_truncates_and_marks_partial():
    g = gate()
    limit = REGISTRY["get_alert"].max_output_bytes
    assert g.cap_output("get_alert", b"x" * limit) == (b"x" * limit, False)
    kept, partial = g.cap_output("get_alert", b"x" * (limit + 1))
    assert len(kept) == limit and partial is True


# ---------------------------------------------------------------- contracts and validation
def test_hypotheses_must_include_a_non_deployment_cause():
    e1 = uuid.uuid4()
    with pytest.raises(ValidationError):
        HypothesesOut(hypotheses=[hyp("deployment_code", [e1]), hyp("deployment_config", [e1])])


def test_inconclusive_cannot_propose_action():
    e1 = uuid.uuid4()
    with pytest.raises(ValidationError):
        DiagnosisDraft(outcome="inconclusive", hypotheses=[hyp("deployment_config", [e1]), hyp("dependency", [e1])],
                       summary="s", proposed_action="config_revert")


def test_invented_and_foreign_citations_rejected():
    mine, other = ev(EvidenceKind.log), ev(EvidenceKind.log, incident=uuid.uuid4())
    records = store(mine, other)
    invented = DiagnosisDraft(outcome="established", leading_index=0, summary="s", ruled_out=["r"],
                              hypotheses=[hyp("deployment_config", [uuid.uuid4()]), hyp("dependency", [mine.evidence_id])])
    with pytest.raises(UnsupportedReference):
        check_citations(invented, records, INC)
    foreign = DiagnosisDraft(outcome="established", leading_index=0, summary="s", ruled_out=["r"],
                             hypotheses=[hyp("deployment_config", [other.evidence_id]), hyp("dependency", [mine.evidence_id])])
    with pytest.raises(UnsupportedReference):
        check_citations(foreign, records, INC)


def test_correlation_only_is_blocking_and_low_confidence():
    change, alert = ev(EvidenceKind.change), ev(EvidenceKind.alert)
    evidence = store(change, alert)
    lead = hyp("deployment_config", [change.evidence_id, alert.evidence_id])
    d = DiagnosisDraft(outcome="established", leading_index=0, summary="s", ruled_out=["db fine"],
                       hypotheses=[lead, hyp("dependency", [alert.evidence_id])])
    issues = code_checks(d, evidence, INC, gaps=0, changed=[])
    assert any(i.kind == IssueKind.correlation_only and i.blocking for i in issues)
    assert score_confidence(lead, evidence, 0, []) == Confidence.low
    accepted = CriticVerdict(verdict="accept", confidence_cap=Confidence.high)
    assert combine(accepted, issues, Confidence.low).verdict.verdict == "revise"     # the model critic can't overrule code


def test_strong_contradiction_blocks():
    log, trace = ev(EvidenceKind.log), ev(EvidenceKind.trace)
    d = DiagnosisDraft(outcome="established", leading_index=0, summary="s", ruled_out=["r"],
                       hypotheses=[hyp("deployment_config", [log.evidence_id], against=[trace.evidence_id]),
                                   hyp("dependency", [trace.evidence_id])])
    issues = code_checks(d, store(log, trace), INC, 0, [])
    assert any(i.kind == IssueKind.contradicted for i in issues)


def test_changed_source_detected_and_blocks():
    log = ev(EvidenceKind.log, summary="original")
    assert recheck_fingerprints([log], lambda e: hashlib.sha256(b"edited").hexdigest()) == [log.evidence_id]
    assert recheck_fingerprints([log], lambda e: e.content_sha256) == []


def established(lead, *others, ruled_out=("r",), action=None):
    return DiagnosisDraft(outcome="established", leading_index=0, summary="s", ruled_out=list(ruled_out),
                          proposed_action=action, hypotheses=[lead, *others])


def kinds(issues):
    return {i.kind for i in issues}


def test_stale_cited_evidence_blocks_through_code_checks():
    log = ev(EvidenceKind.log, tier=3)
    d = established(hyp("deployment_config", [log.evidence_id]), hyp("dependency", [log.evidence_id]))
    issues = code_checks(d, store(log), INC, 0, changed=[log.evidence_id])
    assert [i.blocking for i in issues if i.kind == IssueKind.stale_evidence] == [True]
    assert IssueKind.stale_evidence not in kinds(code_checks(d, store(log), INC, 0, changed=[uuid.uuid4()]))


def test_mechanism_tier_scores_high_and_a_gap_lowers_it():
    split = ev(EvidenceKind.metric, tier=2)
    lead = hyp("deployment_config", [split.evidence_id])
    assert score_confidence(lead, store(split), gaps=0, required_missing=[]) == Confidence.high
    assert score_confidence(lead, store(split), gaps=1, required_missing=[]) == Confidence.medium


def test_kind_alone_gives_no_strength():
    trace, metric, log = ev(EvidenceKind.trace), ev(EvidenceKind.metric), ev(EvidenceKind.log)
    lead = hyp("deployment_config", [trace.evidence_id, metric.evidence_id, log.evidence_id])
    assert score_confidence(lead, store(trace, metric, log), 0, []) == Confidence.low


def test_tier_four_is_medium():
    code = ev(EvidenceKind.trace, tier=4)
    assert score_confidence(hyp("deployment_code", [code.evidence_id]), store(code), 0, []) == Confidence.medium


def test_two_supporting_observations_need_independent_sources():
    a, b = ev(EvidenceKind.error_group, tier=5, system="tracker"), ev(EvidenceKind.metric, tier=6, system="prom")
    same = ev(EvidenceKind.metric, tier=6, system="tracker")
    assert score_confidence(hyp("deployment_code", [a.evidence_id, b.evidence_id]), store(a, b), 0, []) == Confidence.medium
    assert score_confidence(hyp("deployment_code", [a.evidence_id, same.evidence_id]), store(a, same), 0, []) == Confidence.low


def test_timing_alone_is_low_and_flagged_tiers_do_not_count():
    timing = ev(EvidenceKind.change, tier=8)
    flagged = ev(EvidenceKind.metric, tier=2, flagged=True)
    assert score_confidence(hyp("deployment_code", [timing.evidence_id]), store(timing), 0, []) == Confidence.low
    assert score_confidence(hyp("deployment_code", [flagged.evidence_id]), store(flagged), 0, []) == Confidence.low


def test_missing_required_source_is_low_whatever_the_strength():
    split = ev(EvidenceKind.metric, tier=2)
    lead = hyp("deployment_config", [split.evidence_id])
    assert score_confidence(lead, store(split), 0, ["change history"]) == Confidence.low


def test_required_sources():
    alert, change, log, tracker = (ev(EvidenceKind.alert), ev(EvidenceKind.change), ev(EvidenceKind.log),
                                   ev(EvidenceKind.error_group))
    assert missing_required_sources([alert, change, log]) == []
    assert missing_required_sources([alert, change, tracker]) == []
    assert missing_required_sources([log, ev(EvidenceKind.gap)]) == ["alert", "change history"]
    assert missing_required_sources([alert, change, ev(EvidenceKind.metric)]) == ["logs or error tracker"]


def test_any_contradicting_observation_lowers_to_low_and_blocks_unless_guidance_gap_or_timing():
    split = ev(EvidenceKind.metric, tier=2)
    untiered = ev(EvidenceKind.metric)                       # a plain record still counts
    other = hyp("dependency", [split.evidence_id])
    lead = hyp("deployment_config", [split.evidence_id], against=[untiered.evidence_id])
    assert score_confidence(lead, store(split, untiered), 0, []) == Confidence.low
    assert IssueKind.contradicted in kinds(code_checks(established(lead, other), store(split, untiered), INC, 0, []))
    for weak in (ev(EvidenceKind.gap), ev(EvidenceKind.runbook), ev(EvidenceKind.change, tier=8)):
        lead = hyp("deployment_config", [split.evidence_id], against=[weak.evidence_id])
        assert score_confidence(lead, store(split, weak), 0, []) == Confidence.high
        assert IssueKind.contradicted not in kinds(code_checks(established(lead, other), store(split, weak), INC, 0, []))


def test_tier_assignment_from_facts():
    assert assign_tier(EvidenceKind.test_result, ObservationFacts(fails_with_change_clears_without=True)) == 1
    assert assign_tier(EvidenceKind.metric, ObservationFacts(only_new_version_failing=True)) == 2
    assert assign_tier(EvidenceKind.trace, ObservationFacts(changed_key_on_failing_path=True)) == 3
    assert assign_tier(EvidenceKind.error_group, ObservationFacts(frames_in_changed_file=True)) == 4
    assert assign_tier(EvidenceKind.error_group, ObservationFacts(first_seen_after_change=True)) == 5
    assert assign_tier(EvidenceKind.metric, ObservationFacts(failures_grow_with_rollout=True)) == 6
    assert assign_tier(EvidenceKind.change, ObservationFacts(rollback_and_recovery=True)) == 7
    assert assign_tier(EvidenceKind.change, ObservationFacts(linked_by_time=True)) == 8
    assert assign_tier(EvidenceKind.metric, ObservationFacts()) is None
    # strongest wins; a fact on the wrong kind, gaps and guidance give nothing
    both = ObservationFacts(frames_in_changed_file=True, first_seen_after_change=True, linked_by_time=True)
    assert assign_tier(EvidenceKind.error_group, both) == 4
    assert assign_tier(EvidenceKind.log, ObservationFacts(only_new_version_failing=True)) is None
    assert assign_tier(EvidenceKind.runbook, ObservationFacts(linked_by_time=True)) is None
    assert assign_tier(EvidenceKind.gap, ObservationFacts(linked_by_time=True)) is None


def test_one_valid_citation_supports_a_claim():
    good, gap = ev(EvidenceKind.log), ev(EvidenceKind.gap)
    d = established(hyp("deployment_config", [good.evidence_id, gap.evidence_id]), hyp("dependency", [good.evidence_id]))
    report = check_citations(d, store(good, gap), INC)
    assert report.supported_ratio == 1.0


@pytest.mark.parametrize("weak", [ev(EvidenceKind.gap), ev(EvidenceKind.log, flagged=True),
                                  ev(EvidenceKind.runbook), ev(EvidenceKind.past_incident)])
def test_gaps_flagged_and_guidance_give_no_support(weak):
    good = ev(EvidenceKind.log, tier=3)
    d = established(hyp("deployment_config", [good.evidence_id]), hyp("dependency", [weak.evidence_id]))
    assert check_citations(d, store(good, weak), INC).supported_ratio == 0.5
    issues = [i for i in code_checks(d, store(good, weak), INC, 0, []) if i.kind == IssueKind.unsupported_claim]
    assert [i.claim_index for i in issues] == [1] and issues[0].blocking    # an alternative's claim counts too


def test_correlation_only_clears_at_the_medium_bar():
    code = ev(EvidenceKind.trace, tier=4)
    d = established(hyp("deployment_code", [code.evidence_id]), hyp("dependency", [code.evidence_id]))
    assert IssueKind.correlation_only not in kinds(code_checks(d, store(code), INC, 0, []))


def test_scope_raised_for_action_without_a_cited_change_or_on_another_cause():
    change = ev(EvidenceKind.change, tier=3, tool="get_changes")
    log = ev(EvidenceKind.log, tier=3)
    records = store(change, log)
    other = hyp("capacity", [log.evidence_id])
    ok = established(hyp("deployment_config", [change.evidence_id]), other, action="config_revert")
    assert IssueKind.scope not in kinds(code_checks(ok, records, INC, 0, []))
    no_change = established(hyp("deployment_config", [log.evidence_id]), other, action="config_revert")
    assert IssueKind.scope in kinds(code_checks(no_change, records, INC, 0, []))
    wrong_cause = established(hyp("dependency", [change.evidence_id]), other, action="config_revert")
    issues = code_checks(wrong_cause, records, INC, 0, [])
    assert IssueKind.scope in kinds(issues) and all(i.blocking for i in issues)
    no_action = established(hyp("dependency", [log.evidence_id]), other)
    assert IssueKind.scope not in kinds(code_checks(no_action, records, INC, 0, []))


@pytest.mark.parametrize("scored,cap,final", [
    (Confidence.high, Confidence.medium, Confidence.medium), (Confidence.medium, Confidence.high, Confidence.medium),
    (Confidence.high, Confidence.low, Confidence.low), (Confidence.low, Confidence.high, Confidence.low)])
def test_combine_gives_the_lower_of_scored_and_cap(scored, cap, final):
    assert combine(CriticVerdict(verdict="accept", confidence_cap=cap), [], scored).confidence == final


def test_combine_keeps_code_issues_past_the_issue_limit():
    critic = [CriticIssue(kind=IssueKind.overconfident, detail="d", blocking=False)] * 20
    code = [CriticIssue(kind=IssueKind.scope, detail="d", blocking=True)]
    verdict = CriticVerdict(verdict="revise", issues=critic, confidence_cap=Confidence.high)
    assert combine(verdict, code, Confidence.high).verdict.issues[0].kind == IssueKind.scope


def draft_saying(text):
    e1 = uuid.uuid4()
    return established(hyp("deployment_config", [e1], stmt=text), hyp("dependency", [e1]))


@pytest.mark.parametrize("text,code", [
    ("the password: hunter2 leaked", "secret"), ("key AKIAABCDEFGHIJKLMNOP in the log", "secret"),
    ("see https://evil.example/x for details", "link"), ("visit www.evil.example", "link"),
    ("run kubectl rollout undo deploy/checkout", "command"), ("$ ls", "command"), ("then DROP TABLE incidents", "command")])
def test_unsafe_model_output_fails_validation(text, code):
    with pytest.raises(UnsafeOutput) as e:
        check_output(draft_saying(text))
    assert e.value.code == code and text not in str(e.value)


def test_clean_output_passes_and_nested_fields_are_scanned():
    check_output(draft_saying("the timeout value changed in config"))
    e1 = uuid.uuid4()
    nested = hyp("dependency", [e1]).model_copy(update={"claims": [Claim(text="at http://x.example", evidence_ids=[e1])]})
    with pytest.raises(UnsafeOutput):
        check_output(established(hyp("deployment_config", [e1]), nested))


def test_mask_secrets_for_the_stored_copy():
    masked = mask_secrets("token=abc123 and sk-abcdefghijklmnop1234 stay out")
    assert "abc123" not in masked and "sk-abc" not in masked and "stay out" in masked


def test_fingerprint_stable_and_sensitive():
    e1 = uuid.uuid4()
    a = DiagnosisDraft(outcome="established", leading_index=0, summary="one", ruled_out=["r"],
                       hypotheses=[hyp("deployment_config", [e1]), hyp("dependency", [e1])])
    b = DiagnosisDraft(outcome="established", leading_index=0, summary="reworded", ruled_out=["r"],
                       hypotheses=[hyp("deployment_config", [e1]), hyp("dependency", [e1])])
    assert fingerprint(a) == fingerprint(b)                  # rewording isn't a revision
    c = DiagnosisDraft(outcome="established", leading_index=1, summary="one", ruled_out=["r"],
                       hypotheses=[hyp("deployment_config", [e1]), hyp("dependency", [e1])])
    assert fingerprint(a) != fingerprint(c)


def test_critic_cannot_accept_with_blocking_issue():
    with pytest.raises(ValidationError):
        CriticVerdict(verdict="accept", confidence_cap=Confidence.high,
                      issues=[{"kind": "unsupported_claim", "detail": "x", "blocking": True}])


def test_revise_needs_something_to_fix():
    with pytest.raises(ValidationError):
        CriticVerdict(verdict="revise", confidence_cap=Confidence.medium)


def test_evidence_needed_is_typed_and_bounded():
    e1 = uuid.uuid4()
    with pytest.raises(ValidationError):
        HypothesisDraft.model_validate({**hyp("dependency", [e1]).model_dump(), "evidence_needed": ["check logs"]})
    request = {"tool": "get_logs", "reason": "r", "params": {"level": "error"}}
    with pytest.raises(ValidationError):
        HypothesisDraft.model_validate({**hyp("dependency", [e1]).model_dump(), "evidence_needed": [request] * 6})


def test_source_link_required_unless_gap():
    assert ev(EvidenceKind.gap).source_link is None
    fields = ev(EvidenceKind.log).model_dump()
    with pytest.raises(ValidationError):
        Evidence.model_validate({**fields, "source_link": None})


def test_strength_tier_range_and_kinds():
    fields = ev(EvidenceKind.log).model_dump()
    assert Evidence.model_validate({**fields, "strength_tier": 8}).strength_tier == 8
    for bad in (0, 9):
        with pytest.raises(ValidationError):
            Evidence.model_validate({**fields, "strength_tier": bad})
    with pytest.raises(ValidationError):
        Evidence.model_validate({**ev(EvidenceKind.runbook).model_dump(), "strength_tier": 1})


def test_stored_fields_match_the_diagnoses_row():
    e1 = uuid.uuid4()
    d = established(hyp("deployment_config", [e1]), hyp("dependency", [e1]), action="config_revert")
    row = d.stored_fields()
    assert row["category"] == "deployment_config" and row["ruled_out"] == ["r"]
    assert row["claims"] == [{"text": "observed", "evidence_ids": [str(e1)]}]
    inconclusive = DiagnosisDraft(outcome="inconclusive", summary="s", hypotheses=[hyp("dependency", [e1]), hyp("capacity", [e1])])
    assert inconclusive.stored_fields()["category"] == "unknown" and inconclusive.stored_fields()["claims"] == []


CONTEXT = SkillContext(incident_id=INC, service="checkout", environment="prod", started_at=T0)


def test_collect_in_requests_follow_the_mode():
    request = EvidenceRequest(tool="get_logs", reason="r", params={})
    assert CollectIn(context=CONTEXT, mode="baseline").requests == []
    assert len(CollectIn(context=CONTEXT, mode="targeted", requests=[request] * 3).requests) == 3
    with pytest.raises(ValidationError):
        CollectIn(context=CONTEXT, mode="baseline", requests=[request])
    with pytest.raises(ValidationError):
        CollectIn(context=CONTEXT, mode="targeted")
    with pytest.raises(ValidationError):
        CollectIn(context=CONTEXT, mode="targeted", requests=[request] * 4)


def test_knowledge_in_bounds_k():
    assert KnowledgeIn(context=CONTEXT, evidence_ids=[]).k == 5
    with pytest.raises(ValidationError):
        KnowledgeIn(context=CONTEXT, evidence_ids=[], k=11)


def test_patch_contracts():
    e1 = uuid.uuid4()
    params = PatchDraftParams(service="checkout", action_kind="config_revert", target_ref="abc")
    edit = PatchFileEdit(path="config/app.yaml", new_content="a: 1\n", rationale="restores the value", evidence_ids=[e1])
    assert edit.new_content == "a: 1\n"                      # file content isn't stripped
    assert PatchProposal(params=params, files=[edit], summary="revert the setting").files == [edit]
    with pytest.raises(ValidationError):
        PatchProposal(params=params, summary="revert the setting")
    rollback = params.model_copy(update={"action_kind": "release_rollback"})
    assert PatchProposal(params=rollback, summary="roll the release back").files == []
    with pytest.raises(ValidationError):
        PatchFileEdit(path="/etc/passwd", new_content="x", rationale="ten or more", evidence_ids=[e1])
    with pytest.raises(ValidationError):
        PatchFileEdit(path="a.yaml", new_content="x", rationale="short", evidence_ids=[e1])
    good = established(hyp("deployment_config", [e1]), hyp("dependency", [e1]), action="config_revert")
    assert PatchIn(context=CONTEXT, diagnosis_id=e1, diagnosis=good, evidence=[], candidate_no=3).candidate_no == 3
    with pytest.raises(ValidationError):
        PatchIn(context=CONTEXT, diagnosis_id=e1, diagnosis=good, evidence=[], candidate_no=4)
    no_action = established(hyp("deployment_config", [e1]), hyp("dependency", [e1]))
    with pytest.raises(ValidationError):
        PatchIn(context=CONTEXT, diagnosis_id=e1, diagnosis=no_action, evidence=[], candidate_no=1)


def test_skill_result_has_exactly_one_of_output_or_failure():
    failure = SkillFailure(skill="diagnose", code="provider_unavailable", detail="outage")
    out = KnowledgeOut(evidence_ids=[], gap_count=0, index_revision="r")
    with pytest.raises(ValidationError):
        SkillResult[KnowledgeOut](usage=SkillUsage())
    with pytest.raises(ValidationError):
        SkillResult[KnowledgeOut](output=out, failure=failure, usage=SkillUsage())
    with pytest.raises(ValidationError):
        SkillFailure(skill="diagnose", code="made_up", detail="x")
    with pytest.raises(ValidationError):
        ReviewResult(confidence=Confidence.high)


@pytest.mark.parametrize("tool,doc_type", [("search_runbooks", "runbook"), ("search_past_incidents", "past_incident")])
def test_each_knowledge_tool_searches_its_own_document_type(tool, doc_type):
    """The tool picks the type. A model can't widen it: the schema has no doc_type field."""
    calls = []

    class Index:
        def search(self, query, **kw):
            calls.append((query, kw))
            return []
    params = REGISTRY[tool].params(service="checkout", query="5xx", k=3)
    search_knowledge(REGISTRY[tool], params, Index(), {"sre"})
    assert calls == [("5xx", dict(service="checkout", groups={"sre"}, k=3, doc_type=doc_type))]
    assert "doc_type" not in REGISTRY[tool].params.model_fields
