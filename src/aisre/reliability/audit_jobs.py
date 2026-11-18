"""Artifact 5 (ST-32), 12.1, 12.3 (T1.20, WR-30): copying the audit trail, and the weekly check.

Two long-running timer jobs, the same BudgetJobWorkflow as reservation expiry and spend
reconciliation (aisre.workflow.budget_jobs): it runs its activity, sleeps its interval, and
continues as new. A durable timer, not a Temporal Schedule, so the time-skipping test server
runs the intervals too.

    aisre-audit-copy         every minute   copy_audit_trail: new timeline events to the
                                            write-once copy (aisre.record.audit_copy)
    aisre-audit-chain-check  every 7 days   check_audit_chain: verify_timeline_chain on every
                                            incident, and the record against the copy

The check sets `aisre_audit_chain_faults` to the number of incidents with any fault, which
`AisreAuditChainBroken` (ops/alerts.yml) pages on, and logs each fault on `aisre.audit` as
`aisre.audit.chain_fault`. The gauge lives in the worker that ran the check, which serves it on
its metrics port. Without a store configured, the copy does nothing and the check still runs
the chain verification.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import Engine
from temporalio import activity
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy

from aisre.record.audit_copy import Fault, WriteOnceStore, check_chain, copy_timelines
from aisre.reliability import telemetry
from aisre.workflow.budget_jobs import BudgetJobIn, BudgetJobWorkflow

COPY_JOB = "aisre-audit-copy"
CHECK_JOB = "aisre-audit-chain-check"
COPY_EVERY = timedelta(minutes=1)
CHECK_EVERY = timedelta(days=7)  # WR-30: weekly

log = logging.getLogger("aisre.audit")


class AuditJobActivities:
    def __init__(self, engine: Engine, store: WriteOnceStore | None) -> None:
        self.engine, self.store = engine, store

    def all(self) -> list[Callable[..., Any]]:
        return [self.copy_audit_trail, self.check_audit_chain]

    @activity.defn(name="copy_audit_trail")
    async def copy_audit_trail(self) -> int:
        if self.store is None:
            return 0
        store = self.store
        return await asyncio.to_thread(copy_timelines, self.engine, store)

    @activity.defn(name="check_audit_chain")
    async def check_audit_chain(self) -> int:
        """Returns, and sets the gauge to, the number of incidents with a fault."""
        faults = await asyncio.to_thread(self.faults)
        broken = len({f.incident_id for f in faults})
        telemetry.audit_chain_faults.set(broken)
        for f in faults:
            log.error(
                json.dumps(
                    {
                        "event": "aisre.audit.chain_fault",
                        "incident_id": f.incident_id,
                        "at_seq": f.at_seq,
                        "problem": f.problem,
                    },
                    sort_keys=True,
                )
            )
        return broken

    def faults(self) -> list[Fault]:
        with self.engine.connect() as conn:
            return check_chain(conn, self.store)


def jobs() -> list[tuple[str, BudgetJobIn]]:
    return [
        (COPY_JOB, BudgetJobIn("copy_audit_trail", COPY_EVERY.total_seconds())),
        (CHECK_JOB, BudgetJobIn("check_audit_chain", CHECK_EVERY.total_seconds())),
    ]


async def ensure_audit_jobs(client: Client, task_queue: str) -> None:
    """Start both jobs unless they are running. Safe on every worker start."""
    for job_id, inp in jobs():
        await client.start_workflow(
            BudgetJobWorkflow.run,
            inp,
            id=job_id,
            task_queue=task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )
