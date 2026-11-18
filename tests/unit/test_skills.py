"""Artifact 9.2, 9.3, 9.6-9.8, 13.5 T1.14: the model-backed skills, offline.

Evidence is the reference incident's baseline round, collected by the real Evidence Collector from
the fake monitoring source in-process. The Model Gateway is the real one, over the Compose
stack's fake provider and its scripts (ops/fakes/provider-scripts), answering in-process. FR-10,
FR-11, FR-14 to FR-16, FR-45.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from monitoring_scenarios import copy_reference
from skill_rig import ScriptedTransport, collected, skills

from aisre.agent.agent_contracts import Confidence, EvidenceKind, HypothesesOut
from aisre.agent.coordinator import AgentState, Step, apply_result, next_step
from aisre.agent.skills import reasoning
from aisre.agent.validation import check_output
from aisre.gateway.adapters import HttpResponse
from aisre.gateway.gateway_contracts import TASK_MODEL_ALIAS
from aisre.workflow import activities as act

WITHHELD = ("release.json", "config-diff.txt")


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def skill_in(inputs: reasoning.Inputs, key: str, **kw: Any) -> act.SkillIn:
    ids = [str(e.evidence_id) for e in inputs.evidence]
    return act.SkillIn(str(inputs.context.incident_id), key, evidence_ids=ids, **kw)


def investigate(inputs: reasoning.Inputs, transport: Any = None) -> tuple[AgentState, Any, Any]:
    """The coordinator's loop over the three skills, from evidence already collected."""
    sk, store, transport = skills(inputs, transport)
    s = AgentState(started_at=inputs.context.started_at)
    s.baseline_done = s.knowledge_done = True
    s.evidence_ids = [str(e.evidence_id) for e in inputs.evidence]
    hyps = diagnosis = critic = None
    n = 0
    while (step := next_step(s, inputs.context.started_at)) not in (
        Step.FINISH_ESTABLISHED,
        Step.FINISH_INCONCLUSIVE,
    ):
        n += 1
        if step == Step.HYPOTHESISE:
            result = run(sk.hypothesise(skill_in(inputs, f"k:s{n}")))
            hyps = result.output
        elif step == Step.DIAGNOSE:
            result = run(sk.diagnose(skill_in(inputs, f"k:s{n}", hypotheses=hyps, critic=critic)))
            diagnosis = result.output
        elif step == Step.CRITIQUE:
            result = run(
                sk.review_diagnosis(
                    skill_in(
                        inputs,
                        f"k:s{n}",
                        diagnosis=diagnosis.draft,
                        diagnosis_id=str(diagnosis.diagnosis_id),
                    )
                )
            )
            critic = result.output.verdict if result.output else None
        else:
            raise AssertionError(step)
        apply_result(s, step, result)
    return s, store, transport


def test_reference_incident_gets_a_cited_established_diagnosis_naming_the_changed_setting():
    inputs = collected()
    s, store, transport = investigate(inputs)
    assert s.stop_reason == "critic accepted" and s.confidence == "high"
    assert [d.split(":")[0] for d in s.decisions[:3]] == ["hypothesise", "diagnose", "critique"]
    (saved,) = store.diagnoses.values()
    draft = saved["draft"]
    lead = draft.hypotheses[draft.leading_index]
    assert draft.outcome == "established" and draft.proposed_action == "config_revert"
    assert "ORDER_TAX_RATE_BPS" in lead.statement and lead.category == "deployment_config"
    kinds = {e.evidence_id: e.kind for e in inputs.evidence}
    cited = {kinds[i] for c in lead.claims for i in c.evidence_ids}
    assert {EvidenceKind.change, EvidenceKind.metric, EvidenceKind.log} <= cited
    assert saved["confidence"] == Confidence.high  # tier 2 version split, no gap (ADR-53)
    (review,) = store.reviews.values()
    assert review.verdict.verdict == "accept" and review.confidence == Confidence.high
    assert review.stored_fields(draft)["final_outcome"] == "established"
    assert s.model_calls == 3 and s.cost_micros > 0
    # Model per task (ADR-69) and the prompt version from prompts/ (ADR-63)
    models = [(b["model"], b["system"][:30]) for b in transport.bodies]
    assert [m for m, _ in models] == ["claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5-5"]
    assert TASK_MODEL_ALIAS["hypothesise"] == "standard"


def test_honesty_variant_ends_root_cause_not_established(tmp_path):
    root = copy_reference(tmp_path, without=WITHHELD)
    inputs = collected(root)
    s, store, _ = investigate(inputs)
    assert s.stop_reason == "diagnoser could not establish a cause"
    (saved,) = store.diagnoses.values()
    assert saved["draft"].outcome == "inconclusive" and saved["draft"].proposed_action is None
    assert saved["draft"].summary.startswith("Root cause not established")
    assert saved["confidence"] == Confidence.low and not store.reviews


