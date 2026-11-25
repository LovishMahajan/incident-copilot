"""Artifact 10.5–10.8: the Model Gateway — budget, concurrency, retries, deadlines, accounting.

The only component that calls a model (Artifact 6). For each call:
  capability check → result reuse → fleet spend check → then for each attempt: deadline check
  → circuit check → per-call input limit → budget reservation → concurrency slot → provider call
  → classification → output validation → settlement → (bounded retry)

Output validation includes ADR-59's check (validation.check_output): an answer holding a secret,
a link or a command is `invalid_output` with the error "unsafe model output: <kind>", its text
blocks masked, and it is never retried, since a retry would resend the context that produced it.
So the attempt's record keeps no unmasked copy and nothing reusable (ST-24).

Fleet limits are checked once per call, not per attempt: a call is one step, so a step under way
finishes (ADR-55) and the next one is refused.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional, Protocol

from pydantic import BaseModel, ValidationError

from aisre.agent.coordinator import Limits
from aisre.agent.validation import UnsafeOutput, check_output, mask_secrets
from aisre.gateway.adapters import HttpResponse, sha256
from aisre.gateway.gateway_contracts import (
    MAX_INPUT_TOKENS_PER_CALL,
    MAX_OUTPUT_TOKENS_PER_CALL,
    RETRYABLE,
    Capability,
    ModelRequest,
    ModelResult,
    ReasoningBlock,
    ResultStatus,
    StopReason,
    TextBlock,
    ToolProposal,
    Usage,
    estimate_tokens,
)
from aisre.reliability import telemetry

UNSAFE_OUTPUT = "unsafe model output: "  # validation error prefix; the kind follows, never the text


def unsafe_kind(result: ModelResult) -> Optional[str]:
    """`secret`, `link` or `command` when the gateway stopped an answer under ADR-59."""
    for e in result.validation_errors:
        if e.startswith(UNSAFE_OUTPUT):
            return e.removeprefix(UNSAFE_OUTPUT)
    return None


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


# Per pinned model, USD. List prices as of 2026-11-02 (10.1); verify at build.
CLAUDE_PRICES = {
    "claude-opus-5-5": Price(4_000_000, 20_000_000, 200_000, 5_000_000),
    "claude-sonnet-5-5": Price(2_000_000, 10_000_000, 100_000, 2_500_000),
}


def cost_micros(u: Usage, p: Price) -> int:
    return (
        u.input_tokens * p.input_per_mtok
        + u.output_tokens * p.output_per_mtok
        + u.cache_read_input_tokens * p.cache_read_per_mtok
        + u.cache_write_input_tokens * p.cache_write_per_mtok
    ) // 1_000_000


# ---------------------------------------------------------------------------
# Budget (10.5): reserve before, settle after; never release on unknown usage
# ---------------------------------------------------------------------------
class BudgetExceeded(Exception):
    pass


LIMITS = Limits()  # the one configuration object (ADR-52)

MONTHLY_CEILING_MICROS = 500_000_000  # $500, ADR-56 (provisional)
DAILY_LIMIT_MICROS = MONTHLY_CEILING_MICROS // 30  # $16.67, ADR-56 (provisional)
UNRECONCILED_ALERT_ABOVE = 20  # same threshold as AisreUnreconciledUsage (Artifact 12)


@dataclass
class IncidentBudget:
    max_calls: int = LIMITS.max_model_calls
    max_input_tokens: int = LIMITS.max_input_tokens
    max_output_tokens: int = LIMITS.max_output_tokens
    max_cost_micros: int = LIMITS.max_cost_micros  # $2.00, ADR-56 (provisional)
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
    expires_at: float = float("inf")
    sent_at: Optional[float] = None  # write-once, just before the call leaves the process (ADR-61)
    charged_at: float = 0.0

    @property
    def sent(self) -> bool:
        return self.sent_at is not None


@dataclass
class FleetBudget:
    """Spend across all incidents, by UTC day and month (ADR-55, ADR-56). At 100% of either,
    `evidence_only` is true: new investigations make no model calls, and running ones stop after
    their current step."""

    daily_limit: int = DAILY_LIMIT_MICROS
    monthly_limit: int = MONTHLY_CEILING_MICROS
    daily_alert_pct: tuple[int, ...] = (80, 100)
    monthly_alert_pct: tuple[int, ...] = (50, 80, 100)
    daily: dict[str, int] = field(default_factory=dict)
    monthly: dict[str, int] = field(default_factory=dict)
    alerts: list[str] = field(default_factory=list)

    @staticmethod
    def _periods(now: float) -> tuple[str, str]:
        d = datetime.fromtimestamp(now, timezone.utc)
        return d.strftime("%Y-%m-%d"), d.strftime("%Y-%m")

    def charge(self, cost: int, now: float) -> None:
        day, month = self._periods(now)
        self.daily[day] = self.daily.get(day, 0) + cost
        self.monthly[month] = self.monthly.get(month, 0) + cost
        self._alert("daily", day, self.daily[day], self.daily_limit, self.daily_alert_pct)
        self._alert(
            "monthly", month, self.monthly[month], self.monthly_limit, self.monthly_alert_pct
        )

    def _alert(
        self, scope: str, period: str, spent: int, limit: int, levels: tuple[int, ...]
    ) -> None:
        for pct in levels:
            name = f"{scope}_spend_{pct}:{period}"
            if spent * 100 >= limit * pct and name not in self.alerts:
                self.alerts.append(name)

    def evidence_only(self, now: float) -> bool:
        day, month = self._periods(now)
        return (
            self.daily.get(day, 0) >= self.daily_limit
            or self.monthly.get(month, 0) >= self.monthly_limit
        )


@dataclass(frozen=True)
class AttemptRecord:
    """One attempt, as the gateway hands it to the ledger to keep (model_attempts, ADR-64)."""

    req: ModelRequest
    result: ModelResult
    reservation_id: Optional[str]
    provider: str
    model: str
    adapter_version: str
    started_at: float
    finished_at: float


class BudgetLedger:
    """In-memory stand-in for incident_budgets / budget_reservations (Artifact 7), for tests and
    the offline evaluation runs. The durable one is aisre.gateway.ledger.PgBudgetLedger, with the
    same methods; the gateway uses only the async ones. `completed` stands in for the lookup of
    model_attempts by input_sha256 (ADR-61)."""

    def __init__(
        self,
        default: Callable[[], IncidentBudget] = IncidentBudget,
        fleet: Optional[FleetBudget] = None,
        clock: Callable[[], float] = time.time,
    ):
        self.budgets: dict[str, IncidentBudget] = {}
        self.default = default
        self.fleet = fleet or FleetBudget()
        self.clock = clock
        self.open: dict[str, Reservation] = {}  # status = 'reserved'
        self.unreconciled: dict[str, Reservation] = {}  # charged in full, awaiting provider records
        self.completed: dict[tuple[str, str], ModelResult] = {}
        self._lock = asyncio.Lock()

    def get(self, incident_id: str) -> IncidentBudget:
        return self.budgets.setdefault(incident_id, self.default())

    async def reserve(
        self,
        rid: str,
        incident_id: str,
        in_tok: int,
        out_tok: int,
        cost: int,
        expires_at: float = float("inf"),
    ) -> Reservation:
        async with self._lock:
            b = self.get(incident_id)
            if b.calls + 1 > b.max_calls:
                raise BudgetExceeded("model call cap")
            if (
                b.input_tokens + in_tok > b.max_input_tokens
                or b.output_tokens + out_tok > b.max_output_tokens
            ):
                raise BudgetExceeded("token cap")
            if b.spent_cost + b.reserved_cost + cost > b.max_cost_micros:
                raise BudgetExceeded("spend cap")
            b.calls += 1
            b.reserved_cost += cost
            b.input_tokens += in_tok  # held at the estimate until settled
            b.output_tokens += out_tok
            r = Reservation(rid, incident_id, cost, in_tok, out_tok, expires_at)
            self.open[rid] = r
            return r

    async def mark_sent(self, r: Reservation) -> bool:
        """Write-once, just before the call leaves. False if expiry released it: don't send."""
        async with self._lock:
            if r.reservation_id not in self.open:
                return False
            r.sent_at = self.clock()
            return True

    async def evidence_only(self, now: float) -> bool:
        return self.fleet.evidence_only(now)

    async def reusable(self, incident_id: str, input_sha256: str) -> Optional[ModelResult]:
        return self.completed.get((incident_id, input_sha256))

    async def record(self, a: AttemptRecord) -> None:
        if a.result.status == ResultStatus.OK and a.result.input_sha256:
            self.completed[(a.req.incident_id, a.result.input_sha256)] = a.result

    async def settle(self, r: Reservation, usage: Usage, actual_cost: int) -> None:
        async with self._lock:
            if self.open.pop(r.reservation_id, None) is not None:  # expiry may have got there first
                self._settle(r, usage, actual_cost)

    def _settle(self, r: Reservation, usage: Usage, actual_cost: int) -> None:
        b = self.get(r.incident_id)
        b.reserved_cost -= r.cost
        r.charged_at = self.clock()
        if usage.reported:
            b.spent_cost += actual_cost
            self._correct_tokens(b, r, usage)
            self.fleet.charge(actual_cost, r.charged_at)
        else:
            # Q10.35/Q10.36: tokens may have been consumed. Charge the full reservation and flag it
            # for reconciliation against the provider's usage records; never release it as free.
            b.spent_cost += r.cost
            b.pending_reconcile.append(r.reservation_id)
            self.unreconciled[r.reservation_id] = r
            self.fleet.charge(r.cost, r.charged_at)

    @staticmethod
    def _correct_tokens(b: IncidentBudget, r: Reservation, usage: Usage) -> None:
        b.input_tokens += (
            usage.input_tokens
            + usage.cache_read_input_tokens
            + usage.cache_write_input_tokens
            - r.input_tokens
        )
        b.output_tokens += usage.output_tokens - r.output_tokens

    async def release_unsent(self, r: Reservation) -> None:
        """Only for a call that provably never left the process (e.g. no concurrency slot before the deadline)."""
        async with self._lock:
            if self.open.pop(r.reservation_id, None) is not None:
                self._release(r)

    def _release(self, r: Reservation) -> None:
        b = self.get(r.incident_id)
        b.reserved_cost -= r.cost
        b.calls -= 1
        b.input_tokens -= r.input_tokens
        b.output_tokens -= r.output_tokens

    async def expire(self) -> list[str]:
        """ADR-61, for a sweep over expires_at. A sent reservation is charged at the maximum and
        queued for reconciliation; only a never-sent one is released."""
        async with self._lock:
            now, done = self.clock(), []
            for r in [r for r in self.open.values() if r.expires_at <= now]:
                del self.open[r.reservation_id]
                if r.sent:
                    self._settle(r, Usage(reported=False), r.cost)
                else:
                    self._release(r)
                done.append(r.reservation_id)
            return done

    async def pending_reconciliation(self) -> list[str]:
        return list(self.unreconciled)

    async def reconcile(self, records: dict[str, tuple[Usage, int]]) -> list[str]:
        """The daily job (Q10.35): `records` maps reservation ID to the usage and cost in the provider's
        records. Corrects the ledger, in the period charged, and returns what is still unmatched."""
        async with self._lock:
            for rid in [rid for rid in self.unreconciled if rid in records]:
                r = self.unreconciled.pop(rid)
                usage, actual = records[rid]
                b = self.get(r.incident_id)
                b.spent_cost += actual - r.cost
                self._correct_tokens(b, r, usage)
                b.pending_reconcile.remove(rid)
                self.fleet.charge(actual - r.cost, r.charged_at)
            if len(self.unreconciled) > UNRECONCILED_ALERT_ABOVE:  # frequent missing usage (10.8)
                self.fleet.alerts.append(f"missing_usage:{len(self.unreconciled)}")
            return list(self.unreconciled)


