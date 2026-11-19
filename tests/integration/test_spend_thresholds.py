"""T4.9 (Artifact 10.8, 12.3 RB-06; NFR-14, NFR-43; ADR-55, ADR-56): daily and monthly spend at
each threshold, through the real gateway over PgBudgetLedger, priced on the FAKE provider's own
token counts.

One call's usage is what Compose's fake provider reports for a diagnosis-sized request (it counts
4 characters a token); each step settles that usage at the real list price. The limits are scaled
to that one call (the daily limit 10 calls, the monthly ceiling 20), so every threshold is
crossed in 20 calls instead of $500 of spend; the rule is the same arithmetic as at $16.67 and
$500. Everything settles in its own far-future month, so nothing here counts towards the stack's
real day or month (its spend alerts stay quiet and its incidents keep their model steps).

Checked at each step: the alert names the ledger reports (daily 80 and 100, monthly 50, 80 and
100, each once its level is reached and not before), the gauges the alerts read
(`publish_fleet_spend`), and at 100% the next call is refused before it is made (evidence-only,
ADR-55), on that day only for the daily limit and for the rest of the month for the ceiling.
What happens to a running investigation at 100% (finish the current step, then stop and hand
over) is the coordinator's rule, tested offline (tests/unit/test_agent.py, test_workflow.py).
That the expressions fire, with their holds and routes, is test_alert_rules.py (promtool).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import stack
from test_budget_ledger import CAPS, Fake, Out, new_incident, nosleep, request

from aisre.gateway.adapters import ClaudeAdapter
from aisre.gateway.gateway import CLAUDE_PRICES, ModelGateway, cost_micros
from aisre.gateway.gateway_contracts import ResultStatus, Usage
from aisre.gateway.ledger import PgBudgetLedger, publish_fleet_spend
from aisre.reliability import telemetry

PROVIDER = "http://127.0.0.1:59200"
MODEL = "claude-opus-5-5"
DAY1 = datetime(2032, 7, 14, 9, 0, tzinfo=UTC)  # a month no other test touches
DAY2 = DAY1 + timedelta(days=1)


def fake_provider_usage() -> Usage:
    """What the fake provider charges for one diagnosis-sized request."""
    body = {
        "model": MODEL,
        "max_tokens": 2000,
        "system": "You are the diagnosis step. Cite evidence by ID.",
        "messages": [{"role": "user", "content": "ERROR orders-api 500 rate 0.61 " * 400}],
    }
    code, reply = stack.http(
        "POST",
        f"{PROVIDER}/v1/messages",
        body,
        {"x-api-key": "fake-key-for-the-fake-provider", "anthropic-version": "2023-06-01"},
    )
    if code != 200:
        pytest.skip(f"the fake provider isn't answering on {PROVIDER} ({code})")
    u = reply["usage"]
    return Usage(input_tokens=int(u["input_tokens"]), output_tokens=int(u["output_tokens"]))


def gauge(name: str) -> float:
    return telemetry.REGISTRY.get_sample_value(name) or 0.0


def test_spend_alerts_and_evidence_only_at_each_threshold(app_engine, catalogue):
    usage = fake_provider_usage()
    one = cost_micros(usage, CLAUDE_PRICES[MODEL])
    assert one > 0
    daily, monthly = 10 * one, 20 * one
    inc = new_incident(app_engine, catalogue)
    reply = Fake().reply | {
        "usage": {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens}
    }
    seen: list[tuple[str, int, list[str], bool]] = []

    async def step(at: datetime) -> tuple[ResultStatus, int]:
        now = at.timestamp()
        led = PgBudgetLedger(app_engine, CAPS, daily, monthly, clock=lambda: now)
        fake = Fake(reply)
        g = ModelGateway(ClaudeAdapter({"default": MODEL}), fake, led, CLAUDE_PRICES,
                         {"Out": Out}, sleep=nosleep, clock=lambda: now)  # fmt: skip
        # Its own incident each time: the per-incident $2 cap isn't what is tested here.
        r = await g.call(request(new_incident(app_engine, catalogue), deadline_epoch_s=now + 600))
        return r.status, fake.calls

    async def levels(at: datetime) -> tuple[list[str], bool]:
        led = PgBudgetLedger(app_engine, CAPS, daily, monthly)
        return await led.fleet_alerts(at.timestamp()), await led.evidence_only(at.timestamp())

    def run(at: datetime, n: int, label: str) -> None:
        for i in range(n):
            status, calls = asyncio.run(step(at + timedelta(minutes=i)))
            assert status == ResultStatus.OK and calls == 1, (label, i, status)
            alerts, stop = asyncio.run(levels(at + timedelta(minutes=i)))
            seen.append((label, i + 1, alerts, stop))

    assert inc
    run(DAY1, 10, "day 1")
    d1, m1 = DAY1.strftime("%Y-%m-%d"), DAY1.strftime("%Y-%m")
    by_call = {n: (a, s) for lbl, n, a, s in seen if lbl == "day 1"}
    # 80% of the day is reached at the 8th call, 100% at the 10th; 50% of the month at the 10th.
    assert f"daily_spend_80:{d1}" not in by_call[7][0]
    assert f"daily_spend_80:{d1}" in by_call[8][0] and f"daily_spend_100:{d1}" not in by_call[9][0]
    assert f"daily_spend_100:{d1}" in by_call[10][0] and by_call[10][1]  # evidence-only
    assert f"monthly_spend_50:{m1}" not in by_call[9][0]
    assert f"monthly_spend_50:{m1}" in by_call[10][0]
    assert not any(stop for n, (_, stop) in by_call.items() if n < 10)
    # At 100% of the day the next call is refused before it is made (ADR-55).
    status, calls = asyncio.run(step(DAY1 + timedelta(hours=1)))
    assert status == ResultStatus.BUDGET_REFUSED and calls == 0
    # The gauges the alerts read: day and month spend, as the worker publishes them.
    day, month = publish_fleet_spend(app_engine, DAY1 + timedelta(hours=1))
    assert (day, month) == (daily, 10 * one)
    assert gauge("aisre_budget_daily_spent_micros") == daily
    assert gauge("aisre_budget_monthly_spent_micros") == 10 * one

    # The next UTC day starts at 0: model calls resume; the month goes on to 80% and 100%.
    alerts, stop = asyncio.run(levels(DAY2))
    assert not stop and not any(a.startswith("daily_") for a in alerts)
    run(DAY2, 10, "day 2")
    by_call = {n: (a, s) for lbl, n, a, s in seen if lbl == "day 2"}
    assert f"monthly_spend_80:{m1}" not in by_call[5][0]
    assert f"monthly_spend_80:{m1}" in by_call[6][0]  # 16 of 20 calls
    assert f"monthly_spend_100:{m1}" in by_call[10][0] and by_call[10][1]
    # The ceiling holds for the rest of the month, whatever the day.
    status, calls = asyncio.run(step(DAY2 + timedelta(days=3)))
    assert status == ResultStatus.BUDGET_REFUSED and calls == 0
    print(f"one call: {usage.input_tokens} in, {usage.output_tokens} out = {one} micro-dollars")
