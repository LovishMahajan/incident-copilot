"""Artifact 7.9 (T1.5): the live timeline, `GET /v1/incidents/{id}/timeline/stream`, over SSE.

    retry: 3000                     once, on connect
    id: 27                          one message per event, in sequence order
    event: aisre.approval.granted.v1
    data: {...the CloudEvent JSON...}
    : heartbeat                     a comment every 15 s, so proxies keep the connection

- Starts after the `Last-Event-ID` header, else `?after_seq=`, else 0, replaying from
  `timeline_events` (the table is the source of truth, Artifact 7.6), then follows new rows.
- New rows are found by reading the table every POLL_S. Artifact 7.9 asks for a database
  notification; polling gives the same events a little later and needs no trigger. Swap it
  for LISTEN when the latency target asks.
- Ends once the incident is closed or cancelled and its last event has been sent.
- Re-checks permission every 5 minutes: the request's sign-in is verified again (an expired
  or refused token ends the stream) and the caller must still read the incident's service.
- At most 10 open streams per caller (provisional); the 11th is 429.

Only the platform's own events are streamed, never Temporal's history (Q7.53).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import Engine

from aisre.api.auth import DEV_CALLER_HEADER, DEV_GROUPS_HEADER, DEV_ORG_HEADER, Caller
from aisre.api.problems import ApiError
from aisre.contracts import CloudEvent, IncidentStatus
from aisre.record import load_incident, timeline
from aisre.remediation.action import Perm

MEDIA_TYPE = "text/event-stream"
RETRY_MS = 3000
POLL_S = 1.0
HEARTBEAT_S = 15.0
RECHECK_S = 300.0
MAX_PER_CALLER = 10  # provisional (Artifact 7.9)
BATCH = 500
FINISHED = frozenset({IncidentStatus.closed, IncidentStatus.cancelled})


@dataclass(frozen=True)
class Timing:
    poll_s: float = POLL_S
    heartbeat_s: float = HEARTBEAT_S
    recheck_s: float = RECHECK_S


def starting_point(last_event_id: str | None, after_seq: int | None) -> int:
    if last_event_id is not None:
        if not last_event_id.strip().isdigit():
            raise ApiError(400, "bad_last_event_id", "Bad Last-Event-ID", "Expected a sequence.")
        return int(last_event_id)
    return after_seq or 0


def message(event: CloudEvent) -> str:
    return f"id: {event.sequence}\nevent: {event.type}\ndata: {event.model_dump_json()}\n\n"


class Streams:
    def __init__(self, db: Engine, timing: Timing | None = None) -> None:
        self.db = db
        self.timing = timing or Timing()
        self.open: dict[str, int] = {}

    def reserve(self, caller: Caller) -> None:
        if self.open.get(caller.caller_id, 0) >= MAX_PER_CALLER:
            raise ApiError(
                429,
                "too_many_streams",
                "Too many streams",
                f"At most {MAX_PER_CALLER} open timeline streams per caller.",
            )
        self.open[caller.caller_id] = self.open.get(caller.caller_id, 0) + 1

    def _release(self, caller: Caller) -> None:
        left = self.open.get(caller.caller_id, 1) - 1
        if left:
            self.open[caller.caller_id] = left
        else:
            self.open.pop(caller.caller_id, None)

    async def events(
        self, request: Request, caller: Caller, incident_id: UUID, service: str, start: int
    ) -> AsyncIterator[str]:
        """The stream's body. `reserve` must have succeeded for this caller."""
        t = self.timing
        cursor = start
        beat = checked = time.monotonic()
        try:
            yield f"retry: {RETRY_MS}\n\n"
            while True:
                items, status, last = await run_in_threadpool(
                    self._read, incident_id, caller.org_id, cursor
                )
                for e in items:
                    yield message(e)
                    cursor = e.sequence
                if items:
                    beat = time.monotonic()
                if status is None or (status in FINISHED and cursor >= last):
                    return
                if len(items) == BATCH:
                    continue
                now = time.monotonic()
                if now - checked >= t.recheck_s:
                    checked = now
                    if not await run_in_threadpool(self._still_allowed, request, service):
                        return
                if now - beat >= t.heartbeat_s:
                    beat = now
                    yield ": heartbeat\n\n"
                if await request.is_disconnected():
                    return
                await asyncio.sleep(t.poll_s)
        finally:
            self._release(caller)

    def _read(
        self, incident_id: UUID, org_id: UUID, cursor: int
    ) -> tuple[list[CloudEvent], IncidentStatus | None, int]:
        with self.db.connect() as conn:
            view = load_incident(conn, incident_id, org_id)
            if view is None:
                return [], None, 0
            items = timeline(conn, incident_id, org_id, after_seq=cursor, limit=BATCH)
            return items, view.status, view.last_event_seq

    @staticmethod
    def _still_allowed(request: Request, service: str) -> bool:
        """Sign in again with the request's own credentials, then the read check."""
        h = request.headers
        args: dict[str, Any] = {
            "authorization": h.get("authorization"),
            "dev_caller": h.get(DEV_CALLER_HEADER),
            "dev_org": h.get(DEV_ORG_HEADER),
            "dev_groups": h.get(DEV_GROUPS_HEADER),
        }
        try:
            caller = request.app.state.authenticator.caller(request, **args)
        except ApiError:
            return False
        return bool(caller.can(Perm.READ, service))
