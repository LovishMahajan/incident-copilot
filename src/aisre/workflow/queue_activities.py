"""Artifact 8.2, 12.5, 13.5 T4.3 (NFR-16, NFR-17, NFR-38, NFR-39, NFR-44; WR-13): the real
`acquire_investigation_slot`.

Each attempt is one short transaction (aisre.record.queue.acquire): queue the incident by
severity under its run, then grant if activities.may_grant allows, else raise activities.NoSlot,
which the workflow's SLOT_RETRY asks again every 15 s. Nothing is dropped.

When the incident first queues without a slot, on-call is told it is queued (NFR-17); when the
queue then holds more than QUEUE_ALERT_AT waiting, the incident lead is told too (NFR-39), as
well as paged by AisreQueueDepthHigh through Alertmanager. Both go through the Notifier with keys
derived from the run, so a retried attempt never sends twice. Every attempt refreshes
`aisre_incident_queue_depth` and `aisre_incident_queue_oldest_age_seconds` per severity, and a
grant observes `aisre_incident_queue_wait_seconds`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Engine
from temporalio import activity

from aisre.notify.channels import Notification
from aisre.notify.notifier import INCIDENT_LEAD, Notifier
from aisre.record import queue
from aisre.reliability import telemetry
from aisre.workflow import activities as act

QUEUE_ALERT_AT = 50  # NFR-39, provisional: more than this waiting alerts the incident lead
SEVERITIES = ("sev1", "sev2", "sev3", "sev4", "none")


def publish_depth(engine: Engine) -> None:
    """`aisre_incident_queue_depth` and `aisre_incident_queue_oldest_age_seconds` per severity,
    from investigation_queue. Every attempt calls it, and the worker's probe cycle too, so the
    gauges fall back to 0 when the queue drains without another attempt (a cancel)."""
    with engine.connect() as conn:
        depth = queue.snapshot(conn)
    for sev in SEVERITIES:
        n, age = depth.get(sev, (0, 0.0))
        telemetry.queue_depth.labels(severity=sev).set(n)
        telemetry.queue_oldest_age.labels(severity=sev).set(age)


class QueueActivities:
    def __init__(self, engine: Engine, notifier: Notifier) -> None:
        self.engine, self.notifier = engine, notifier

    def all(self) -> list[Callable[..., Any]]:
        return [self.acquire_investigation_slot]

    def _attempt(self, inp: act.SlotIn) -> queue.Grant:
        with self.engine.begin() as conn:
            grant = queue.acquire(conn, UUID(inp.incident_id), inp.run_id)
        if grant.newly_queued and not grant.granted:
            self.notifier.send(
                Notification(
                    key=f"{inp.idempotency_key}:queued",
                    kind="queued",
                    audience=["on_call"],
                    incident_id=inp.incident_id,
                    detail=f"waiting for an investigation slot, {grant.waiting} waiting",
                )
            )
            if grant.waiting > QUEUE_ALERT_AT:
                self.notifier.send(
                    Notification(
                        key=f"{inp.idempotency_key}:queue_overflow",
                        kind="queue_overflow",
                        audience=[INCIDENT_LEAD],
                        incident_id=inp.incident_id,
                        detail=f"{grant.waiting} incidents waiting, over {QUEUE_ALERT_AT} (NFR-39)",
                    )
                )
        publish_depth(self.engine)
        return grant

    @activity.defn(name="acquire_investigation_slot")
    async def acquire_investigation_slot(self, inp: act.SlotIn) -> None:
        grant = await asyncio.to_thread(self._attempt, inp)
        if not grant.granted:
            raise act.NoSlot(f"queued: {grant.waiting} waiting")
        if not grant.just_granted:
            return  # a repeat after the grant: already counted
        waited = (datetime.now(UTC) - grant.queued_at).total_seconds()
        telemetry.queue_wait.labels(severity=grant.severity or "none").observe(max(waited, 0.0))
