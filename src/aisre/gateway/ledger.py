"""Artifact 10.5, 10.8 (T1.7): the durable budget ledger, behind gateway.BudgetLedger's methods.

Every method is one transaction in aisre.record.budget, run on a thread (aisre.record's rule
for async callers), so limits hold across workers and survive a restart: a reservation is a
row before the call leaves, `sent_at` is written before it leaves, and settlement, release,
expiry and reconciliation move money only through the schema's triggers. Fleet limits are
read from the derived daily and monthly totals before each call (ADR-55, ADR-56).
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Optional, TypeVar
from uuid import UUID

from sqlalchemy import Connection, Engine

from aisre.gateway.adapters import sha256
from aisre.gateway.gateway import (
    DAILY_LIMIT_MICROS,
    MONTHLY_CEILING_MICROS,
    AttemptRecord,
    BudgetExceeded,
    IncidentBudget,
    Reservation,
)
from aisre.gateway.gateway_contracts import ModelResult, ResultStatus, Usage
from aisre.record import budget
from aisre.record.incidents import uuid7

T = TypeVar("T")

FLEET_ALERT_PCT = {"daily": (80, 100), "monthly": (50, 80, 100)}  # as FleetBudget


def _at(epoch_s: float) -> datetime:
    return datetime.fromtimestamp(epoch_s, UTC)


def default_caps() -> budget.Caps:
    b = IncidentBudget()  # the one configuration object (ADR-52)
    return budget.Caps(b.max_calls, b.max_input_tokens, b.max_output_tokens, b.max_cost_micros)


class PgBudgetLedger:
    def __init__(
        self,
        engine: Engine,
        caps: Optional[budget.Caps] = None,
        daily_limit: int = DAILY_LIMIT_MICROS,
        monthly_limit: int = MONTHLY_CEILING_MICROS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.engine = engine
        self.caps = caps or default_caps()
        self.daily_limit, self.monthly_limit = daily_limit, monthly_limit
        self.clock = clock

    async def _tx(self, fn: Callable[[Connection], T]) -> T:
        def run() -> T:
            with self.engine.begin() as conn:
                return fn(conn)

        return await asyncio.to_thread(run)

    async def reserve(
        self, rid: str, incident_id: str, in_tok: int, out_tok: int, cost: int, expires_at: float
    ) -> Reservation:
        def go(conn: Connection) -> None:
            budget.reserve(
                conn,
                reservation_id=UUID(rid),
                incident_id=UUID(incident_id),
                caps=self.caps,
                input_tokens=in_tok,
                output_tokens=out_tok,
                cost_micros=cost,
                expires_at=_at(expires_at),
            )

        try:
            await self._tx(go)
        except budget.OverBudget as e:
            raise BudgetExceeded(str(e)) from None
        return Reservation(rid, incident_id, cost, in_tok, out_tok, expires_at)

    async def mark_sent(self, r: Reservation) -> bool:
        now = self.clock()
        if not await self._tx(lambda c: budget.mark_sent(c, UUID(r.reservation_id), _at(now))):
            return False
        r.sent_at = now
        return True

    async def settle(self, r: Reservation, usage: Usage, actual_cost: int) -> None:
        r.charged_at = now = self.clock()
        if usage.reported:
            tin: Optional[int] = (
                usage.input_tokens + usage.cache_read_input_tokens + usage.cache_write_input_tokens
            )
            tout: Optional[int] = usage.output_tokens
            cost = actual_cost
        else:  # Q10.35/Q10.36: charged in full, reconciled later; never released as free
            tin = tout = None
            cost = r.cost
        await self._tx(
            lambda c: budget.settle(
                c,
                UUID(r.reservation_id),
                cost_micros=cost,
                input_tokens=tin,
                output_tokens=tout,
                at=_at(now),
            )
        )

    async def release_unsent(self, r: Reservation) -> None:
        now = self.clock()
        await self._tx(lambda c: budget.release(c, UUID(r.reservation_id), _at(now)))

    async def expire(self) -> list[str]:
        """The scheduled sweep (T1.21). Returns the reservations it settled or released."""
        now = self.clock()
        done = await self._tx(lambda c: budget.expire(c, _at(now)))
        return [str(e.reservation_id) for e in done]

    async def reconcile(self, records: dict[str, tuple[Usage, int]]) -> list[str]:
        """The daily job (Q10.35). `records` maps reservation ID to the provider's usage and
        cost; each match becomes a correction. Returns what is still unmatched."""

        def go(conn: Connection) -> list[str]:
            left = []
            for rid in budget.unreconciled(conn):
                found = records.get(str(rid))
                if found is None:
                    left.append(str(rid))
                    continue
                usage, cost = found
                budget.correct(
                    conn,
                    rid,
                    cost_micros=cost,
                    input_tokens=usage.input_tokens
                    + usage.cache_read_input_tokens
                    + usage.cache_write_input_tokens,
                    output_tokens=usage.output_tokens,
                    reason="provider usage records",
                )
            return left

        return await self._tx(go)

    async def unreconciled(self) -> list[str]:
        return [str(r) for r in await self._tx(budget.unreconciled)]

    async def fleet_spend(self, now: float) -> tuple[int, int]:
        return await self._tx(lambda c: budget.fleet_spend(c, _at(now)))

    async def evidence_only(self, now: float) -> bool:
        day, month = await self.fleet_spend(now)
        return day >= self.daily_limit or month >= self.monthly_limit

    async def fleet_alerts(self, now: float) -> list[str]:
        """The levels crossed in the current day and month, named as FleetBudget names them."""
        day, month = await self.fleet_spend(now)
        d, m = _at(now).strftime("%Y-%m-%d"), _at(now).strftime("%Y-%m")
        out = []
        for scope, period, spent, limit in (
            ("daily", d, day, self.daily_limit),
            ("monthly", m, month, self.monthly_limit),
        ):
            out += [
                f"{scope}_spend_{pct}:{period}"
                for pct in FLEET_ALERT_PCT[scope]
                if spent * 100 >= limit * pct
            ]
        return out

    async def reusable(self, incident_id: str, input_sha256: str) -> Optional[ModelResult]:
        """ADR-61, T1.8: the recorded result of this step input, from model_attempts."""
        found = await self._tx(lambda c: budget.reusable_result(c, UUID(incident_id), input_sha256))
        return None if found is None else ModelResult.model_validate(found)

    async def record(self, a: AttemptRecord) -> None:
        r, req, ident = a.result, a.req, a.result.identity
        row = budget.Attempt(
            model_attempt_id=uuid7(),
            incident_id=UUID(req.incident_id),
            reservation_id=UUID(a.reservation_id) if a.reservation_id else None,
            task=req.task,
            step_attempt_no=min(3, max(1, r.attempt)),
            provider=a.provider,
            model=a.model,
            checkpoint=ident.checkpoint if ident else None,
            adapter_version=a.adapter_version,
            prompt_template_version=req.prompt_version,
            prompt_sha256=ident.prompt_sha256
            if ident
            else hashlib.sha256(req.system.encode()).hexdigest(),
            output_schema_sha256=(ident.output_schema_sha256 if ident else None)
            or (sha256(req.output_schema) if req.output_schema else None),
            input_sha256=r.input_sha256 or sha256(req.model_dump(mode="json")),
            status=r.status.value,
            stop_reason=r.stop_reason.value,
            validation_errors=list(r.validation_errors) or None,
            provider_request_id=r.provider_request_id,
            input_tokens=r.usage.input_tokens,
            output_tokens=r.usage.output_tokens,
            cache_read_input_tokens=r.usage.cache_read_input_tokens,
            cache_write_input_tokens=r.usage.cache_write_input_tokens,
            usage_reported=r.usage.reported,
            cost_micros=r.cost_micros,
            latency_ms=r.latency_ms,
            started_at=_at(a.started_at),
            finished_at=_at(a.finished_at),
            result=r.model_dump(mode="json") if r.status == ResultStatus.OK else None,
        )
        await self._tx(lambda c: budget.record_attempt(c, row))
