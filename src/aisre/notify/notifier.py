"""Artifact 6 component 15, Artifact 7.6, 7.8, 12.1 (T1.19, NFR-09): the Notifier.

Runs in the worker (ADR-51). Two ways in:

- **The `notify` activity**, which the workflow calls for `opened`, `summary`,
  `handed_over` and the rest (activities.NotifyIn): one message to the named audience.
- **Rejected alerts** (ADR-58, closing T1.2's open item): each pass reads new dead letters in
  `dead_letter_id` order after the Notifier's stored cursor, tells the incident lead, and moves
  the cursor, in one transaction per row.

Every message is claimed in the Notifier's own record (`notifications`) under its key before
it is sent, in the same transaction: a repeat is dropped, and a failed send rolls the claim
back so the next try sends it (aisre.record.notifications). The first message for an incident
or a rejected alert observes `aisre_time_to_first_notification_seconds`, from the alert's
arrival (the incident's `created_at`, or the dead letter's `received_at`) to the send.

Database calls are synchronous (aisre.record) and run through `asyncio.to_thread`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Engine
from temporalio import activity

from aisre.notify.channels import Channel, LogChannel, Notification
from aisre.record import notifications as rec
from aisre.reliability import telemetry
from aisre.workflow import activities as act

DEAD_LETTERS = "notifier:dead_letters"
INCIDENT_LEAD = "incident_lead"
BATCH = 50
IDLE_WAIT_S = 2.0
SETTLE = timedelta(seconds=2)

log = logging.getLogger("aisre.notify")


def _observe(alert_at: datetime | None) -> None:
    if alert_at is not None:
        waited = (datetime.now(UTC) - alert_at).total_seconds()
        telemetry.time_to_first_notification.observe(max(waited, 0.0))


class Notifier:
    def __init__(
        self, engine: Engine, channel: Channel | None = None, *, settle: timedelta = SETTLE
    ) -> None:
        self.engine = engine
        self.channel: Channel = channel or LogChannel()
        self.settle = settle

    def activities(self) -> list[Callable[..., Any]]:
        return [self.notify]

    @activity.defn(name="notify")
    async def notify(self, inp: act.NotifyIn) -> None:
        n = Notification(
            key=inp.notification_id,
            kind=inp.kind,
            audience=list(inp.audience),
            incident_id=inp.incident_id,
            detail=inp.detail,
        )
        await asyncio.to_thread(self.send, n)

    def send(self, n: Notification) -> bool:
        """Claim, send, commit. False if the key was sent already."""
        with self.engine.begin() as conn:
            claimed = self._claim(conn, n)
            if claimed is None:
                return False
            self.channel.send(n)
        if claimed.first:
            _observe(claimed.alert_at)
        return True

    def dead_letter_pass(self) -> int:
        """Tell the incident lead about each new dead letter. Returns how many were sent."""
        sent = 0
        while sent < BATCH:
            with self.engine.begin() as conn:
                position = rec.cursor(conn, DEAD_LETTERS)
                after = UUID(position) if position else None
                rows = rec.dead_letters_after(conn, after, limit=1, settle=self.settle)
                if not rows:
                    return sent
                row = rows[0]
                n = Notification(
                    key=f"dead_letter:{row.dead_letter_id}",
                    kind="dead_letter",
                    audience=[INCIDENT_LEAD],
                    dead_letter_id=str(row.dead_letter_id),
                    detail=f"alert rejected: {row.reason}",
                    fields={"org_id": str(row.org_id), "service": str(row.service_slug or "")},
                )
                claimed = self._claim(conn, n)
                if claimed is not None:
                    self.channel.send(n)
                rec.advance(conn, DEAD_LETTERS, str(row.dead_letter_id))
            if claimed is not None:
                sent += 1
                _observe(claimed.alert_at)
        return sent

    def _claim(self, conn: Any, n: Notification) -> rec.Claimed | None:
        return rec.claim(
            conn,
            key=n.key,
            kind=n.kind,
            audience=n.audience,
            channel=self.channel.name,
            incident_id=UUID(n.incident_id) if n.incident_id else None,
            dead_letter_id=UUID(n.dead_letter_id) if n.dead_letter_id else None,
            detail=n.detail,
        )

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.to_thread(self.dead_letter_pass)
            except Exception:
                log.exception("notifier pass failed")
            try:
                await asyncio.wait_for(stop.wait(), IDLE_WAIT_S)
            except TimeoutError:
                pass
