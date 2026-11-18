"""Artifact 10.5, 10.8 (T1.21, WR-26, WR-27): the ledger's two scheduled jobs, as activities.

`expire_reservations` is the sweep over `expires_at` (ADR-61): a reservation still open after
its call's deadline and grace is charged in full if it was sent, released if it never was.
`reconcile_spend` is the daily job (Q10.35): it asks a usage source for the provider's figures
for each reservation settled with unknown usage, and records a correction for each one found.

aisre.workflow.budget_jobs runs each on its interval. Both are idempotent: a sweep touches only
open rows past their expiry, and a correction is recorded at most once per reservation.

No source of the provider's per-request usage exists yet (the Usage and Cost API reports by
period and model, not by request), so the default source finds nothing: unmatched charges stay
charged in full, which never under-counts, and the count is logged; above 20 it is the
AisreUnreconciledUsage condition (Artifact 12).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from temporalio import activity

from aisre.gateway.gateway import UNRECONCILED_ALERT_ABOVE
from aisre.gateway.gateway_contracts import Usage

log = logging.getLogger("aisre.gateway.jobs")

UsageRecords = dict[str, tuple[Usage, int]]  # reservation ID -> provider usage and cost
UsageSource = Callable[[list[str]], Awaitable[UsageRecords]]


class SweptLedger(Protocol):
    async def expire(self) -> list[str]: ...
    async def pending_reconciliation(self) -> list[str]: ...
    async def reconcile(self, records: UsageRecords) -> list[str]: ...


@dataclass
class JobOut:
    done: int  # reservations expired, or charges corrected
    left: int = 0  # still unreconciled


async def no_usage_records(reservation_ids: list[str]) -> UsageRecords:
    return {}


class BudgetJobActivities:
    def __init__(self, ledger: SweptLedger, usage: UsageSource = no_usage_records) -> None:
        self.ledger, self.usage = ledger, usage

    @activity.defn(name="expire_reservations")
    async def expire_reservations(self) -> JobOut:
        done = await self.ledger.expire()
        if done:
            log.info("expired %d reservations", len(done))
        return JobOut(len(done))

    @activity.defn(name="reconcile_spend")
    async def reconcile_spend(self) -> JobOut:
        pending = await self.ledger.pending_reconciliation()
        if not pending:
            return JobOut(0)
        records = await self.usage(pending)
        left = await self.ledger.reconcile(records)
        level = logging.WARNING if len(left) > UNRECONCILED_ALERT_ABOVE else logging.INFO
        log.log(level, "spend reconciliation: %d corrected, %d unmatched", len(pending) - len(left), len(left))  # fmt: skip
        return JobOut(len(pending) - len(left), len(left))

    def all(self) -> list[Callable[[], Awaitable[JobOut]]]:
        return [self.expire_reservations, self.reconcile_spend]
