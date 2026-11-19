"""Artifact 12.7, 13.5 T4.8 (NFR-20, NFR-45): the daily retention job.

`aisre-retention` is the same timer job as the audit and budget jobs (BudgetJobWorkflow): it
runs `run_retention` (aisre.record.retention, as aisre_retention), sleeps a day, and continues
as new. The worker starts it only when AISRE_RETENTION_DATABASE_URL is set: no other role may
delete. Each run logs its report on `aisre.audit` as `aisre.retention.run`, then the audit
query's counts, and sets `aisre_retention_overdue{class}` from them.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import asdict
from datetime import timedelta
from typing import Any

from sqlalchemy import Engine
from temporalio import activity
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy

from aisre.record import retention
from aisre.reliability import telemetry
from aisre.workflow.budget_jobs import BudgetJobIn, BudgetJobWorkflow

JOB = "aisre-retention"
EVERY = timedelta(days=1)

log = logging.getLogger("aisre.audit")


class RetentionActivities:
    def __init__(self, engine: Engine, delete_object: retention.DeleteObject | None) -> None:
        self.engine, self.delete_object = engine, delete_object

    def all(self) -> list[Callable[..., Any]]:
        return [self.run_retention]

    def _run(self) -> dict[str, Any]:
        report = retention.run(self.engine, self.delete_object)
        with self.engine.connect() as conn:
            left = retention.overdue(conn)
        for cls, n in left.items():
            telemetry.retention_overdue.labels(**{"class": cls}).set(n)
        out = {"event": "aisre.retention.run", **asdict(report), "overdue_after": left}
        log.info(json.dumps(out, sort_keys=True))
        return out

    @activity.defn(name="run_retention")
    async def run_retention(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._run)


async def ensure_retention_job(client: Client, task_queue: str) -> None:
    await client.start_workflow(
        BudgetJobWorkflow.run,
        BudgetJobIn("run_retention", EVERY.total_seconds()),
        id=JOB,
        task_queue=task_queue,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
    )
