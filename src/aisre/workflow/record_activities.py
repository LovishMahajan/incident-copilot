"""Artifact 8.2, 13.5 T0.11, T1.10: the real `load_incident`, `record_transition`, `hand_over`
and `collect_evidence`.

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
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Optional, TypeVar
from uuid import UUID

from sqlalchemy import Connection, Engine
from sqlalchemy.exc import DBAPIError
from temporalio import activity

from aisre.agent.agent_contracts import EvidenceKind, SkillFailure, SkillResult, SkillUsage
from aisre.agent.tool_registry import MemoryToolCalls, ToolGate
from aisre.evidence import collector
from aisre.evidence.facts import ChangeInfo
from aisre.evidence.snapshots import SnapshotFailed, SnapshotStore, snapshot_key
from aisre.evidence.sources import SourceSet
from aisre.record import evidence as ev_rec
from aisre.record import transitions as rec
from aisre.record.tool_calls import PgToolCalls, rows_in
from aisre.workflow import activities as act
from aisre.workflow.states import InvalidTransition

R = TypeVar("R")
log = logging.getLogger(__name__)


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


def _transaction(engine: Engine, fn: Callable[[Connection], R]) -> R:
    """One transaction; a database error is an outage the activity's retry policy waits out."""
    try:
        with engine.begin() as conn:
            return fn(conn)
    except DBAPIError as e:
        raise ConnectionError(f"database: {type(e.orig).__name__}") from e


def _heartbeat() -> None:
    if activity.in_activity():
        activity.heartbeat()


class EvidenceActivities:
    """The Evidence Collector's activity (T1.10), for `Worker(activities=...)`: `.all()`.

    `sources` says which adapter answers each READ tool; `snapshots` is the object store, or
    None, in which case records are stored with their fingerprint and no snapshot."""

    def __init__(
        self,
        engine: Engine,
        sources: SourceSet,
        snapshots: SnapshotStore | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.engine = engine
        self.sources = sources
        self.snapshots = snapshots
        self.tool_calls = PgToolCalls(engine)
        self.clock = clock

    def all(self) -> list[Callable[..., Any]]:
        return [self.collect_evidence]

    def now(self) -> datetime:
        replay = self.sources.replay_as_of
        return min(self.clock(), replay) if replay else self.clock()

    @activity.defn(name="collect_evidence")
    async def collect_evidence(self, inp: act.EvidenceIn) -> SkillResult[act.EvidenceOut]:
        """Activity 8.2's Evidence Collector. Idempotent on `idempotency_key`: a round already
        stored is returned as it was. A failed source is a gap; a database outage raises, and
        TOOL_RETRY retries the round."""
        incident_id = UUID(inp.incident_id)

        def prepare(
            conn: Connection,
        ) -> tuple[
            ev_rec.IncidentScope | None, list[Any], list[dict[str, Any]], list[dict[str, Any]]
        ]:
            return (
                ev_rec.incident_scope(conn, incident_id),
                ev_rec.round_evidence(conn, incident_id, inp.idempotency_key),
                rows_in(conn, inp.incident_id),
                ev_rec.change_history(conn, incident_id),
            )

        scope, done, rows, changes = await asyncio.to_thread(_transaction, self.engine, prepare)
        if scope is None:
            failure = SkillFailure(
                skill="collect_evidence", code="store_failed", detail="incident not found"
            )
            return SkillResult(failure=failure, usage=SkillUsage())
        if done:
            gaps = sum(e.kind == EvidenceKind.gap for e in done)
            out = act.EvidenceOut([str(e.evidence_id) for e in done], gaps)
            return SkillResult(output=out, usage=SkillUsage())

        now = self.now()
        # The gate checks against the rows as read; store_round counts again under the lock.
        checked = MemoryToolCalls()  # type: ignore[no-untyped-call]
        for row in rows:
            checked.add(inp.incident_id, row)
        gate = ToolGate(inp.incident_id, scope.service, scope.started_at, now, store=checked)
        cscope = collector.Scope(
            inp.incident_id, scope.service, scope.environment, scope.started_at, scope.alert_id
        )
        calls = (
            collector.baseline_plan(cscope)
            if inp.mode == "baseline"
            else [collector.Call(r["tool"], dict(r["params"])) for r in inp.requests]
        )
        outcome = await collector.collect(
            cscope,
            calls,
            gate,
            self.sources,
            collection_key=inp.idempotency_key,
            now=now,
            on_progress=_heartbeat,
            known_changes=[ChangeInfo.from_json(c) for c in changes],
        )
        items = [await self._with_snapshot(inp.incident_id, c) for c in outcome.collected]
        fresh = {str(c.evidence.evidence_id) for c in outcome.collected}
        call_rows = [
            {
                "tool": c.tool,
                "hash": c.call_hash,
                "params": c.params,
                "skill": c.skill,
                "new_evidence": c.new_evidence,
                "evidence_ids": list(c.evidence_ids),
                "fresh": bool(fresh & set(c.evidence_ids)),
            }
            for c in outcome.calls
        ]
        stored, refused = await asyncio.to_thread(
            _transaction,
            self.engine,
            lambda conn: ev_rec.store_round(
                conn,
                org_id=scope.org_id,
                incident_id=incident_id,
                items=items,
                calls=call_rows,
                tool_calls=self.tool_calls,
                now=now,
            ),
        )
        for r in outcome.refused + refused:
            log.info("incident %s: %s refused (%s)", inp.incident_id, r["tool"], r["refused"])
        refused_calls = {r["hash"] for r in refused}  # one round never repeats a call
        ids = [i for c in outcome.calls if c.call_hash not in refused_calls for i in c.evidence_ids]
        gaps = sum(
            c.evidence.kind == EvidenceKind.gap and c.evidence.evidence_id in stored
            for c in outcome.collected
        )
        usage = SkillUsage(tool_calls=len(call_rows) - len(refused))
        return SkillResult(output=act.EvidenceOut(list(dict.fromkeys(ids)), gaps), usage=usage)

    async def _with_snapshot(self, incident_id: str, c: collector.Collected) -> ev_rec.NewEvidence:
        """Puts the snapshot in the object store; without one, the record keeps its fingerprint
        and says the snapshot wasn't kept."""
        if c.snapshot is None:
            return ev_rec.NewEvidence(c.evidence, c.attributes)
        if self.snapshots is None:
            return ev_rec.NewEvidence(c.evidence, c.attributes | {"snapshot": "not configured"})
        key = snapshot_key(incident_id, str(c.evidence.evidence_id))
        try:
            await asyncio.to_thread(self.snapshots.put, key, c.snapshot, "application/json")
        except SnapshotFailed as e:
            log.warning("incident %s: snapshot not stored: %s", incident_id, e)
            return ev_rec.NewEvidence(c.evidence, c.attributes | {"snapshot": "not stored"})
        return ev_rec.NewEvidence(c.evidence, c.attributes, key, len(c.snapshot))
