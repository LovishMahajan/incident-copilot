"""Artifact 13.5 T1.8: result reuse over model_attempts by input_sha256 (ADR-61), as aisre_app.

A worker that is lost after a call succeeded is replaced; Temporal re-runs the activity, and
the new gateway, on a new ledger, finds the recorded result: no reservation, no call, nothing
billed twice. Only a schema-valid result is reused, and only within its incident.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

from sqlalchemy import text
from test_budget_ledger import CAPS, Fake, gateway, new_incident, request

from aisre.gateway.gateway_contracts import ResultStatus
from aisre.gateway.ledger import PgBudgetLedger
from aisre.record import budget as rb


def calls_held(engine, inc: str) -> int:
    with engine.connect() as conn:
        u = rb.use(conn, UUID(inc))
    return 0 if u is None else u.calls


def test_a_rerun_after_a_lost_worker_reuses_the_recorded_result(app_engine, catalogue):
    inc = new_incident(app_engine, catalogue)
    step = request(inc)
    first, second = Fake(), Fake()
    done = asyncio.run(gateway(first, PgBudgetLedger(app_engine, CAPS)).call(step))
    rerun = asyncio.run(
        gateway(second, PgBudgetLedger(app_engine, CAPS)).call(
            step.model_copy(update={"request_id": f"{inc}:diagnose:rerun"})
        )
    )
    assert done.status == rerun.status == ResultStatus.OK and second.calls == 0
    assert rerun.parsed == done.parsed and rerun.request_id.endswith(":rerun")
    assert calls_held(app_engine, inc) == 1


def test_a_different_input_or_incident_is_a_new_call(app_engine, catalogue):
    inc, other = new_incident(app_engine, catalogue), new_incident(app_engine, catalogue)
    step = request(inc)
    fake = Fake()
    asyncio.run(gateway(fake, PgBudgetLedger(app_engine, CAPS)).call(step))
    asyncio.run(
        gateway(fake, PgBudgetLedger(app_engine, CAPS)).call(
            step.model_copy(update={"system": "Diagnose again."})
        )
    )
    asyncio.run(
        gateway(fake, PgBudgetLedger(app_engine, CAPS)).call(
            step.model_copy(update={"incident_id": other})
        )
    )
    assert fake.calls == 3


def test_an_invalid_result_is_not_reused(app_engine, catalogue):
    inc = new_incident(app_engine, catalogue)
    bad = Fake(
        {
            "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": "not json"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
    )
    step = request(inc)
    assert (
        asyncio.run(gateway(bad, PgBudgetLedger(app_engine, CAPS)).call(step)).status
        == ResultStatus.INVALID_OUTPUT
    )
    good = Fake()
    assert asyncio.run(gateway(good, PgBudgetLedger(app_engine, CAPS)).call(step)).status == (
        ResultStatus.OK
    )
    assert bad.calls == 3 and good.calls == 1
    with app_engine.connect() as conn:
        rows = conn.execute(
            rb.model_attempts.select().where(rb.model_attempts.c.incident_id == UUID(inc))
        ).all()
    assert sorted((r.status, r.result is not None) for r in rows) == [
        ("invalid_output", False),
        ("invalid_output", False),
        ("invalid_output", False),
        ("ok", True),
    ]


def test_migration_0004_keeps_results_on_ok_rows_only(app_engine):
    with app_engine.connect() as conn:
        found = conn.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
                " WHERE conname = 'model_attempts_result_ok'"
            )
        ).scalar_one()
    assert "result IS NULL" in found and "'ok'" in found
