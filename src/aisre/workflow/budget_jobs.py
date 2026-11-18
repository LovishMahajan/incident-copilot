"""Artifact 10.5, 10.8 (T1.21, WR-26, WR-27): scheduling for reservation expiry and spend
reconciliation.

Each job is one long-running BudgetJobWorkflow with a fixed ID, which runs its activity and
then sleeps for its interval, continuing as new every RUNS_PER_RUN runs so its history stays
small. A durable timer is the schedule: it survives worker restarts like the rest of Temporal
state, and, unlike a Temporal Schedule, it runs under the time-skipping test server, so the
intervals themselves are tested. `ensure_budget_jobs` starts both and is safe to call on every
worker start: a running job is left as it is.

Deterministic: the workflow only sleeps and calls activities by name (aisre.gateway.jobs).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy, WorkflowIDConflictPolicy
from temporalio.exceptions import ActivityError

EXPIRY_JOB = "aisre-reservation-expiry"
RECONCILIATION_JOB = "aisre-spend-reconciliation"
EXPIRY_EVERY = timedelta(minutes=1)  # provisional: reservations expire 60 s after a deadline
RECONCILIATION_EVERY = timedelta(days=1)  # Q10.35: the daily job
RUNS_PER_RUN = 200  # then continue as new
JOB_RETRY = RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=5))


@dataclass
class BudgetJobIn:
    activity: str  # "expire_reservations" | "reconcile_spend"
    every_s: float
    runs: int = 0  # runs in earlier continued-as-new executions


@workflow.defn
class BudgetJobWorkflow:
    @workflow.run
    async def run(self, inp: BudgetJobIn) -> None:
        every = timedelta(seconds=inp.every_s)
        for _ in range(RUNS_PER_RUN):
            try:
                await workflow.execute_activity(
                    inp.activity,
                    start_to_close_timeout=min(every, timedelta(minutes=5)),
                    retry_policy=JOB_RETRY,
                )
            except ActivityError:  # the next run tries again; the job itself never ends
                workflow.logger.warning("%s failed; next run in %s", inp.activity, every)
            inp.runs += 1
            await workflow.sleep(every)
        workflow.continue_as_new(inp)


def jobs() -> list[tuple[str, BudgetJobIn]]:
    return [
        (EXPIRY_JOB, BudgetJobIn("expire_reservations", EXPIRY_EVERY.total_seconds())),
        (
            RECONCILIATION_JOB,
            BudgetJobIn("reconcile_spend", RECONCILIATION_EVERY.total_seconds()),
        ),
    ]


async def ensure_budget_jobs(client: Client, task_queue: str) -> None:
    for job_id, inp in jobs():
        await client.start_workflow(
            BudgetJobWorkflow.run,
            inp,
            id=job_id,
            task_queue=task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )
