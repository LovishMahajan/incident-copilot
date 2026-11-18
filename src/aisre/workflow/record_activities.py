"""Artifact 8.2, 13.5 T0.11: the real `load_incident`, `record_transition` and `hand_over`.

Bound to the worker's engine (`aisre_app`, ADR-51). The Incident Record is synchronous
(aisre.record), so each call runs in a thread through `asyncio.to_thread`, one connection and
one transaction per call, and the event loop never waits on the database.

Errors, as the workflow's retry policies expect (8.5): a stale version raises
activities.StaleVersion, and the workflow reloads and writes again (ADR-69). A transition the
database refuses (check_violation, restrict_violation), or an action not in the status the
workflow thinks it is, raises InvalidTransition, which DB_RETRY doesn't retry. Anything else
from the database is an outage (ConnectionError), retried until it ends.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Callable
from typing import Any, Optional, TypeVar
from uuid import UUID

from sqlalchemy import Connection, Engine
from sqlalchemy.exc import DBAPIError
from temporalio import activity

from aisre.record import transitions as rec
from aisre.workflow import activities as act
from aisre.workflow.states import InvalidTransition

R = TypeVar("R")


class RecordActivities:
    """The three record activities, for `Worker(activities=...)`: `.all()`."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def all(self) -> list[Callable[..., Any]]:
        return [self.load_incident, self.record_transition, self.hand_over]

    async def _in_transaction(self, fn: Callable[[Connection], R]) -> R:
        def run() -> R:
            try:
                with self.engine.begin() as conn:
                    return fn(conn)
            except DBAPIError as e:
                sqlstate = getattr(e.orig, "sqlstate", None) or ""
                raise act.db_error(sqlstate, str(e.orig)) from e

        return await asyncio.to_thread(run)

    @activity.defn(name="load_incident")
    async def load_incident(self, inp: act.IncidentIn) -> Optional[act.IncidentSnapshot]:
        snap = await self._in_transaction(lambda c: rec.snapshot(c, UUID(inp.incident_id)))
        if snap is None:
            return None
        # remediation_enabled stays False: Release B needs a recorded decision (ADR-44).
        return act.IncidentSnapshot(
            str(snap.incident_id), snap.service, snap.environment, snap.version, snap.status
        )

    @activity.defn(name="record_transition")
    async def record_transition(self, inp: act.TransitionIn) -> act.TransitionOut:
        t = rec.Transition(**dataclasses.asdict(inp))
        try:
            version, seq = await self._in_transaction(lambda c: rec.record_transition(c, t))
        except rec.StaleVersion as e:
            raise act.StaleVersion(str(e)) from e
        except rec.ActionMismatch as e:
            raise InvalidTransition(str(e)) from e
        return act.TransitionOut(new_version=version, event_seq=seq)

    @activity.defn(name="hand_over")
    async def hand_over(self, inp: act.HandOverIn) -> None:
        await self._in_transaction(
            lambda c: rec.hand_over(c, UUID(inp.incident_id), inp.reason, inp.handover_key)
        )
