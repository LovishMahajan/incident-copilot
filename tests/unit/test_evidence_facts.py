"""Artifact 9.7 (ADR-53), 13.5 T1.11: ObservationFacts computed from collected evidence.

On the reference incident the config match is tier 3, as its answer key says, with the version
split at 2, the new signature at 5 and the scale match at 6. Each rule also has a control.
FR-46, ST-34.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from monitoring_scenarios import ALERT_ID, CUT_OFF, STARTED, copy_reference, serving, sources

from aisre.agent.agent_contracts import (
    Claim,
    Confidence,
    DiagnosisDraft,
    EvidenceKind,
    HypothesisDraft,
    IssueKind,
)
from aisre.agent.tool_registry import ToolGate
from aisre.agent.validation import code_checks, missing_required_sources, score_confidence
from aisre.evidence.collector import Call, Collected, Scope, baseline_plan, collect
from aisre.evidence.facts import ChangeInfo, Observed, change_info, tier_and_reasons

T = datetime(2026, 10, 20, 14, 0, tzinfo=UTC)


def iso(minutes: float) -> str:
    return (T + timedelta(minutes=minutes)).isoformat()


def reference(root=None, calls=None, known=()) -> list[Collected]:
    s = Scope(str(uuid.uuid4()), "orders-api", "production", STARTED, ALERT_ID)
    with serving(*([root] if root else [])) as url:
        out = asyncio.run(
            collect(
                s,
                calls or baseline_plan(s),
                ToolGate(s.incident_id, s.service, STARTED, CUT_OFF),
                sources(url),
                collection_key="k",
                now=CUT_OFF,
                known_changes=known,
            )
        )
    return out.collected


def tiers(collected: list[Collected]) -> dict[str, int | None]:
    return {
        c.attributes["params"].get("metric", c.evidence.tool): c.evidence.strength_tier
        for c in collected
    }


# ---------------------------------------------------------------- the reference incident
def test_reference_incident_tiers_match_its_answer_key():
    got = tiers(reference())
    assert got == {
        "get_alert": 8,
        "get_changes": None,
        "http_5xx_rate": 6,  # scale match: about 3%, 15%, 30% at 5%, 25%, 50%
        "error_rate_by_version": 2,  # version split
        "get_logs": 3,  # config match: the stack reads order_tax_rate_bps
        "get_error_groups": 5,  # ValueError first seen at 14:02:04, after the rollout began
        "get_traces": 3,  # config match: app.config.read on the failing span
        "get_dependency_health": None,
    }


def test_the_config_match_is_tier_3_with_its_reason_kept():
    logs = next(c for c in reference() if c.evidence.tool == "get_logs")
    assert logs.evidence.strength_tier == 3
    assert "changed_key_on_failing_path" in logs.attributes["facts"]


def test_change_history_is_kept_on_the_change_record_for_later_rounds():
    change = next(c for c in reference() if c.evidence.tool == "get_changes")
    info = ChangeInfo.from_json(change.attributes["change"][0])
    assert info.keys == ("ORDER_TAX_RATE_BPS",) and info.version == "2.14.1"
    assert info.changed_at == datetime(2026, 10, 20, 14, 2, tzinfo=UTC)
    assert ChangeInfo.from_json(info.to_json()) == info


def test_a_later_round_uses_the_change_history_an_earlier_one_stored():
    change = next(c for c in reference() if c.evidence.tool == "get_changes")
    known = [ChangeInfo.from_json(c) for c in change.attributes["change"]]
    logs = reference(calls=[Call("get_logs", {"service": "orders-api"})], known=known)
    assert logs[0].evidence.strength_tier == 3
    alone = reference(calls=[Call("get_logs", {"service": "orders-api"})])
    assert alone[0].evidence.strength_tier is None  # nothing to link to


def test_fr46_the_leading_hypothesis_on_the_reference_evidence_scores_high():
    collected = reference()
    ev = {c.evidence.evidence_id: c.evidence for c in collected}
    cite = [
        c.evidence.evidence_id for c in collected if c.evidence.tool in ("get_logs", "get_changes")
    ]
    lead = HypothesisDraft(
        statement="ORDER_TAX_RATE_BPS 8.25 breaks compute_totals",
        category="deployment_config",
        claims=[Claim(text="The failing frame reads the changed key", evidence_ids=cite)],
        confirm_test="Revert the key",
        refute_test="Errors persist after revert",
    )
    gaps = sum(e.kind == EvidenceKind.gap for e in ev.values())
    assert (
        score_confidence(lead, ev, gaps, missing_required_sources(ev.values())) == Confidence.high
    )


def test_withheld_change_history_leaves_no_tier_and_scores_low(tmp_path):
    """Run 2 of Q1.17: without the change history nothing links to a change (ADR-54)."""
    root = copy_reference(tmp_path, without=("release.json", "config-diff.txt"))
    collected = reference(root)
    assert all(c.evidence.strength_tier is None for c in collected)
    ev = {c.evidence.evidence_id: c.evidence for c in collected}
    logs = next(c.evidence for c in collected if c.evidence.tool == "get_logs")
    h = HypothesisDraft(
        statement="The new config value breaks orders",
        category="deployment_config",
        claims=[Claim(text="ValueError quotes 8.25", evidence_ids=[logs.evidence_id])],
        confirm_test="c",
        refute_test="r",
    )
    missing = missing_required_sources(ev.values())
    assert missing == ["change history"] and score_confidence(h, ev, 1, missing) == Confidence.low


# ---------------------------------------------------------------- each rule, with a control
CHANGE = ChangeInfo(
    version="2.0.1",
    previous_version="2.0.0",
    keys=("PAYMENTS_TIMEOUT_MS",),
    files=("src/payments/client.py",),
    changed_at=T,
    rollout=((10.0, T), (50.0, T + timedelta(minutes=10))),
    rollback_at=None,
)


def rate_series(values: list[float], start: float = -30) -> list[dict]:
    return [{"at": iso(start + i), "value": v, "requests": 1000} for i, v in enumerate(values)]


def tier(kind, records, params=None, change=CHANGE, context=()):
    return tier_and_reasons(Observed(kind, params or {}, records), [change], context)[0]


def by_version(new: float, old: float, n: int = 10) -> list[dict]:
    return [
        {
            "at": iso(i),
            "versions": {
                "2.0.0": {"rate": old, "requests": 900},
                "2.0.1": {"rate": new, "requests": 100},
            },
        }
        for i in range(n)
    ]


def test_tier2_version_split_and_its_control():
    params = {"metric": "error_rate_by_version"}
    assert tier(EvidenceKind.metric, by_version(0.4, 0.001), params) == 2
    assert tier(EvidenceKind.metric, by_version(0.4, 0.2), params) != 2  # both versions fail
    assert tier(EvidenceKind.metric, by_version(0.4, 0.001, n=2), params) != 2  # too few minutes


def log(level: str, minutes: float, text: str) -> dict:
    return {"ts": iso(minutes), "level": level, "message": text, "stack": text}


def test_tier3_a_changed_key_on_the_failing_path_and_its_controls():
    on_path = log("ERROR", 5, "client.py line 9: int(settings.payments_timeout_ms)")
    assert tier(EvidenceKind.log, [on_path]) == 3
    assert tier(EvidenceKind.log, [log("INFO", 5, on_path["message"])]) != 3  # not failing
    assert tier(EvidenceKind.log, [log("ERROR", -5, on_path["message"])]) != 3  # before the change
    assert tier(EvidenceKind.log, [log("ERROR", 5, "payments_timeout_msx")]) != 3  # another name


def test_tier4_frames_in_a_changed_file_and_tier5_a_new_signature():
    group = {
        "type": "KeyError",
        "message": "'amount'",
        "first_seen": iso(3),
        "last_seen": iso(9),
        "frames": [["/app/src/payments/client.py", "12"]],
    }
    assert tier(EvidenceKind.error_group, [group]) == 4
    elsewhere = group | {"frames": [["/app/src/orders/api.py", "3"]]}
    assert tier(EvidenceKind.error_group, [elsewhere]) == 5
    old = elsewhere | {"first_seen": iso(-20)}
    assert tier(EvidenceKind.error_group, [old]) == 8  # seen before: only time links it


def test_tier6_failures_grow_with_the_rollout_and_its_control():
    params = {"metric": "http_5xx_rate"}
    grows = rate_series([0.001] * 30 + [0.03] * 10 + [0.15] * 10)
    assert tier(EvidenceKind.metric, grows, params) == 6
    flat = rate_series([0.001] * 30 + [0.1] * 10 + [0.1] * 10)
    assert tier(EvidenceKind.metric, flat, params) != 6


def test_tier7_rollback_and_recovery_on_the_change_and_its_control():
    rolled = ChangeInfo(**{**CHANGE.__dict__, "rollback_at": T + timedelta(minutes=20)})
    metric = Observed(
        EvidenceKind.metric,
        {"metric": "http_5xx_rate"},
        rate_series([0.001] * 30 + [0.2] * 20 + [0.3] * 5 + [0.001] * 10),
    )
    assert tier(EvidenceKind.change, [{}], change=rolled, context=[metric]) == 7
    stuck = Observed(metric.kind, metric.params, rate_series([0.001] * 30 + [0.2] * 35))
    assert tier(EvidenceKind.change, [{}], change=rolled, context=[stuck]) is None
    assert tier(EvidenceKind.change, [{}], change=CHANGE, context=[metric]) is None  # no rollback


def test_change_info_reads_rollout_and_rollback_from_the_record():
    record = {
        "version": "2.0.1",
        "previous_version": "2.0.0",
        "config_changes": [{"key": "A", "old": "1", "new": "2"}],
        "files": ["x.py"],
        "stages": [
            {"step": "approved", "at": iso(-10)},
            {"step": "rollback_started", "at": iso(30)},
        ],
        "rollout": [{"traffic_pct": 50, "at": iso(5)}, {"traffic_pct": 5, "at": iso(0)}],
    }
    info = change_info(record)
    assert info.changed_at == T and info.rollback_at == T + timedelta(minutes=30)
    assert [p for p, _ in info.rollout] == [5.0, 50.0]
    assert change_info(
        {"stages": [{"step": "deployed", "at": iso(1)}]}
    ).changed_at == T + timedelta(minutes=1)


@pytest.mark.parametrize("kind", [EvidenceKind.gap, EvidenceKind.runbook, EvidenceKind.dependency])
def test_records_no_rule_covers_get_no_tier(kind):
    assert tier(kind, [log("ERROR", 5, "PAYMENTS_TIMEOUT_MS")]) is None


# ---------------------------------------------------------------- ST-34
def test_st34_timing_alone_scores_low_and_blocks_a_deployment_cause():
    """Records linked to the change by time only get tier 8; a diagnosis resting on them is low,
    and code_checks raises correlation_only, which the critic can't overrule."""
    from aisre.agent.agent_contracts import Evidence

    unrelated = log("ERROR", 5, "TimeoutError: upstream did not answer")
    assert tier(EvidenceKind.log, [unrelated]) == 8
    inc = uuid.uuid4()
    ev = Evidence(
        evidence_id=uuid.uuid4(),
        incident_id=inc,
        kind=EvidenceKind.log,
        source_system="logs",
        tool="get_logs",
        query="q",
        window_start=T,
        window_end=T,
        collected_at=T,
        source_as_of=None,
        source_link="ref:1",
        summary="TimeoutError after the deploy",
        content_sha256="0" * 64,
        strength_tier=8,
    )
    h = HypothesisDraft(
        statement="The deploy caused it",
        category="deployment_code",
        claims=[Claim(text="Errors began after the deploy", evidence_ids=[ev.evidence_id])],
        confirm_test="c",
        refute_test="r",
    )
    other = h.model_copy(update={"category": "dependency", "statement": "Upstream is slow"})
    draft = DiagnosisDraft(
        outcome="established",
        leading_index=0,
        hypotheses=[h, other],
        summary="Deploy",
        ruled_out=["upstream fine"],
    )
    evidence = {ev.evidence_id: ev}
    assert score_confidence(h, evidence, 0, []) == Confidence.low
    kinds = {i.kind for i in code_checks(draft, evidence, inc, 0, [])}
    assert IssueKind.correlation_only in kinds
