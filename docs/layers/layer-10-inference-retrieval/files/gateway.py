"""Artifact 10.5–10.8: the Model Gateway — budget, concurrency, retries, deadlines, accounting.

The only component that calls a model (Artifact 6). For each call:
  capability check → deadline check → budget reservation → concurrency slot → provider call
  → classification → output validation → settlement → (bounded retry)
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from pydantic import BaseModel, ValidationError

from adapters import HttpResponse
from gateway_contracts import (RETRYABLE, ModelRequest, ModelResult, ResultStatus, StopReason, TextBlock,
                               Usage)


# ---------------------------------------------------------------------------
# Pricing and token estimates (10.8)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Price:
    """Money in millionths of the currency unit per million tokens. Values come from configuration."""
    input_per_mtok: int
    output_per_mtok: int
    cache_read_per_mtok: int
    cache_write_per_mtok: int


def estimate_tokens(text: str) -> int:
    """Conservative pre-call estimate: about 3 characters per token (real text averages nearer 4).
    Used only to reserve budget; the provider's reported usage settles it."""
    return math.ceil(len(text) / 3) + 16


def cost_micros(u: Usage, p: Price) -> int:
    return (u.input_tokens * p.input_per_mtok + u.output_tokens * p.output_per_mtok
            + u.cache_read_input_tokens * p.cache_read_per_mtok
            + u.cache_write_input_tokens * p.cache_write_per_mtok) // 1_000_000


# ---------------------------------------------------------------------------
# Budget (10.5): reserve before, settle after; never release on unknown usage
# ---------------------------------------------------------------------------
class BudgetExceeded(Exception):
    pass


@dataclass
class IncidentBudget:
    max_calls: int = 30
    max_input_tokens: int = 200_000
    max_output_tokens: int = 20_000
    max_cost_micros: int = 2_000_000
    calls: int = 0
    reserved_cost: int = 0
    spent_cost: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    pending_reconcile: list[str] = field(default_factory=list)


@dataclass
class Reservation:
    reservation_id: str
    incident_id: str
    cost: int
    input_tokens: int
    output_tokens: int


class BudgetLedger:
    """In-memory stand-in for incident_budgets / budget_reservations (Artifact 7). The production
    implementation does the same arithmetic under a row lock in one transaction."""

    def __init__(self, default: Callable[[], IncidentBudget] = IncidentBudget):
        self.budgets: dict[str, IncidentBudget] = {}
        self.default = default
        self._lock = asyncio.Lock()

    def get(self, incident_id: str) -> IncidentBudget:
        return self.budgets.setdefault(incident_id, self.default())

    async def reserve(self, rid: str, incident_id: str, in_tok: int, out_tok: int, cost: int) -> Reservation:
        async with self._lock:
            b = self.get(incident_id)
            if b.calls + 1 > b.max_calls:
                raise BudgetExceeded("model call cap")
            if b.input_tokens + in_tok > b.max_input_tokens or b.output_tokens + out_tok > b.max_output_tokens:
                raise BudgetExceeded("token cap")
            if b.spent_cost + b.reserved_cost + cost > b.max_cost_micros:
                raise BudgetExceeded("spend cap")
            b.calls += 1
            b.reserved_cost += cost
            b.input_tokens += in_tok           # held at the estimate until settled
            b.output_tokens += out_tok
            return Reservation(rid, incident_id, cost, in_tok, out_tok)

    async def settle(self, r: Reservation, usage: Usage, actual_cost: int) -> None:
        async with self._lock:
            b = self.get(r.incident_id)
            b.reserved_cost -= r.cost
            if usage.reported:
                b.spent_cost += actual_cost
                b.input_tokens += usage.input_tokens + usage.cache_read_input_tokens + usage.cache_write_input_tokens - r.input_tokens
                b.output_tokens += usage.output_tokens - r.output_tokens
            else:
                # Q10.35/Q10.36: tokens may have been consumed. Charge the full reservation and flag it
                # for reconciliation against the provider's usage records; never release it as free.
                b.spent_cost += r.cost
                b.pending_reconcile.append(r.reservation_id)

    async def release_unsent(self, r: Reservation) -> None:
        """Only for a call that provably never left the process (e.g. no concurrency slot before the deadline)."""
        async with self._lock:
            b = self.get(r.incident_id)
            b.reserved_cost -= r.cost
            b.calls -= 1
            b.input_tokens -= r.input_tokens
            b.output_tokens -= r.output_tokens


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------
Transport = Callable[[str, dict[str, Any], float], Awaitable[tuple[HttpResponse, bool]]]
# (provider, body, timeout_s) -> (response, stream_completed). Raises asyncio.TimeoutError on timeout.


@dataclass
class GatewayConfig:
    max_attempts: int = 3                      # per step, including the first (Artifact 4)
    global_concurrency: int = 8                # across all incidents, per provider (10.6)
    per_incident_concurrency: int = 2
    backoff_s: tuple[float, ...] = (2.0, 4.0)
    allow_cross_provider_fallback: bool = False   # Q10.39: off


