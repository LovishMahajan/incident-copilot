"""Artifact 13.5 T1.7: durable reservations and the daily and monthly ledgers, as aisre_app.

The real gateway over PgBudgetLedger with a fake transport. Limits are enforced before the call
(invariant 8): a refused call never reaches the transport (NFR-13, NFR-36, NFR-42, ST-35), and
the database's CHECK decides when workers race. Every attempt leaves a model_attempts row with
its cost (NFR-15). Fleet tests settle in their own far-future month, so other tests' spend in
the same database doesn't count.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel
from sqlalchemy import select, text

from aisre.contracts import IncidentCreate
from aisre.gateway.adapters import ClaudeAdapter, HttpResponse
from aisre.gateway.gateway import (
    BudgetExceeded,
    GatewayConfig,
    ModelGateway,
    Price,
)
from aisre.gateway.gateway_contracts import ModelRequest, ResultStatus, Usage
from aisre.gateway.ledger import PgBudgetLedger
from aisre.record import budget as rb
from aisre.record import create_incident

PRICE = Price(3_000_000, 15_000_000, 300_000, 3_750_000)
PRICES = {"claude-opus-5-5": PRICE}
FAR = datetime(2031, 3, 15, 12, 0, tzinfo=UTC).timestamp()  # a month no other test touches
CAPS = rb.Caps(max_calls=30, max_input_tokens=200_000, max_output_tokens=20_000,
               max_cost_micros=2_000_000)  # fmt: skip


class Out(BaseModel):
    leading: str


GOOD = json.dumps({"leading": "config revert"})


class Fake:
    """Fake transport: a fixed reply, counting calls."""

    def __init__(self, reply=None, status=200):
        self.calls, self.status = 0, status
        self.reply = reply or {
            "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": GOOD}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 120, "output_tokens": 40},
        }

    async def __call__(self, provider, body, timeout):
        self.calls += 1
        return HttpResponse(self.status, self.reply, {"request-id": "req_1"}), True


async def nosleep(_):
    return None


def new_incident(engine, catalogue) -> str:
    org, service, _ = catalogue
    body = IncidentCreate.model_validate(
        {
            "service": service,
            "environment": "production",
            "started_at": datetime(2026, 10, 20, 14, 7, tzinfo=UTC),
            "title": "OrdersApiHighErrorRate",
            "source": "monitoring",
            "source_alert_id": f"alrt-{uuid4().hex[:8]}",
            "fingerprint": f"{service}:{uuid4().hex}",
        }
    )
    # A zero grouping window keeps each call its own incident; otherwise FR-43 would attach
    # every alert for this service within 10 minutes to the first open incident.
    with engine.begin() as conn:
        intake = create_incident(
            conn, org_id=org, caller_id="dev:test", body=body, grouping_window=timedelta(0)
        )
        return str(intake.incident_id)


def request(incident_id: str, **kw) -> ModelRequest:
    base = dict(
        request_id=f"{incident_id}:diagnose:{uuid4()}",
        incident_id=incident_id,
        task="diagnose",
        prompt_id="diagnose",
        prompt_version="diagnose@1",
        system="Diagnose.",
        context=[{"ref": "evidence:1", "kind": "log", "text": f"ERROR {uuid4()}"}],
        output_schema=Out.model_json_schema(),
        output_schema_name="Out",
        max_output_tokens=500,
        deadline_epoch_s=time.time() + 600,
    )
    return ModelRequest(**(base | kw))


def gateway(transport, ledger) -> ModelGateway:
    return ModelGateway(
        ClaudeAdapter({"default": "claude-opus-5-5"}),
        transport,
        ledger,
        PRICES,
        {"Out": Out},
        GatewayConfig(),
        sleep=nosleep,
    )


def row(engine, incident_id: str) -> rb.Use:
    with engine.connect() as conn:
        u = rb.use(conn, UUID(incident_id))
    assert u is not None
    return u


def attempts(engine, incident_id: str) -> list:
    with engine.connect() as conn:
        return conn.execute(
            select(rb.model_attempts)
            .where(rb.model_attempts.c.incident_id == UUID(incident_id))
            .order_by(rb.model_attempts.c.started_at)
        ).all()


def test_a_call_settles_in_the_database_and_records_its_attempt(app_engine, catalogue):
    inc = new_incident(app_engine, catalogue)
    fake = Fake()
    result = asyncio.run(gateway(fake, PgBudgetLedger(app_engine, CAPS)).call(request(inc)))
    assert result.status == ResultStatus.OK and fake.calls == 1
    u = row(app_engine, inc)
    cost = (120 * 3_000_000 + 40 * 15_000_000) // 1_000_000
    assert (u.calls, u.reserved_cost_micros, u.spent_cost_micros) == (1, 0, cost)
    assert (u.input_tokens, u.output_tokens) == (120, 40)
    [a] = attempts(app_engine, inc)
    assert (a.status, a.cost_micros, a.input_tokens, a.usage_reported) == ("ok", cost, 120, True)
    assert a.reservation_id is not None and a.provider_request_id == "req_1"
    assert a.input_sha256 == result.input_sha256


def test_the_call_cap_holds_across_workers_before_the_call(app_engine, catalogue):
    """NFR-13: a second ledger (another worker) sees the first's call; the cap refuses it
    before the transport is reached, and the refusal is recorded."""
    inc = new_incident(app_engine, catalogue)
    caps = rb.Caps(1, 200_000, 20_000, 2_000_000)
    first, second = Fake(), Fake()
    assert asyncio.run(gateway(first, PgBudgetLedger(app_engine, caps)).call(request(inc))).status == ResultStatus.OK  # fmt: skip
    refused = asyncio.run(gateway(second, PgBudgetLedger(app_engine, caps)).call(request(inc)))
    assert refused.status == ResultStatus.BUDGET_REFUSED and refused.error == "model call cap"
    assert second.calls == 0 and row(app_engine, inc).calls == 1
    assert [a.status for a in attempts(app_engine, inc)] == ["ok", "budget_refused"]


@pytest.mark.parametrize(
    ("caps", "reason"),
    [
        (rb.Caps(30, 100, 20_000, 2_000_000), "token cap"),
        (rb.Caps(30, 200_000, 400, 2_000_000), "token cap"),
        (rb.Caps(30, 200_000, 20_000, 1_000), "spend cap"),
    ],
    ids=["input_tokens", "output_tokens", "cost"],
)
def test_each_incident_cap_refuses_before_the_call(app_engine, catalogue, caps, reason):
    """NFR-36, NFR-42, ST-35."""
    inc = new_incident(app_engine, catalogue)
    fake = Fake()
    r = asyncio.run(gateway(fake, PgBudgetLedger(app_engine, caps)).call(request(inc)))
    assert (r.status, r.error, fake.calls) == (ResultStatus.BUDGET_REFUSED, reason, 0)
    with app_engine.connect() as conn:  # the refusal rolled back; nothing is held
        assert rb.use(conn, UUID(inc)) is None
    assert [a.status for a in attempts(app_engine, inc)] == ["budget_refused"]


def test_racing_workers_cannot_overrun_the_spend_cap(app_engine, catalogue):
    """Ten reservations at once against room for three: the CHECK lets exactly three in."""
    inc = new_incident(app_engine, catalogue)
    ledgers = [PgBudgetLedger(app_engine, rb.Caps(30, 200_000, 20_000, 3_000)) for _ in range(10)]

    async def one(led: PgBudgetLedger) -> bool:
        try:
            await led.reserve(str(uuid4()), inc, 10, 10, 1_000, time.time() + 600)
            return True
        except BudgetExceeded:
            return False

    async def all_at_once():
        return await asyncio.gather(*(one(led) for led in ledgers))

    assert sum(asyncio.run(all_at_once())) == 3
    u = row(app_engine, inc)
    assert (u.calls, u.reserved_cost_micros) == (3, 3_000)


def test_expiry_charges_a_sent_reservation_and_releases_an_unsent_one(app_engine, catalogue):
    """ADR-61: sent with unknown usage is charged in full and awaits reconciliation; never sent
    is released. A call whose reservation expired before sending is not sent."""
    inc = new_incident(app_engine, catalogue)
    now = [time.time()]
    led = PgBudgetLedger(app_engine, CAPS, clock=lambda: now[0])

    async def go():
        sent = await led.reserve(str(uuid4()), inc, 100, 100, 5_000, now[0] + 60)
        unsent = await led.reserve(str(uuid4()), inc, 100, 100, 7_000, now[0] + 60)
        assert await led.mark_sent(sent)
        now[0] += 61
        done = await led.expire()
        late = await led.mark_sent(unsent)
        await led.settle(sent, Usage(input_tokens=1, output_tokens=1), 1)  # expiry won: no-op
        return sent, unsent, done, late

    sent, unsent, done, late = asyncio.run(go())
    # Other tests' reservations in the same database may fall due in the same sweep.
    assert {sent.reservation_id, unsent.reservation_id} <= set(done) and not late
    u = row(app_engine, inc)
    assert (u.calls, u.spent_cost_micros, u.reserved_cost_micros) == (1, 5_000, 0)
    assert sent.reservation_id in asyncio.run(led.pending_reconciliation())


def test_reconciliation_corrects_the_charge_in_the_period_it_was_charged(app_engine, catalogue):
    inc = new_incident(app_engine, catalogue)
    led = PgBudgetLedger(app_engine, CAPS, clock=lambda: FAR)

    async def go():
        r = await led.reserve(str(uuid4()), inc, 100, 100, 9_000, FAR + 600)
        await led.mark_sent(r)
        await led.settle(r, Usage(reported=False), 0)  # broken stream: charged in full
        before = await led.fleet_spend(FAR)
        left = await led.reconcile({r.reservation_id: (Usage(input_tokens=80, output_tokens=20), 2_500)})  # fmt: skip
        after = await led.fleet_spend(FAR)
        next_month = await led.fleet_spend(datetime(2031, 4, 2, tzinfo=UTC).timestamp())
        return r, before, left, after, next_month

    r, before, left, after, next_month = asyncio.run(go())
    assert r.reservation_id not in left
    assert after[0] - before[0] == after[1] - before[1] == 2_500 - 9_000
    assert next_month == (0, 0)
    u = row(app_engine, inc)
    assert (u.spent_cost_micros, u.input_tokens, u.output_tokens) == (2_500, 80, 20)


def test_fleet_limit_refuses_before_the_call_and_alerts(app_engine, catalogue):
    """ADR-55, ADR-56: at 100% of the day, a new call is refused before it is made."""
    inc = new_incident(app_engine, catalogue)
    probe = PgBudgetLedger(app_engine, CAPS, clock=lambda: FAR)

    async def go():
        r = await probe.reserve(str(uuid4()), inc, 10, 10, 4_000, FAR + 600)
        await probe.mark_sent(r)
        await probe.settle(r, Usage(input_tokens=10, output_tokens=10), 4_000)
        day, month = await probe.fleet_spend(FAR)
        led = PgBudgetLedger(app_engine, CAPS, daily_limit=day, monthly_limit=month * 10, clock=lambda: FAR)  # fmt: skip
        fake = Fake()
        g = ModelGateway(ClaudeAdapter({"default": "claude-opus-5-5"}), fake, led, PRICES,
                         {"Out": Out}, sleep=nosleep, clock=lambda: FAR)  # fmt: skip
        result = await g.call(request(inc, deadline_epoch_s=FAR + 600))
        return result, fake.calls, await led.fleet_alerts(FAR)

    result, calls, alerts = asyncio.run(go())
    assert result.status == ResultStatus.BUDGET_REFUSED and calls == 0
    assert "daily_spend_100:2031-03-15" in alerts and "monthly_spend_100:2031-03" not in alerts


def test_settlement_above_what_the_caps_leave_is_charged_up_to_the_cap(app_engine, catalogue):
    """Money already spent can't be refused: a low estimate settles at the cap, and the exact
    usage stays on the attempt."""
    inc = new_incident(app_engine, catalogue)
    led = PgBudgetLedger(app_engine, rb.Caps(30, 200, 20_000, 2_000_000))

    async def go():
        r = await led.reserve(str(uuid4()), inc, 150, 100, 5_000, time.time() + 600)
        await led.mark_sent(r)
        await led.settle(r, Usage(input_tokens=900, output_tokens=50), 6_000)
        return r

    asyncio.run(go())
    u = row(app_engine, inc)
    assert (u.input_tokens, u.output_tokens, u.spent_cost_micros) == (200, 50, 6_000)


def test_release_only_returns_an_unsent_reservation(app_engine, catalogue):
    inc = new_incident(app_engine, catalogue)
    led = PgBudgetLedger(app_engine, CAPS)

    async def go():
        r = await led.reserve(str(uuid4()), inc, 10, 10, 1_000, time.time() + 600)
        await led.mark_sent(r)
        await led.release_unsent(r)  # sent: release refused, it stays held
        return r

    asyncio.run(go())
    with app_engine.connect() as conn:
        status = conn.execute(
            select(rb.budget_reservations.c.status).where(
                rb.budget_reservations.c.incident_id == UUID(inc)
            )
        ).scalar_one()
    assert status == "reserved" and row(app_engine, inc).calls == 1


def test_migration_0003_indexes_the_settled_reservations(app_engine):
    with app_engine.connect() as conn:
        found = conn.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_reservations_settled'")
        ).scalar_one()
    assert "(settled_at)" in found and "WHERE (status = 'settled'" in found
