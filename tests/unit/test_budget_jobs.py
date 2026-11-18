"""T1.21 (WR-26, WR-27): reservation expiry and spend reconciliation run on their intervals.

Time-skipped: the Temporal test server jumps the clock, so a minute's sweep and the daily
reconciliation are observed over days in a second. The activities run over the in-memory
ledger; tests/integration/test_budget_ledger.py covers the same ledger methods in PostgreSQL.
"""

from __future__ import annotations

import os
import time
from datetime import timedelta

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from aisre.gateway.gateway import BudgetLedger, Reservation
from aisre.gateway.gateway_contracts import Usage
from aisre.gateway.jobs import BudgetJobActivities, JobOut
from aisre.workflow import activities as act
from aisre.workflow.budget_jobs import (
    EXPIRY_EVERY,
    EXPIRY_JOB,
    RECONCILIATION_EVERY,
    RECONCILIATION_JOB,
    BudgetJobWorkflow,
    ensure_budget_jobs,
    jobs,
)

TEST_SERVER = os.environ.get("TEMPORAL_TEST_SERVER")  # None: download, as test_workflow.py does
QUEUE = act.INVESTIGATION_QUEUE


class CountingLedger(BudgetLedger):
    """The in-memory ledger, counting each job's runs; `fail_expiry` breaks the sweep once."""

    def __init__(self) -> None:
        super().__init__()
        self.expiries = self.reconciliations = 0
        self.fail_expiry = 0

    async def expire(self) -> list[str]:
        self.expiries += 1
        if self.fail_expiry:
            self.fail_expiry -= 1
            raise RuntimeError("database unreachable")
        return await super().expire()

    async def pending_reconciliation(self) -> list[str]:
        self.reconciliations += 1
        return await super().pending_reconciliation()


def env():
    return WorkflowEnvironment.start_time_skipping(
        test_server_existing_path=TEST_SERVER, data_converter=act.DATA_CONVERTER
    )


def test_the_intervals_are_a_minute_and_a_day():
    assert (EXPIRY_EVERY, RECONCILIATION_EVERY) == (timedelta(minutes=1), timedelta(days=1))
    assert [(i, j.activity) for i, j in jobs()] == [
        (EXPIRY_JOB, "expire_reservations"),
        (RECONCILIATION_JOB, "reconcile_spend"),
    ]


@pytest.mark.asyncio
async def test_expiry_runs_every_minute_and_reconciliation_every_day():
    ledger = CountingLedger()
    async with await env() as e:
        acts = BudgetJobActivities(ledger).all()
        async with Worker(e.client, task_queue=QUEUE, workflows=[BudgetJobWorkflow], activities=acts):  # fmt: skip
            await ensure_budget_jobs(e.client, QUEUE)
            await e.sleep(timedelta(minutes=10, seconds=30))
            assert ledger.expiries == 11 and ledger.reconciliations == 1
            await e.sleep(timedelta(days=1))
            assert ledger.reconciliations == 2 and ledger.expiries >= 11 + 24 * 60
            # Called again, as on every worker start: the running jobs are kept, not doubled.
            await ensure_budget_jobs(e.client, QUEUE)
            before = ledger.reconciliations
            await e.sleep(timedelta(days=1))
            assert ledger.reconciliations == before + 1


@pytest.mark.asyncio
async def test_the_sweep_charges_a_sent_reservation_and_releases_an_unsent_one():
    ledger = BudgetLedger()
    past = time.time() - 1
    sent = await ledger.reserve("r-sent", "inc-1", 100, 100, 5_000, past)
    await ledger.reserve("r-unsent", "inc-1", 100, 100, 7_000, past)
    sent.sent_at = past
    async with await env() as e:
        acts = BudgetJobActivities(ledger).all()
        async with Worker(e.client, task_queue=QUEUE, workflows=[BudgetJobWorkflow], activities=acts):  # fmt: skip
            await ensure_budget_jobs(e.client, QUEUE)
            await e.sleep(timedelta(minutes=2))
    b = ledger.get("inc-1")
    assert not ledger.open and (b.calls, b.spent_cost, b.reserved_cost) == (1, 5_000, 0)
    assert b.pending_reconcile == ["r-sent"]


@pytest.mark.asyncio
async def test_reconciliation_corrects_what_the_usage_source_finds():
    ledger = BudgetLedger()
    r: Reservation = await ledger.reserve("r-1", "inc-1", 100, 100, 9_000, time.time() + 600)
    await ledger.mark_sent(r)
    await ledger.settle(r, Usage(reported=False), 0)
    asked: list[list[str]] = []

    async def source(ids: list[str]) -> dict[str, tuple[Usage, int]]:
        asked.append(ids)
        return {"r-1": (Usage(input_tokens=80, output_tokens=20), 2_500)}

    out = await BudgetJobActivities(ledger, source).reconcile_spend()
    assert asked == [["r-1"]] and out == JobOut(1, 0)
    assert ledger.get("inc-1").spent_cost == 2_500 and not ledger.unreconciled
    assert await BudgetJobActivities(ledger, source).reconcile_spend() == JobOut(0)


@pytest.mark.asyncio
async def test_a_failed_sweep_does_not_stop_the_job():
    ledger = CountingLedger()
    ledger.fail_expiry = 3  # every retry of the first run fails
    async with await env() as e:
        acts = BudgetJobActivities(ledger).all()
        async with Worker(e.client, task_queue=QUEUE, workflows=[BudgetJobWorkflow], activities=acts):  # fmt: skip
            await ensure_budget_jobs(e.client, QUEUE)
            await e.sleep(timedelta(minutes=3, seconds=30))
            handle = e.client.get_workflow_handle(EXPIRY_JOB)
            assert (await handle.describe()).status.name == "RUNNING"
    assert ledger.expiries >= 3 + 3  # the failed run's attempts, then the next runs


def test_the_worker_registers_the_jobs_beside_the_investigation_activities():
    from aisre.workflow import worker

    chosen = worker.budget_job_activities(object())  # type: ignore[arg-type]
    assert [a.__name__ for a in chosen] == ["expire_reservations", "reconcile_spend"]
    assert all(getattr(a, "__self__", None) is not None for a in chosen)
