"""Artifact 7.7 (ADR-19): the outbox relay, the only path from the record to Temporal.

Runs inside the worker process; any number of relays can run at once. Each pass claims up to
50 undelivered rows whose time has come, `FOR UPDATE SKIP LOCKED` in `outbox_id` order, so two
relays never take the same row. For each row it starts workflow `incident-<id>` or signals it,
then marks the row delivered in the same transaction that holds the claim. Delivery is at
least once: a crash between Temporal and the commit sends the row again, which is harmless.
"Already started" is success (the workflow ID is the dedupe key, and REJECT_DUPLICATE keeps a
finished `incident-<id>` from starting twice), and the workflow ignores signal IDs it has seen.

A failed row gets attempts + 1, its error, and an exponential backoff capped at one minute
(`available_at`). Within a pass, once a row fails, that incident's later rows wait for the
next pass, so one incident's messages stay in order.

Metrics (Artifact 12.1): `aisre_event_available_seconds` per delivered row (outbox write to
delivery, WR-31), and the pending count and oldest pending age after each pass.

The database calls are synchronous (aisre.record), so each runs through `asyncio.to_thread`;
one connection serves a whole pass, used by one thread at a time.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Engine, func, select, update
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from aisre.record import SIGNAL_WORKFLOW, START_WORKFLOW
from aisre.record.tables import outbox
from aisre.reliability import telemetry
from aisre.workflow.activities import INVESTIGATION_QUEUE
from aisre.workflow.workflow import (
    ApprovalSignal,
    CancelSignal,
    IncidentWorkflow,
    IncidentWorkflowInput,
    LeadSignal,
)

log = logging.getLogger(__name__)

BATCH = 50
MAX_BACKOFF = timedelta(minutes=1)
IDLE_WAIT_S = 0.5  # between passes when caught up; NFR-31 wants p95 <= 1 s
ERROR_TEXT_MAX = 1000
SIGNALS: dict[str, type[Any]] = {
    "approval_decided": ApprovalSignal,
    "cancellation_requested": CancelSignal,
    "lead_decision": LeadSignal,
}


@dataclass(frozen=True)
class Message:
    outbox_id: int
    incident_id: UUID
    kind: str
    workflow_id: str
    payload: dict[str, Any]
    attempts: int  # before this pass


class UnknownMessage(ValueError):
    """A row the relay can't turn into a Temporal call. It stays undelivered and alerts."""


def backoff(attempts: int) -> timedelta:
    """Wait before the next try after `attempts` failures: 1 s, 2 s, 4 s ... capped at 1 min."""
    return min(timedelta(seconds=2 ** max(attempts - 1, 0)), MAX_BACKOFF)


class Relay:
    def __init__(
        self,
        engine: Engine,
        client: Client,
        *,
        batch: int = BATCH,
        task_queue: str = INVESTIGATION_QUEUE,
    ) -> None:
        self.engine, self.client = engine, client
        self.batch, self.task_queue = batch, task_queue

    async def run(self, stop: asyncio.Event, on_pass: Callable[[], None] | None = None) -> None:
        """Pass after pass until `stop` is set. A full batch means more is waiting: no pause."""
        while not stop.is_set():
            delivered = 0
            try:
                delivered = await self.run_once()
            except Exception:
                log.exception("outbox relay pass failed")
            if on_pass is not None:
                on_pass()
            if delivered < self.batch:
                try:
                    await asyncio.wait_for(stop.wait(), IDLE_WAIT_S)
                except TimeoutError:
                    pass

    async def run_once(self) -> int:
        """One pass: claim, deliver, record. Returns the number of rows delivered."""
        conn = await asyncio.to_thread(self.engine.connect)
        try:
            await asyncio.to_thread(conn.begin)
            messages = await asyncio.to_thread(_claim, conn, self.batch)
            blocked: set[UUID] = set()
            delivered = 0
            for m in messages:
                if m.incident_id in blocked:
                    continue  # an earlier row of this incident failed: keep the order
                try:
                    await self.deliver(m)
                except Exception as e:
                    blocked.add(m.incident_id)
                    log.warning("outbox row %s not delivered: %r", m.outbox_id, e)
                    await asyncio.to_thread(_mark_failed, conn, m, repr(e))
                    continue
                waited = await asyncio.to_thread(_mark_delivered, conn, m.outbox_id)
                telemetry.event_available.observe(waited)
                delivered += 1
            await asyncio.to_thread(conn.commit)
            pending, oldest = await asyncio.to_thread(_backlog, conn)
            telemetry.outbox_pending.set(pending)
            telemetry.outbox_oldest_age.set(oldest)
            return delivered
        finally:
            await asyncio.to_thread(conn.close)

    async def deliver(self, m: Message) -> None:
        """Start or signal the workflow. Raises on anything but success or "already started"."""
        if m.kind == START_WORKFLOW:
            try:
                await self.client.start_workflow(
                    IncidentWorkflow.run,
                    IncidentWorkflowInput(**m.payload),
                    id=m.workflow_id,
                    task_queue=self.task_queue,
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                )
            except WorkflowAlreadyStartedError:
                pass  # delivered before: success (Artifact 7.8)
        elif m.kind == SIGNAL_WORKFLOW:
            name = m.payload.get("signal")
            if name not in SIGNALS:
                raise UnknownMessage(f"unknown signal {name!r}")
            arg = SIGNALS[name](**m.payload.get("arg", {}))
            await self.client.get_workflow_handle(m.workflow_id).signal(name, arg)
        else:
            raise UnknownMessage(f"unknown kind {m.kind!r}")


def _claim(conn: Connection, batch: int) -> list[Message]:
    rows = conn.execute(
        select(
            outbox.c.outbox_id,
            outbox.c.incident_id,
            outbox.c.kind,
            outbox.c.workflow_id,
            outbox.c.payload,
            outbox.c.attempts,
        )
        .where(outbox.c.delivered_at.is_(None), outbox.c.available_at <= func.now())
        .order_by(outbox.c.outbox_id)
        .limit(batch)
        .with_for_update(skip_locked=True)
    )
    return [
        Message(r.outbox_id, r.incident_id, r.kind, r.workflow_id, r.payload, r.attempts)
        for r in rows
    ]


def _mark_delivered(conn: Connection, outbox_id: int) -> float:
    """Mark delivered now (the wall clock, not the transaction's start); return the wait."""
    delivered = func.clock_timestamp()
    waited = conn.execute(
        update(outbox)
        .where(outbox.c.outbox_id == outbox_id)
        .values(delivered_at=delivered, attempts=outbox.c.attempts + 1, last_error=None)
        .returning(func.extract("epoch", outbox.c.delivered_at - outbox.c.created_at))
    ).scalar_one()
    return float(waited)


def _mark_failed(conn: Connection, m: Message, error: str) -> None:
    conn.execute(
        update(outbox)
        .where(outbox.c.outbox_id == m.outbox_id)
        .values(
            attempts=outbox.c.attempts + 1,
            last_error=error[:ERROR_TEXT_MAX],
            available_at=func.clock_timestamp() + backoff(m.attempts + 1),
        )
    )


def _backlog(conn: Connection) -> tuple[int, float]:
    """Undelivered rows, and the age in seconds of the oldest (0 when none)."""
    row = conn.execute(
        select(
            func.count(),
            func.coalesce(func.extract("epoch", func.now() - func.min(outbox.c.created_at)), 0),
        )
        .select_from(outbox)
        .where(outbox.c.delivered_at.is_(None))
    ).one()
    return int(row[0]), float(row[1])
