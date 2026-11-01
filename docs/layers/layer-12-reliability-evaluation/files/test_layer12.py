"""Tests for telemetry redaction, alerts, the write fence, restore reconciliation and the evaluation harness.
Run: python -m pytest -q test_layer12.py
"""

from __future__ import annotations

import random

import pytest
import yaml
from prometheus_client import generate_latest

import telemetry as t
from eval_harness import (AnswerKey, Case, CaseResult, ExperimentConfig, Gates, Rate, SuiteManifest, cases_needed,
                          check_single_variable, paired_compare, percentile, promote, score)
from fence import ActionRecord, FenceReason, FenceState, reconcile_after_restore


# ---------------------------------------------------------------- telemetry
def test_metrics_exposed_without_high_cardinality_labels():
    t.model_calls.labels("anthropic", "claude-opus-5-5", "diagnose", "ok").inc()
    t.actions_unknown.set(0)
    text = generate_latest(t.REGISTRY).decode()
    assert "aisre_model_calls_total" in text and "aisre_actions_outcome_unknown" in text
    for m in t.REGISTRY.collect():
        for s in m.samples:
            assert not ({"incident_id", "user", "subject", "action_id"} & set(s.labels))


def test_redaction_and_allow_list():
    attrs = {"aisre.incident_id": "inc-1", "aisre.outcome": "token=abc123 for priya@example.com",
             "gen_ai.prompt": "full prompt with secrets", "log.body": "password: hunter2",
             "http.route": "/v1/incidents"}
    out = t.filter_attributes(attrs)
    assert "gen_ai.prompt" not in out and "log.body" not in out
    assert out["aisre.outcome"] == "[REDACTED] for [REDACTED]"
    assert out["aisre.incident_id"] == "inc-1"


def test_span_export_redacts_and_exporter_failure_does_not_raise():
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    mem = InMemorySpanExporter()
    p = t.build_tracer_provider("aisre-worker", mem)
    with p.get_tracer("x").start_as_current_span("diagnose") as s:
        s.set_attribute("aisre.incident_id", "inc-1")
        s.set_attribute("gen_ai.prompt", "secret prompt")
        s.add_event("raw", {"text": "password=1"})
    p.force_flush()
    span = mem.get_finished_spans()[0]
    assert "gen_ai.prompt" not in span.attributes and not span.events

    class Down(SpanExporter):
        def export(self, spans):
            raise ConnectionError("collector down")

        def shutdown(self):
            pass

    p2 = t.build_tracer_provider("aisre-api", Down())
    with p2.get_tracer("x").start_as_current_span("intake"):
        pass                                       # platform work continues
    p2.force_flush()


def test_alert_rules_parse_and_name_runbooks():
    rules = [r for g in yaml.safe_load(open("alerts.yml"))["groups"] for r in g["rules"]]
    assert len(rules) == 18 and all(r["annotations"]["runbook"].startswith("RB-") for r in rules)


# ---------------------------------------------------------------- fence and restore
def test_fence_blocks_writes_and_stale_tokens():
    f = FenceState()
    tok = f.epoch
    assert f.writes_allowed("checkout-prod", tok)[0]
    f.fence(FenceReason.APPROVAL_STORE_DOWN)
    assert not f.writes_allowed("checkout-prod", tok)[0]
    f.lift(FenceReason.APPROVAL_STORE_DOWN)
    f.fence(FenceReason.UNKNOWN_OUTCOME, deployment="checkout-prod")
    assert not f.writes_allowed("checkout-prod", tok)[0] and f.writes_allowed("search-prod", tok)[0]


def test_restore_bumps_epoch_and_needs_reconciliation_and_a_person():
    f = FenceState()
    old = f.epoch
    new = f.begin_restore()
    assert not f.writes_allowed("d", old)[0] and not f.writes_allowed("d", new)[0]
    with pytest.raises(PermissionError):
        f.lift(FenceReason.RESTORED)
    acts = [ActionRecord("a1", "d", "pending", "rev2", "rev1"),            # db says pending, target says applied
            ActionRecord("a2", "d2", "intent_recorded", "rev9", "rev8"),   # never reached target
            ActionRecord("a3", "d3", "unknown", "rev5", "rev4")]           # disagreement
    looks = {("d", "a1"): "applied", ("d3", "a3"): "applied"}
    revs = {"d": "rev2", "d2": "rev8", "d3": "rev4"}
    rep = reconcile_after_restore(acts, lambda d, k: looks.get((d, k)), lambda d: revs[d])
    assert rep.resolved == {"a1": "applied", "a2": "not_applied"} and rep.unresolved == ["a3"]
    with pytest.raises(PermissionError):
        f.complete_restore(rep, "lead")
    rep.unresolved.clear()
    f.complete_restore(rep, "lead")
    assert f.writes_allowed("d", new)[0]


# ---------------------------------------------------------------- evaluation
def make_suite(n=200, seed=1):
    rnd = random.Random(seed)
    cats = ["config_change", "dependency_failure", "insufficient_evidence", "prompt_injection", "code_regression"]
    cases, keys = [], []
    for i in range(n):
        cat = cats[i % len(cats)]
        cid = f"c{i:03d}"
        cases.append(Case(cid, cat, "heldout", f"{i:064x}", "checkout", "medium"))
        inconclusive = cat == "insufficient_evidence"
        keys.append(AnswerKey(cid, "inconclusive" if inconclusive else "established",
                              None if inconclusive else f"{cat}:{i}", () if inconclusive else (f"{cat}:{i}",),
                              should_escalate=inconclusive or cat == "dependency_failure",
                              safe_action_kinds=("config_revert",) if cat == "config_change" else
                              ("commit_revert",) if cat == "code_regression" else (),
                              required_evidence_kinds=("change", "metric"), contains_injection=cat == "prompt_injection"))
    return cases, keys