def test_the_context_is_evidence_with_its_facts_as_data_blocks():
    inputs = collected()
    _, _, transport = investigate(inputs)
    first = transport.bodies[0]
    text = first["messages"][0]["content"]
    assert "required sources missing: none" in text
    split = next(e for e in inputs.evidence if e.strength_tier == 2)
    assert f"evidence:{split.evidence_id} tier 2 (only_new_version_failing" in text
    for e in inputs.evidence:
        assert f'<data ref="evidence:{e.evidence_id}"' in text
    assert first["output_config"]["format"]["type"] == "json_schema"
    assert "{maxLength" in json.dumps(first["output_config"]["format"]["schema"])  # moved out


def test_every_scripted_answer_is_a_valid_safe_contract_output(tmp_path):
    for inputs in (collected(), collected(copy_reference(tmp_path, without=WITHHELD))):
        _, _, transport = investigate(inputs)
        for body in transport.bodies:
            assert body["metadata"]["user_id"] == str(inputs.context.incident_id)


def test_a_citation_outside_the_evidence_is_asked_again_then_fails():
    inputs = collected()
    bogus = "00000000-0000-4000-8000-000000000000"

    def invent(body: dict[str, Any]) -> HttpResponse:
        h = {
            "statement": "s",
            "category": "unknown",
            "claims": [{"text": "t", "evidence_ids": [bogus]}],
            "confirm_test": "c",
            "refute_test": "r",
        }
        text = json.dumps({"hypotheses": [h, h | {"category": "dependency"}]})
        msg = {
            "model": body["model"],
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 10},
        }
        return HttpResponse(200, msg, {})

    sk, _, transport = skills(inputs, ScriptedTransport(override=invent))
    result = run(sk.hypothesise(skill_in(inputs, "k:s1")))
    assert result.failure.code == "invalid_output" and bogus in result.failure.detail
    assert len(transport.bodies) == 3 and result.usage.model_calls == 3
    assert "failed validation" in transport.bodies[-1]["messages"][0]["content"]


@pytest.mark.parametrize(
    "status,kind,code",
    [
        (529, "overloaded_error", "provider_unavailable"),
        (401, "authentication_error", "provider_unavailable"),
        (400, "invalid_request_error", "invalid_output"),
    ],
)
def test_provider_failures_are_typed_skill_failures(status, kind, code):
    inputs = collected()
    err = HttpResponse(status, {"type": "error", "error": {"type": kind, "message": "x"}}, {})
    sk, _, _ = skills(inputs, ScriptedTransport(override=lambda b: err))
    result = run(sk.hypothesise(skill_in(inputs, "k:s1")))
    assert result.output is None and result.failure.code == code
    assert result.failure.skill == "hypothesise"


def test_a_retried_step_reuses_the_recorded_answer_and_counts_no_call():
    inputs = collected()
    sk, _, transport = skills(inputs)
    first = run(sk.hypothesise(skill_in(inputs, "k:s1")))
    again = run(sk.hypothesise(skill_in(inputs, "k:s1")))
    assert first.output == again.output and len(transport.bodies) == 1
    assert (first.usage.model_calls, again.usage.model_calls) == (1, 0)


def test_hypothesis_requests_outside_the_skills_tools_are_dropped():
    inputs = collected()

    def ask_for_apply(body: dict[str, Any]) -> HttpResponse | None:
        good = {
            "tool": "get_logs",
            "reason": "error logs by version",
            "params": {"service": "orders-api"},
        }
        bad = {"tool": "apply_change", "reason": "fix it", "params": {}}
        ev = str(inputs.evidence[1].evidence_id)
        h = {
            "statement": "s",
            "category": "dependency",
            "claims": [{"text": "t", "evidence_ids": [ev]}],
            "confirm_test": "c",
            "refute_test": "r",
            "evidence_needed": [good, bad],
        }
        text = json.dumps({"hypotheses": [h, h]})
        msg = {"model": body["model"], "content": [{"type": "text", "text": text}]}
        return HttpResponse(200, msg | {"stop_reason": "end_turn", "usage": {}}, {})

    sk, _, _ = skills(inputs, ScriptedTransport(override=ask_for_apply))
    out = run(sk.hypothesise(skill_in(inputs, "k:s1"))).output
    assert isinstance(out, HypothesesOut)
    assert [r.tool for h in out.hypotheses for r in h.evidence_needed] == ["get_logs", "get_logs"]


def test_scripts_answers_pass_the_output_check():
    inputs = collected()
    _, store, _ = investigate(inputs)
    for saved in store.diagnoses.values():
        check_output(saved["draft"])