# ---------------------------------------------------------------------------
# Circuit breaker (10.7, ADR-61, provisional)
# ---------------------------------------------------------------------------
# Provider unavailable, as opposed to a request that was wrong or refused.
BREAKER_FAILURES = {
    ResultStatus.TIMEOUT,
    ResultStatus.RATE_LIMITED,
    ResultStatus.PROVIDER_ERROR,
    ResultStatus.INCOMPLETE,
}


class CircuitBreaker:
    """One per provider: a gateway wraps one adapter. Opens at a share of failures among at least
    `min_calls` sent calls in the window, then stays open for `open_s`."""

    def __init__(
        self,
        clock: Callable[[], float],
        failure_ratio: float,
        min_calls: int,
        window_s: float,
        open_s: float,
        dependency: Optional[str] = None,
    ):
        self.clock, self.failure_ratio, self.min_calls = clock, failure_ratio, min_calls
        self.window_s, self.open_s = window_s, open_s
        self.calls: deque[tuple[float, bool]] = deque()
        self.open_until = 0.0
        self.dependency = dependency  # aisre_circuit_open{dependency} when named (T4.2)

    def _publish(self, is_open: bool) -> None:
        if self.dependency is not None:
            telemetry.circuit_state.labels(dependency=self.dependency).set(1 if is_open else 0)

    def allow(self) -> bool:
        allowed = self.clock() >= self.open_until
        self._publish(not allowed)
        return allowed

    def record(self, failed: bool) -> None:
        now = self.clock()
        self.calls.append((now, failed))
        while self.calls[0][0] <= now - self.window_s:
            self.calls.popleft()
        failures = sum(1 for _, f in self.calls if f)
        if len(self.calls) >= self.min_calls and failures >= self.failure_ratio * len(self.calls):
            self.open_until = now + self.open_s
            self.calls.clear()
            self._publish(True)


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------
Transport = Callable[[str, dict[str, Any], float], Awaitable[tuple[HttpResponse, bool]]]
# (provider, body, timeout_s) -> (response, stream_completed). Raises asyncio.TimeoutError on timeout.


