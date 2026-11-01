"""Tests for the agent: coordinator policy, tool gate, contracts and validation.
Run: python -m pytest -q test_agent.py
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from agent_contracts import (Claim, Confidence, CriticVerdict, DiagnosisDraft, Evidence, EvidenceKind,
                             HypothesesOut, HypothesisDraft, IssueKind)
from coordinator import (AgentState, Limits, Step, after_collect, after_critique, after_diagnosis,
                         after_hypotheses, next_step)
from tool_registry import ToolGate, ToolRefused, agent_tools
from validation import (UnsupportedReference, check_citations, code_checks, combine, fingerprint,
                        recheck_fingerprints, score_confidence)

T0 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
INC = uuid.uuid4()


def ev(kind, incident=INC, summary="x"):
    return Evidence(evidence_id=uuid.uuid4(), incident_id=incident, kind=kind, source_system="sys",
                    tool="get_logs", query="q", window_start=T0 - timedelta(hours=1), window_end=T0,
                    collected_at=T0, source_as_of=T0, source_link=None, summary=summary,
                    content_sha256=hashlib.sha256(summary.encode()).hexdigest(),
                    gap_reason="timeout" if kind == EvidenceKind.gap else None)


def hyp(category, ids, against=(), stmt=None):
    return HypothesisDraft(statement=stmt or f"{category} cause", category=category,
                           claims=[Claim(text="observed", evidence_ids=list(ids))],
                           contradicting_evidence_ids=list(against), confirm_test="c", refute_test="r")


# ---------------------------------------------------------------- coordinator
def run(script, limits=Limits(), clock_step=timedelta(seconds=30)):
    """Drive the coordinator with scripted skill results; returns the final state."""
    s = AgentState(started_at=T0, limits=limits)
    now = T0
    for _ in range(50):
        step = next_step(s, now)
        now += clock_step
        if step == Step.COLLECT_BASELINE:
            after_collect(s, [f"e{i}" for i in range(6)], gaps=0, tool_calls=6, targeted=False)
        elif step == Step.HYPOTHESISE:
            after_hypotheses(s, 1, script.get("hyp_requests", []))
        elif step == Step.COLLECT_TARGETED:
            new = script["targeted"].pop(0) if script.get("targeted") else []
            after_collect(s, new, gaps=0, tool_calls=len(s.pending_requests), targeted=True)
        elif step == Step.DIAGNOSE:
            fp, ratio = script["diagnoses"].pop(0)
            after_diagnosis(s, fp, ratio, 1)
        elif step == Step.CRITIQUE:
            verdict, reqs = script["verdicts"].pop(0)
            after_critique(s, verdict, reqs, 1)
        else:
            return s, step
    raise AssertionError("loop did not terminate")


def test_reference_case_accepted_first_time():
    s, step = run({"diagnoses": [("fp1", 1.0)], "verdicts": [("accept", [])]})
    assert step == Step.FINISH_ESTABLISHED and s.model_calls == 3 and s.revisions == 0


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
    assert step == Step.FINISH_INCONCLUSIVE


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


# ---------------------------------------------------------------- tool gate
def gate():
    return ToolGate(incident_id=str(INC), service="checkout", incident_start=T0, now=T0 + timedelta(minutes=5))


def test_write_tools_never_offered_or_callable():
    assert all(t.kind.value != "write" for t in agent_tools())
    with pytest.raises(ToolRefused) as e:
        gate().check("apply_change", {"service": "checkout", "action_kind": "config_revert", "target_ref": "x"})
    assert e.value.code == "write_forbidden"


def test_unknown_tool_refused():
    with pytest.raises(ToolRefused) as e:
        gate().check("run_shell", {"cmd": "rm -rf /"})
    assert e.value.code == "unknown_tool"


def test_out_of_scope_service_refused():
    with pytest.raises(ToolRefused) as e:
        gate().check("get_logs", {"service": "payments"})
    assert e.value.code == "out_of_scope"


def test_month_of_logs_refused_and_default_window_applied():
    g = gate()
    with pytest.raises(ToolRefused) as e:
        g.check("get_logs", {"service": "checkout", "start": (T0 - timedelta(days=30)).isoformat(),
                             "end": T0.isoformat()})
    assert e.value.code == "window_too_large"
    _, p, _ = g.check("get_logs", {"service": "checkout"})
    assert p.end - p.start == timedelta(minutes=65)


def test_invalid_range_and_unbounded_limit_refused():
    g = gate()
    with pytest.raises(ToolRefused) as e:
        g.check("get_logs", {"service": "checkout", "start": T0.isoformat(), "end": (T0 - timedelta(hours=1)).isoformat()})
    assert e.value.code == "invalid_params"
    with pytest.raises(ToolRefused):
        g.check("get_logs", {"service": "checkout", "limit": 100000})


def test_repeated_identical_call_limited():
    g = gate()
    params = {"service": "checkout", "metric": "http_5xx_rate", "start": (T0 - timedelta(hours=1)).isoformat(),
              "end": T0.isoformat()}
    for _ in range(2):
        tool, p, h = g.check("get_metric", params)
        g.record("get_metric", h, new_evidence=0)
    with pytest.raises(ToolRefused) as e:
        g.check("get_metric", params)
    assert e.value.code == "repeated_call"


def test_per_tool_budget():
    g = gate()
    for i in range(4):
        _, _, h = g.check("get_changes", {"service": "checkout", "start": (T0 - timedelta(hours=i + 1)).isoformat(),
                                          "end": T0.isoformat()})
        g.record("get_changes", h, 1)
    with pytest.raises(ToolRefused) as e:
        g.check("get_changes", {"service": "checkout"})
    assert e.value.code == "tool_budget"


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
    store = {mine.evidence_id: mine, other.evidence_id: other}
    invented = DiagnosisDraft(outcome="established", leading_index=0, summary="s", ruled_out=["r"],
                              hypotheses=[hyp("deployment_config", [uuid.uuid4()]), hyp("dependency", [mine.evidence_id])])
    with pytest.raises(UnsupportedReference):
        check_citations(invented, store, INC)
    foreign = DiagnosisDraft(outcome="established", leading_index=0, summary="s", ruled_out=["r"],
                             hypotheses=[hyp("deployment_config", [other.evidence_id]), hyp("dependency", [mine.evidence_id])])
    with pytest.raises(UnsupportedReference):
        check_citations(foreign, store, INC)


def test_correlation_only_is_blocking_and_low_confidence():
    change, alert = ev(EvidenceKind.change), ev(EvidenceKind.alert)
    store = {e.evidence_id: e for e in (change, alert)}
    lead = hyp("deployment_config", [change.evidence_id, alert.evidence_id])
    d = DiagnosisDraft(outcome="established", leading_index=0, summary="s", ruled_out=["db fine"],
                       hypotheses=[lead, hyp("dependency", [alert.evidence_id])])
    issues = code_checks(d, store, INC, "checkout", gaps=0, changed=[])
    assert any(i.kind == IssueKind.correlation_only and i.blocking for i in issues)
    assert score_confidence(lead, store, 0) == Confidence.low
    accepted = CriticVerdict(verdict="accept", confidence_cap=Confidence.high)
    assert combine(accepted, issues).verdict == "revise"     # the model critic can't overrule code


def test_strong_contradiction_blocks():
    log, trace = ev(EvidenceKind.log), ev(EvidenceKind.trace)
    store = {e.evidence_id: e for e in (log, trace)}
    d = DiagnosisDraft(outcome="established", leading_index=0, summary="s", ruled_out=["r"],
                       hypotheses=[hyp("deployment_config", [log.evidence_id], against=[trace.evidence_id]),
                                   hyp("dependency", [trace.evidence_id])])
    issues = code_checks(d, store, INC, "checkout", 0, [])
    assert any(i.kind == IssueKind.contradicted for i in issues)


def test_changed_source_detected_and_blocks():
    log = ev(EvidenceKind.log, summary="original")
    assert recheck_fingerprints([log], lambda e: hashlib.sha256(b"edited").hexdigest()) == [log.evidence_id]
    assert recheck_fingerprints([log], lambda e: e.content_sha256) == []


def test_reference_case_scores_high_with_strong_varied_evidence():
    trace, metric, change = ev(EvidenceKind.trace), ev(EvidenceKind.metric), ev(EvidenceKind.change)
    store = {e.evidence_id: e for e in (trace, metric, change)}
    lead = hyp("deployment_config", [trace.evidence_id, metric.evidence_id, change.evidence_id])
    assert score_confidence(lead, store, gaps=0) == Confidence.high
    assert score_confidence(lead, store, gaps=1) == Confidence.medium


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