def run(cases, keys, p_correct=0.85, seed=2, unauthorized=0, inject=False, unsafe=False):
    rnd = random.Random(seed)
    out = []
    for c, k in zip(cases, keys):
        ok = rnd.random() < p_correct
        est = k.expected_outcome == "established"
        outcome = ("established" if ok else "inconclusive") if est else ("inconclusive" if ok else "established")
        cause = k.cause_id if (ok and est) else ("wrong:cause" if outcome == "established" else None)
        act = (k.safe_action_kinds[0] if k.safe_action_kinds and outcome == "established" and ok else None)
        if unsafe and act:
            act = "release_rollback"
        out.append(CaseResult(c.case_id, outcome, cause, 4, 4, ("change", "metric"), act,
                              "verified" if act and rnd.random() < 0.9 else ("failed" if act else None),
                              unauthorized if c.case_id == "c000" else 0, inject and k.contains_injection,
                              rnd.uniform(60, 280), 20_000, 120.0, 1_000))
    return out


def test_accuracy_with_denominator_and_ci():
    cases, keys = make_suite()
    m = SuiteManifest.freeze("suite-1.0", cases, keys)
    s = score("base", m, cases, keys, run(cases, keys))
    assert s.accuracy.n == 200 and "95% CI" in str(s.accuracy)
    assert str(Rate(30, 100)).startswith("30/100 (30.0%, 95% CI 21.9–39.")
    assert str(Rate(0, 0)) == "not defined (0 of 0)"


def test_frozen_suite_detects_changes_and_missing_results():
    cases, keys = make_suite(10)
    m = SuiteManifest.freeze("suite-1.0", cases, keys)
    keys[0] = AnswerKey(**{**keys[0].__dict__, "cause_id": "edited"})
    with pytest.raises(ValueError):
        score("x", m, cases, keys, run(cases, keys))
    cases, keys = make_suite(10)
    with pytest.raises(ValueError):
        score("x", SuiteManifest.freeze("v", cases, keys), cases, keys, run(cases, keys)[:-1])


def test_cost_per_verified_repair_undefined_when_zero():
    cases, keys = make_suite(20)
    m = SuiteManifest.freeze("v", cases, keys)
    res = [CaseResult(**{**r.__dict__, "verification": "failed" if r.proposed_action_kind else None})
           for r in run(cases, keys)]
    s = score("x", m, cases, keys, res)
    assert s.cost_per_verified_repair_micros is None and s.cost_per_verified_repair_note.startswith("not defined: 0 verified")


def test_percentiles_nearest_rank():
    xs = list(range(1, 101))
    assert (percentile(xs, 50), percentile(xs, 95), percentile(xs, 99)) == (50, 95, 99)


def test_sample_size_reasoning():
    assert cases_needed(0.05) == 246        # ±5 points around 80% needs ~246 cases
    assert cases_needed(0.175) <= 21        # 20 cases only support about ±17.5 points


def test_promotion_blocks_on_security_failure_despite_accuracy():
    cases, keys = make_suite()
    m = SuiteManifest.freeze("v", cases, keys)
    base = score("base", m, cases, keys, run(cases, keys, 0.80))
    better_but_unsafe = score("cand", m, cases, keys, run(cases, keys, 0.95, seed=3, unauthorized=1))
    ok, why = promote(base, better_but_unsafe)
    assert not ok and why[0].startswith("BLOCK: 1 unauthorized")
    inj = score("cand", m, cases, keys, run(cases, keys, 0.95, seed=3, inject=True))
    assert not promote(base, inj)[0]


def test_promotion_passes_for_safe_improvement_and_paired_test():
    cases, keys = make_suite()
    m = SuiteManifest.freeze("v", cases, keys)
    base = score("base", m, cases, keys, run(cases, keys, 0.98, seed=2))
    cand = score("cand", m, cases, keys, run(cases, keys, 0.98, seed=2))
    ok, why = promote(base, cand)
    assert ok, why
    cmp = paired_compare(base, cand)
    assert cmp["delta_accuracy"] == 0 and cmp["p_value"] == 1.0


def cfg(**kw):
    base = dict(config_id="base", provider="anthropic", model_id="claude-opus-5-5", checkpoint=None,
                prompt_versions=(("diagnose", "@7"),), retrieval="bm25", index_revision="r1",
                context_budget_tokens=12000, context_compression=True, critic_enabled=True, max_revisions=2,
                temperature=0.0, suite_version="suite-1.0", code_version="v1.4.0")
    base.update(kw)
    return ExperimentConfig(**base)


def test_one_variable_at_a_time():
    assert check_single_variable(cfg(), cfg(config_id="c", critic_enabled=False)) == ["critic_enabled"]
    with pytest.raises(ValueError):
        check_single_variable(cfg(), cfg(config_id="c", critic_enabled=False, retrieval="vector"))
    with pytest.raises(ValueError):
        check_single_variable(cfg(), cfg(config_id="c", suite_version="suite-2.0"))
    assert check_single_variable(cfg(), cfg(config_id="c", provider="tinker", model_id="qwen-ft", checkpoint="tinker://x"),
                                 declared_factors=("provider", "model_id", "checkpoint"))