@dataclass(frozen=True)
class GatewayConfig:
    max_attempts: int = 3  # per step, including the first (Artifact 4)
    global_concurrency: int = 8  # across all incidents, per provider (10.6)
    per_incident_concurrency: int = 2
    backoff_s: tuple[float, ...] = (2.0, 4.0)
    allow_cross_provider_fallback: bool = False  # Q10.39: off
    breaker_failure_ratio: float = 0.5  # ADR-61, provisional
    breaker_min_calls: int = 10
    breaker_window_s: float = 60.0
    breaker_open_s: float = 30.0
    reservation_grace_s: float = 60.0  # expires_at = deadline + this, after any call has ended


class Ledger(Protocol):
    """What the gateway needs from a budget ledger: BudgetLedger here, or the durable
    aisre.gateway.ledger.PgBudgetLedger."""

    async def reserve(
        self, rid: str, incident_id: str, in_tok: int, out_tok: int, cost: int, expires_at: float
    ) -> Reservation: ...
    async def mark_sent(self, r: Reservation) -> bool: ...
    async def settle(self, r: Reservation, usage: Usage, actual_cost: int) -> None: ...
    async def release_unsent(self, r: Reservation) -> None: ...
    async def evidence_only(self, now: float) -> bool: ...
    async def reusable(self, incident_id: str, input_sha256: str) -> Optional[ModelResult]: ...
    async def record(self, a: AttemptRecord) -> None: ...


