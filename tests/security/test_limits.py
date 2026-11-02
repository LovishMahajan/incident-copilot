"""ST-35: injected text can't burn the budget. Real gateway, budget ledger, ToolGate and coordinator."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from aisre.agent.coordinator import AgentState, Limits, Step, next_step
from aisre.gateway.gateway import BudgetLedger, FleetBudget, IncidentBudget
from aisre.gateway.gateway_contracts import MAX_INPUT_TOKENS_PER_CALL, MAX_OUTPUT_TOKENS_PER_CALL, ResultStatus, StopReason
from support import NOW_EPOCH, FakeProvider, draft_json, gateway, provider_reply, request
from aisre.agent.tool_registry import ToolGate, ToolRefused

NOW = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)


def run(provider, ledger=None, **over):
    gw = gateway(provider, ledger)
    return asyncio.run(gw.call(request(**over))), gw


def refused_before_the_call(result, provider):
    assert result.status == ResultStatus.BUDGET_REFUSED and provider.bodies == []


def test_st35_oversize_request_is_refused_before_reservation_and_recorded():
    provider, ledger = FakeProvider(provider_reply(draft_json())), BudgetLedger(clock=lambda: NOW_EPOCH)
    huge = "x" * (MAX_INPUT_TOKENS_PER_CALL * 3 + 300)
    result, gw = run(provider, ledger, context=[{"ref": "evidence:1", "kind": "log", "text": huge}])
    refused_before_the_call(result, provider)
    assert ledger.open == {} and ledger.get("inc-1").calls == 0
    assert [r.status for r in gw.attempt_log] == [ResultStatus.BUDGET_REFUSED]


def test_st35_output_over_the_per_call_limit_cannot_be_requested():
    with pytest.raises(ValidationError):
        request(max_output_tokens=MAX_OUTPUT_TOKENS_PER_CALL + 1)


@pytest.mark.parametrize("budget", [IncidentBudget(calls=30), IncidentBudget(spent_cost=2_000_000),
                                    IncidentBudget(input_tokens=200_000), IncidentBudget(output_tokens=20_000)],
                         ids=["calls", "cost", "input_tokens", "output_tokens"])
def test_st35_at_any_incident_cap_the_call_is_refused_before_it_is_made(budget):
    provider, ledger = FakeProvider(provider_reply(draft_json())), BudgetLedger(clock=lambda: NOW_EPOCH)
    ledger.budgets["inc-1"] = budget
    refused_before_the_call(run(provider, ledger)[0], provider)


def test_st35_control_with_budget_left_the_call_is_made():
    provider = FakeProvider(provider_reply(draft_json()))
    assert run(provider)[0].status == ResultStatus.OK and len(provider.bodies) == 1


def test_st35_fleet_limit_refuses_before_the_call():
    fleet = FleetBudget()
    fleet.charge(fleet.daily_limit, NOW_EPOCH)
    provider = FakeProvider(provider_reply(draft_json()))
    refused_before_the_call(run(provider, BudgetLedger(fleet=fleet, clock=lambda: NOW_EPOCH))[0], provider)


def test_st35_output_that_keeps_truncating_gets_one_larger_retry():
    provider = FakeProvider(provider_reply('{"outcome":', stop="max_tokens"))
    result, _ = run(provider)
    assert result.status == ResultStatus.TRUNCATED and len(provider.bodies) == 2
    assert result.stop_reason == StopReason.MAX_TOKENS


def test_st35_a_retry_loop_is_charged_every_attempt():
    provider, ledger = FakeProvider(provider_reply("not json")), BudgetLedger(clock=lambda: NOW_EPOCH)
    run(provider, ledger)
    assert ledger.get("inc-1").calls == 3 and ledger.get("inc-1").spent_cost > 0


def test_st35_evidence_cannot_drive_a_tool_loop():
    gate = ToolGate("inc-1", "checkout", NOW, NOW + timedelta(minutes=5))
    params = {"service": "checkout"}
    for _ in range(2):
        _, _, call_hash = gate.check("get_logs", params, "collect_evidence")
        gate.record("get_logs", call_hash, 0)
    with pytest.raises(ToolRefused) as e:
        gate.check("get_logs", params, "collect_evidence")
    assert e.value.code == "repeated_call"


def test_st35_tool_budget_stops_varied_calls_too():
    gate = ToolGate("inc-1", "checkout", NOW, NOW + timedelta(minutes=5))
    for n in range(8):
        _, _, call_hash = gate.check("get_logs", {"service": "checkout", "contains": f"e{n}"}, "collect_evidence")
        gate.record("get_logs", call_hash, 1)
    with pytest.raises(ToolRefused) as e:
        gate.check("get_logs", {"service": "checkout", "contains": "e9"}, "collect_evidence")
    assert e.value.code == "tool_budget"


@pytest.mark.parametrize("spent,reason", [(dict(model_calls=30), "model call budget"), (dict(input_tokens=200_000), "token budget"),
                                          (dict(cost_micros=2_000_000), "spend limit")])
def test_st35_the_loop_hands_over_at_the_cap(spent, reason):
    state = AgentState(started_at=NOW, baseline_done=True, knowledge_done=True, **spent)
    assert next_step(state, NOW) == Step.FINISH_INCONCLUSIVE and reason in state.stop_reason


def test_st35_a_concurrent_burst_cannot_pass_the_cap_together():
    provider, ledger = FakeProvider(provider_reply(draft_json())), BudgetLedger(clock=lambda: NOW_EPOCH)
    ledger.budgets["inc-1"] = IncidentBudget(calls=Limits().max_model_calls - 1)
    gw = gateway(provider, ledger)

    async def burst():
        return await asyncio.gather(*(gw.call(request(request_id=f"r{n}", prompt_version=f"d@{n}", system=f"s{n}")) for n in range(5)))
    results = asyncio.run(burst())
    assert len(provider.bodies) == 1
    assert sum(r.status == ResultStatus.BUDGET_REFUSED for r in results) == 4