class ModelGateway:
    def __init__(self, adapter, transport: Transport, ledger: BudgetLedger, price: Price,
                 schemas: dict[str, type[BaseModel]], config: GatewayConfig = GatewayConfig(),
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 clock: Callable[[], float] = time.time):
        self.adapter, self.transport, self.ledger, self.price = adapter, transport, ledger, price
        self.schemas, self.cfg, self.sleep, self.clock = schemas, config, sleep, clock
        self.global_slots = asyncio.Semaphore(config.global_concurrency)
        self.incident_slots: dict[str, asyncio.Semaphore] = {}
        self.attempt_log: list[ModelResult] = []

    async def call(self, req: ModelRequest) -> ModelResult:
        missing = [c for c in req.required_capabilities if c not in self.adapter.capabilities]
        if missing:                                                    # Q10.22: refuse, don't degrade silently
            return self._final(req, 0, ResultStatus.CAPABILITY_UNSUPPORTED, f"missing {[m.value for m in missing]}")

        attempt, current, last = 0, req, None
        while attempt < self.cfg.max_attempts:
            attempt += 1
            if self.clock() >= req.deadline_epoch_s:                   # Q10.32
                return self._final(req, attempt, ResultStatus.DEADLINE_PASSED, "investigation deadline passed")
            last = await self._attempt(current, attempt)
            if last.status == ResultStatus.OK:
                return last
            if last.status == ResultStatus.TRUNCATED and current.max_output_tokens < 16_000:
                # One larger retry for an output that hit the limit; the budget still applies.
                current = current.model_copy(update={"max_output_tokens": min(16_000, current.max_output_tokens * 2)})
                continue
            if last.status not in RETRYABLE:                           # auth, bad request, refusal, budget...
                return last
            if last.status == ResultStatus.INVALID_OUTPUT:             # tell the model what was wrong
                current = current.model_copy(update={"context": list(current.context) + [{
                    "ref": f"validation:{attempt}", "kind": "validation_error",
                    "text": "Your previous answer failed validation: " + "; ".join(last.validation_errors[:5])}]})
            if attempt < self.cfg.max_attempts:
                wait = self.cfg.backoff_s[min(attempt - 1, len(self.cfg.backoff_s) - 1)]
                if self.clock() + wait >= req.deadline_epoch_s:
                    return last
                await self.sleep(wait)
        return last

    async def _attempt(self, req: ModelRequest, attempt: int) -> ModelResult:
        body = self.adapter.build(req)
        in_est = estimate_tokens(json.dumps(body))
        reserve_cost = cost_micros(Usage(input_tokens=in_est, output_tokens=req.max_output_tokens), self.price)
        rid = f"{req.request_id}:a{attempt}"
        try:
            res = await self.ledger.reserve(rid, req.incident_id, in_est, req.max_output_tokens, reserve_cost)
        except BudgetExceeded as e:
            return self._final(req, attempt, ResultStatus.BUDGET_REFUSED, str(e))

        slots = self.incident_slots.setdefault(req.incident_id, asyncio.Semaphore(self.cfg.per_incident_concurrency))
        remaining = req.deadline_epoch_s - self.clock()
        try:
            await asyncio.wait_for(self._acquire(slots), timeout=max(0.0, remaining))
        except asyncio.TimeoutError:
            await self.ledger.release_unsent(res)                       # never sent: safe to release
            return self._final(req, attempt, ResultStatus.DEADLINE_PASSED, "no capacity before deadline")
        started = self.clock()
        try:
            timeout = min(req.call_timeout_s, max(0.1, req.deadline_epoch_s - self.clock()))   # Q10.33
            try:
                resp, completed = await asyncio.wait_for(
                    self.transport(self.adapter.provider, body, timeout), timeout=timeout)
            except asyncio.TimeoutError:
                await self.ledger.settle(res, Usage(reported=False), 0)  # may have consumed tokens
                return self._final(req, attempt, ResultStatus.TIMEOUT, f"no response in {timeout:.0f}s")
        finally:
            slots.release()
            self.global_slots.release()

        result = self.adapter.parse(req, attempt, resp, int((self.clock() - started) * 1000), completed)
        actual = cost_micros(result.usage, self.price) if result.usage.reported else res.cost
        await self.ledger.settle(res, result.usage, actual)
        result = result.model_copy(update={"cost_micros": actual})
        if result.status == ResultStatus.OK and req.output_schema_name:
            result = self._validate(req, result)
        self.attempt_log.append(result)          # every attempt is recorded (model_attempts, Artifact 7)
        return result

    async def _acquire(self, slots: asyncio.Semaphore) -> None:
        await slots.acquire()
        try:
            await self.global_slots.acquire()
        except BaseException:
            slots.release()
            raise

    def _validate(self, req: ModelRequest, result: ModelResult) -> ModelResult:
        """Q10.20, Q10.21: parse, then validate against the skill's Pydantic contract."""
        text = "".join(b.text for b in result.content if isinstance(b, TextBlock)).strip()
        model = self.schemas[req.output_schema_name]
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            return result.model_copy(update={"status": ResultStatus.INVALID_OUTPUT,
                                             "validation_errors": [f"not valid JSON: {e.msg} at {e.pos}"]})
        try:
            parsed = model.model_validate(data)
        except ValidationError as e:
            return result.model_copy(update={"status": ResultStatus.INVALID_OUTPUT,
                                             "validation_errors": [f'{".".join(map(str, x["loc"]))}: {x["msg"]}'
                                                                   for x in e.errors()]})
        return result.model_copy(update={"parsed": parsed.model_dump(mode="json")})

    def _final(self, req: ModelRequest, attempt: int, status: ResultStatus, error: str) -> ModelResult:
        r = ModelResult(request_id=req.request_id, attempt=attempt, status=status,
                        stop_reason=StopReason.UNKNOWN, error=error, usage=Usage(reported=True))
        self.attempt_log.append(r)
        return r