class ModelGateway:
    def __init__(
        self,
        adapter,
        transport: Transport,
        ledger: Ledger,
        prices: dict[str, Price],
        schemas: dict[str, type[BaseModel]],
        config: GatewayConfig = GatewayConfig(),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
    ):
        self.adapter, self.transport, self.ledger, self.prices = (
            adapter,
            transport,
            ledger,
            prices,
        )  # by pinned model
        self.schemas, self.cfg, self.sleep, self.clock = schemas, config, sleep, clock
        self.global_slots = asyncio.Semaphore(config.global_concurrency)
        self.incident_slots: dict[str, asyncio.Semaphore] = {}
        for model in set(getattr(adapter, "model_ids", {}).values()):  # the paging series
            for task in telemetry.MODEL_TASKS:  # exist at 0, so a first rejected key after a
                telemetry.model_calls.labels(  # restart counts in increase() (T4.6 pass 2)
                    provider=adapter.provider, model=model, task=task, status="auth_error"
                )
        self.breaker = CircuitBreaker(
            clock,
            config.breaker_failure_ratio,
            config.breaker_min_calls,
            config.breaker_window_s,
            config.breaker_open_s,
            dependency=f"model:{adapter.provider}",
        )
        self.attempt_log: list[ModelResult] = []

    def _unavailable(self, req: ModelRequest) -> Optional[str]:
        """Q10.22, C13: a schema or tools need the capability whether or not the request lists it."""
        if not self.adapter.enabled:
            return f"{self.adapter.provider} adapter is disabled"
        required = set(req.required_capabilities)
        if req.output_schema is not None:
            required.add(Capability.STRUCTURED_OUTPUT)
        if req.tools:
            required |= {Capability.TOOL_USE, Capability.STRICT_TOOLS}
        missing = sorted(c.value for c in required - self.adapter.capabilities)
        return f"missing {missing}" if missing else None

    async def call(self, req: ModelRequest) -> ModelResult:
        refused = self._unavailable(req)
        if refused:  # Q10.22: refuse, don't degrade silently
            return await self._final(req, 0, ResultStatus.CAPABILITY_UNSUPPORTED, refused)

        input_sha256 = sha256(self.adapter.build(req))  # the step's input, as the skill asked
        recorded = await self.ledger.reusable(req.incident_id, input_sha256)
        if recorded is not None:  # ADR-61: no new reservation, no call
            return recorded.model_copy(update={"request_id": req.request_id})
        if await self.ledger.evidence_only(self.clock()):  # ADR-55: refused here, never mid-step
            return await self._final(
                req, 0, ResultStatus.BUDGET_REFUSED, "fleet spend limit reached", sha=input_sha256
            )

        attempt, current, last, truncation_retried = req.step_attempts_used, req, None, False
        while attempt < self.cfg.max_attempts:
            attempt += 1
            if self.clock() >= req.deadline_epoch_s:  # Q10.32
                return await self._final(
                    req,
                    attempt,
                    ResultStatus.DEADLINE_PASSED,
                    "investigation deadline passed",
                    sha=input_sha256,
                )
            if not self.breaker.allow():  # unsent and unreserved; no point retrying
                return await self._final(
                    req,
                    attempt,
                    ResultStatus.PROVIDER_ERROR,
                    "circuit breaker open",
                    sha=input_sha256,
                )
            last = await self._attempt(current, attempt, input_sha256)
            if last.status == ResultStatus.OK:
                return last
            if (
                last.stop_reason == StopReason.CONTEXT_EXCEEDED
            ):  # the skill reassembles a smaller input
                return last
            if last.status == ResultStatus.TRUNCATED:
                if truncation_retried or current.max_output_tokens >= MAX_OUTPUT_TOKENS_PER_CALL:
                    return last
                # One larger retry for an output that hit the limit; the budget still applies.
                truncation_retried = True
                current = current.model_copy(
                    update={
                        "max_output_tokens": min(
                            MAX_OUTPUT_TOKENS_PER_CALL, current.max_output_tokens * 2
                        )
                    }
                )
                continue
            if last.status not in RETRYABLE or unsafe_kind(last):  # auth, refusal, ADR-59...
                return last
            if last.status == ResultStatus.INVALID_OUTPUT:  # tell the model what was wrong
                current = current.model_copy(
                    update={
                        "context": list(current.context)
                        + [
                            {
                                "ref": f"validation:{attempt}",
                                "kind": "validation_error",
                                "text": "Your previous answer failed validation: "
                                + "; ".join(last.validation_errors[:5]),
                            }
                        ]
                    }
                )
            if attempt < self.cfg.max_attempts:
                wait = self.cfg.backoff_s[min(attempt - 1, len(self.cfg.backoff_s) - 1)]
                if self.clock() + wait >= req.deadline_epoch_s:
                    return last
                await self.sleep(wait)
        return last

    async def _attempt(self, req: ModelRequest, attempt: int, input_sha256: str) -> ModelResult:
        body = self.adapter.build(req)
        in_est = estimate_tokens(json.dumps(body))
        if in_est > MAX_INPUT_TOKENS_PER_CALL:  # ADR-61: refused before reservation
            return await self._final(
                req,
                attempt,
                ResultStatus.BUDGET_REFUSED,
                f"estimated input {in_est} tokens exceeds {MAX_INPUT_TOKENS_PER_CALL}",
                sha=input_sha256,
            )
        price = self.prices.get(body["model"])
        if price is None:  # never send a call we can't charge for
            return await self._final(
                req,
                attempt,
                ResultStatus.BAD_REQUEST,
                f"no configured price for {body['model']}",
                sha=input_sha256,
            )
        reserve_cost = cost_micros(
            Usage(input_tokens=in_est, output_tokens=req.max_output_tokens), price
        )
        try:
            res = await self.ledger.reserve(
                str(uuid.uuid4()),
                req.incident_id,
                in_est,
                req.max_output_tokens,
                reserve_cost,
                req.deadline_epoch_s + self.cfg.reservation_grace_s,
            )
        except BudgetExceeded as e:
            return await self._final(
                req, attempt, ResultStatus.BUDGET_REFUSED, str(e), sha=input_sha256
            )

        slots = self.incident_slots.setdefault(
            req.incident_id, asyncio.Semaphore(self.cfg.per_incident_concurrency)
        )
        remaining = req.deadline_epoch_s - self.clock()
        try:
            await asyncio.wait_for(self._acquire(slots), timeout=max(0.0, remaining))
        except asyncio.TimeoutError:
            await self.ledger.release_unsent(res)  # never sent: safe to release
            return await self._final(
                req,
                attempt,
                ResultStatus.DEADLINE_PASSED,
                "no capacity before deadline",
                sha=input_sha256,
                reservation_id=res.reservation_id,
            )
        started = self.clock()
        try:
            timeout = min(
                req.call_timeout_s, max(0.1, req.deadline_epoch_s - self.clock())
            )  # Q10.33
            if not await self.ledger.mark_sent(res):  # expiry released it: never send
                return await self._final(
                    req,
                    attempt,
                    ResultStatus.DEADLINE_PASSED,
                    "reservation expired before sending",
                    sha=input_sha256,
                    reservation_id=res.reservation_id,
                )
            try:
                resp, completed = await asyncio.wait_for(
                    self.transport(self.adapter.provider, body, timeout), timeout=timeout
                )
            except asyncio.TimeoutError:
                self.breaker.record(True)
                await self.ledger.settle(
                    res, Usage(reported=False), res.cost
                )  # may have consumed tokens
                return await self._final(
                    req,
                    attempt,
                    ResultStatus.TIMEOUT,
                    f"no response in {timeout:.0f}s",
                    usage=Usage(reported=False),
                    cost=res.cost,
                    input_sha256=input_sha256,
                    latency_ms=int((self.clock() - started) * 1000),
                    reservation_id=res.reservation_id,
                    started=started,
                )
        finally:
            slots.release()
            self.global_slots.release()

        result = self.adapter.parse(
            req, attempt, resp, int((self.clock() - started) * 1000), completed
        )
        self.breaker.record(result.status in BREAKER_FAILURES)
        actual = cost_micros(result.usage, price) if result.usage.reported else res.cost
        await self.ledger.settle(res, result.usage, actual)
        result = result.model_copy(update={"cost_micros": actual, "input_sha256": input_sha256})
        proposals = result.stop_reason == StopReason.TOOL_USE and any(
            isinstance(b, ToolProposal) for b in result.content
        )
        if (
            result.status == ResultStatus.OK and req.output_schema_name and not proposals
        ):  # proposals go to the gate
            result = self._validate(req, result)
        await self._record(req, result, res.reservation_id, started)
        return result

    async def _record(
        self,
        req: ModelRequest,
        result: ModelResult,
        reservation_id: Optional[str],
        started: Optional[float] = None,
        sha: Optional[str] = None,
    ) -> None:
        """Every attempt is recorded (model_attempts, Artifact 7, ADR-64), refused ones too."""
        self.attempt_log.append(result)
        if result.input_sha256 is None and sha is not None:
            result = result.model_copy(update={"input_sha256": sha})
        now = self.clock()
        ident = result.identity
        model_ids = getattr(self.adapter, "model_ids", {})
        model = ident.model if ident else model_ids.get(req.model_alias, req.model_alias)
        labels = {"provider": self.adapter.provider, "model": model, "task": req.task}
        telemetry.model_calls.labels(**labels, status=result.status.value).inc()  # T4.2
        if result.latency_ms:
            telemetry.model_latency.labels(**labels).observe(result.latency_ms / 1000)
        await self.ledger.record(
            AttemptRecord(
                req=req,
                result=result,
                reservation_id=reservation_id,
                provider=self.adapter.provider,
                model=ident.model if ident else model_ids.get(req.model_alias, req.model_alias),
                adapter_version=self.adapter.version,
                started_at=now if started is None else started,
                finished_at=now,
            )
        )

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
            return result.model_copy(
                update={
                    "status": ResultStatus.INVALID_OUTPUT,
                    "validation_errors": [f"not valid JSON: {e.msg} at {e.pos}"],
                }
            )
        try:
            parsed = model.model_validate(data)
        except ValidationError as e:
            return result.model_copy(
                update={
                    "status": ResultStatus.INVALID_OUTPUT,
                    "validation_errors": [
                        f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()
                    ],
                }
            )
        try:
            check_output(parsed)
        except UnsafeOutput as e:
            masked = [
                b.model_copy(update={"text": mask_secrets(b.text)})
                if isinstance(b, TextBlock)
                else b
                for b in result.content
                if not isinstance(b, ReasoningBlock)
            ]
            return result.model_copy(
                update={
                    "status": ResultStatus.INVALID_OUTPUT,
                    "content": masked,
                    "validation_errors": [f"{UNSAFE_OUTPUT}{e.code}"],
                }
            )
        return result.model_copy(update={"parsed": parsed.model_dump(mode="json")})

    async def _final(
        self,
        req: ModelRequest,
        attempt: int,
        status: ResultStatus,
        error: str,
        usage: Usage = Usage(),
        cost: int = 0,
        input_sha256: Optional[str] = None,
        latency_ms: int = 0,
        sha: Optional[str] = None,
        reservation_id: Optional[str] = None,
        started: Optional[float] = None,
    ) -> ModelResult:
        """A result the gateway makes itself, recorded like any attempt. `sha` is the step's
        input fingerprint for the record when the result doesn't carry one."""
        r = ModelResult(
            request_id=req.request_id,
            attempt=attempt,
            status=status,
            stop_reason=StopReason.UNKNOWN,
            error=error,
            usage=usage,
            cost_micros=cost,
            input_sha256=input_sha256,
            latency_ms=latency_ms,
        )
        await self._record(req, r, reservation_id, started, sha or input_sha256)
        return r
