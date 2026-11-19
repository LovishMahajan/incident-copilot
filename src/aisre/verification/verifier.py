"""Artifact 11.12, 13.5 T3.8, T3.17 (FR-30, FR-31, FR-50, ADR-66, ADR-73): the Recovery Verifier.

Two activities on the worker, bound to its engine (`aisre_app`: reads actions and attempts,
inserts `verifications` and timeline events), and to a read-only monitoring source:

- `verify_recovery`: the plan is the criteria recorded on the action before execution (FR-30),
  refused as inconclusive unless they still hash to `recovery_predicates_digest`, which every
  approval signed. The window starts at the first whole minute after the applied attempt and
  lasts the action's hold period (FR-50): `verified` only once every minute of it has ended and
  held. One minute that breaks a criterion is `failed` at once. Missing data, an unreadable
  source or a short window is `inconclusive`, never verified. Monitoring is read directly
  (FR-31): the dispatcher's answer and the model's are no input. One `verifications` row and
  `aisre.verification.completed.v1`, idempotent on the key, and
  `aisre_verification_outcomes_total{verdict, action_kind, category}`.
- `watch_after_change`: after a verified fix, for the watch period (60 minutes, provisional,
  `AISRE_POST_CHANGE_WATCH_SECONDS`), flags a minute breaking the criteria again or an error
  group first seen after the change, as `aisre.verification.watch_flagged.v1` linked to the
  action, once per flag; `aisre.verification.watch_ended.v1` at the end. It never rolls back
  and writes nothing to production (11.12, ADR-48).

`LiveMonitoring` is the one source there is: the FAKE monitoring source's live endpoints
(ops/fakes/monitoring.py), which follow what the fake release process runs (ADR-73); Settings
refuses its URL in production. Database calls are synchronous and run through `asyncio.to_thread`.
"""

from __future__ import annotations

import asyncio
import json
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode
from uuid import UUID, uuid5

from sqlalchemy import Connection, Engine, text
from temporalio import activity
from temporalio.exceptions import ApplicationError

from aisre.contracts import (
    VerificationCompletedData,
    VerificationResult,
    VerificationWatchEndedData,
    VerificationWatchFlaggedData,
)
from aisre.record.incidents import append_event
from aisre.reliability import telemetry
from aisre.verification.verification import (
    Verdict,
    VerificationPlan,
    WatchFlag,
    criteria_digest,
    group_name,
    plan_from_criteria,
    samples_from_rows,
    verify,
    watch_flags,
    window_after,
)
from aisre.verification.verification import (
    VerificationResult as Evaluation,
)
from aisre.workflow import activities as act

VERIFIER = "system:recovery-verifier"
SOURCE = "/aisre/worker/verifier"
NAMESPACE = UUID("6f1c2f8e-5a0b-4c1e-9d2f-3b7a8c9d0e11")  # verification IDs from their keys
POLL_SECONDS = 10.0
SETTLE = timedelta(seconds=5)  # monitoring's last minute is in once it has ended and this passed
BEFORE = timedelta(hours=1)  # the error groups already known before the change
MONITORING_NOTE = "fake monitoring source, live metrics (ADR-73)"


class LiveMonitoring:
    """Read-only: `GET /v1/live/<deployment>/metrics` and `/errors`, one row a minute."""

    def __init__(self, url: str, timeout: float = 5.0) -> None:
        self.url, self.timeout = url.rstrip("/"), timeout

    def _get(self, deployment: str, kind: str, start: datetime, end: datetime) -> Any:
        q = urlencode({"start": _iso(start), "end": _iso(end)})
        path = f"{self.url}/v1/live/{quote(deployment, safe='')}/{kind}?{q}"
        with urllib.request.urlopen(path, timeout=self.timeout) as r:  # noqa: S310 (http only)
            return json.loads(r.read())

    def metrics(self, deployment: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        return list(self._get(deployment, "metrics", start, end))

    def errors(self, deployment: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        return list(self._get(deployment, "errors", start, end))


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Facts:
    """What the verifier reads from the record, never from its caller."""

    incident_id: UUID
    action_id: UUID
    kind: str
    service: str
    deployment: str
    criteria: list[dict[str, Any]]
    criteria_ok: bool
    hold_seconds: int
    category: str
    attempt_id: UUID
    applied_at: datetime


def load_facts(conn: Connection, action_id: UUID) -> Facts:
    r = (
        conn.execute(
            text(
                "SELECT a.incident_id, a.kind, a.target_deployment, a.recovery_criteria, "
                "a.recovery_predicates_digest, a.hold_seconds, s.slug AS service, "
                "d.category, x.attempt_id, x.finished_at FROM actions a "
                "JOIN services s ON s.service_id = a.target_service_id "
                "JOIN diagnoses d ON d.diagnosis_id = a.diagnosis_id "
                "LEFT JOIN LATERAL (SELECT attempt_id, finished_at FROM execution_attempts "
                " WHERE action_id = a.action_id AND outcome = 'applied' "
                " ORDER BY finished_at DESC LIMIT 1) x ON true "
                "WHERE a.action_id = :a"
            ),
            {"a": action_id},
        )
        .mappings()
        .first()
    )
    if r is None or r["attempt_id"] is None:
        raise ApplicationError(
            f"action {action_id} has no applied attempt to verify", type="NotApplied",
            non_retryable=True,
        )  # fmt: skip
    criteria = list(r["recovery_criteria"])
    return Facts(
        incident_id=r["incident_id"],
        action_id=action_id,
        kind=str(r["kind"]),
        service=str(r["service"]),
        deployment=str(r["target_deployment"]),
        criteria=criteria,
        criteria_ok=criteria_digest(criteria) == str(r["recovery_predicates_digest"]).strip(),
        hold_seconds=int(r["hold_seconds"]),
        category=str(r["category"]),
        attempt_id=r["attempt_id"],
        applied_at=r["finished_at"],
    )


class RecoveryVerifier:
    def __init__(
        self,
        engine: Engine,
        monitoring: LiveMonitoring,
        *,
        watch_seconds: int = 3600,
        poll_seconds: float = POLL_SECONDS,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.engine, self.monitoring = engine, monitoring
        self.watch_seconds, self.poll_seconds = watch_seconds, poll_seconds
        self.clock, self.sleep = clock, sleep

    def all(self) -> list[Callable[..., Any]]:
        return [self.verify_recovery, self.watch_after_change]

    def _read(self, fn: Callable[[Connection], Any]) -> Any:
        with self.engine.connect() as conn:
            return fn(conn)

    async def _rows(self, f: Facts, start: datetime, end: datetime) -> list[dict[str, Any]]:
        try:
            return await asyncio.to_thread(self.monitoring.metrics, f.deployment, start, end)
        except (OSError, ValueError):
            return []  # unreadable now: the minutes stay missing, which is never "held"

    # ------------------------------------------------------------------ verify
    @activity.defn(name="verify_recovery")
    async def verify_recovery(self, inp: act.VerifyIn) -> act.VerifyOut:
        vid = uuid5(NAMESPACE, f"{inp.action_id}:{inp.verification_key}")
        action_id = UUID(inp.action_id)
        found = await asyncio.to_thread(self._read, lambda c: _verdict_of(c, vid))
        if found is not None:
            return self._out(vid, found[0], found[1])
        f: Facts = await asyncio.to_thread(self._read, lambda c: load_facts(c, action_id))
        plan = plan_from_criteria(f.service, f.criteria, f.hold_seconds)
        start, end = window_after(f.applied_at, f.hold_seconds)
        rows: list[dict[str, Any]] = []
        res = Evaluation(Verdict.INCONCLUSIVE, ["the window was not observed"])
        while f.criteria_ok:
            now = self.clock()
            if now >= start:
                rows = await self._rows(f, start, min(now, end))
                res = verify(plan, samples_from_rows(rows, start, end, now))
            if res.verdict == Verdict.FAILED or now >= end + SETTLE:
                break  # one bad minute fails it; otherwise the whole window has to pass
            activity.heartbeat(len(rows))
            await self.sleep(self.poll_seconds)
        if not f.criteria_ok:
            res = Evaluation(
                Verdict.INCONCLUSIVE,
                ["the recorded criteria no longer match recovery_predicates_digest"],
            )
        await asyncio.to_thread(self._record, vid, f, start, end, rows, res)
        telemetry.verification_outcomes.labels(
            verdict=res.verdict.value, action_kind=f.kind, category=f.category
        ).inc()
        return self._out(vid, res.verdict.value, end)

    def _out(self, vid: UUID, verdict: str, end: datetime) -> act.VerifyOut:
        verified = verdict == Verdict.VERIFIED.value
        return act.VerifyOut(
            str(vid),
            verdict,  # type: ignore[arg-type]  # a Verdict value
            window_end=_iso(end),
            watch_seconds=self.watch_seconds if verified else 0,
        )

    def _record(
        self,
        vid: UUID,
        f: Facts,
        start: datetime,
        end: datetime,
        rows: list[dict[str, Any]],
        res: Evaluation,
    ) -> None:
        measurements = {
            "source": MONITORING_NOTE,
            "intervals": [
                {k: r.get(k) for k in ("minute_utc", "requests_total", "rate_500_total")}
                for r in rows
            ],
            "per_predicate": res.per_predicate,
            "reasons": res.reasons,
        }
        now = self.clock()
        with self.engine.begin() as conn:
            conn.execute(
                text("SELECT 1 FROM incidents WHERE incident_id = :i FOR UPDATE"),
                {"i": f.incident_id},
            )
            if _verdict_of(conn, vid) is not None:
                return
            conn.execute(
                text(
                    "INSERT INTO verifications (verification_id, action_id, attempt_id, criteria, "
                    "hold_seconds, window_start, window_end, measurements, result, verified_by) "
                    "VALUES (:v, :a, :x, CAST(:c AS jsonb), :h, :s, :e, CAST(:m AS jsonb), "
                    "CAST(:r AS verification_result), :by)"
                ),
                {
                    "v": vid,
                    "a": f.action_id,
                    "x": f.attempt_id,
                    "c": json.dumps(f.criteria),
                    "h": f.hold_seconds,
                    "s": start,
                    "e": max(end, start),
                    "m": json.dumps(measurements),
                    "r": res.verdict.value,
                    "by": VERIFIER,
                },
            )
            append_event(
                conn,
                f.incident_id,
                "aisre.verification.completed.v1",
                VERIFIER,
                VerificationCompletedData(
                    verification_id=vid,
                    action_id=f.action_id,
                    result=VerificationResult(res.verdict.value),
                    hold_seconds=f.hold_seconds,
                ),
                occurred_at=now,
                source=SOURCE,
            )

    # ------------------------------------------------------------------- watch
    @activity.defn(name="watch_after_change")
    async def watch_after_change(self, inp: act.WatchIn) -> act.WatchOut:
        action_id, vid = UUID(inp.action_id), UUID(inp.verification_id)
        start, end = _parse(inp.start), _parse(inp.end)
        f: Facts = await asyncio.to_thread(self._read, lambda c: load_facts(c, action_id))
        plan = plan_from_criteria(f.service, f.criteria, f.hold_seconds)
        seen = set(inp.flagged)
        while True:
            now = self.clock()
            new = [
                flag
                for flag in await self._flags(f, plan, start, min(now, end), now)
                if flag.key not in seen
            ]
            if new:
                await asyncio.to_thread(self._flagged, f, vid, new, now)
                return act.WatchOut([act.WatchFlagOut(x.key, x.kind, x.detail) for x in new], False)
            if now >= end + SETTLE:
                await asyncio.to_thread(self._ended, f, vid, start, end, len(seen))
                return act.WatchOut([], True)
            activity.heartbeat(len(seen))
            await self.sleep(self.poll_seconds)

    async def _flags(
        self, f: Facts, plan: VerificationPlan, start: datetime, until: datetime, now: datetime
    ) -> list[WatchFlag]:
        if until <= start:
            return []
        rows = await self._rows(f, start, until)
        before_change = f.applied_at.replace(second=0, microsecond=0)
        try:
            known = await asyncio.to_thread(
                self.monitoring.errors,
                f.deployment,
                before_change - BEFORE,
                before_change - timedelta(seconds=1),  # whole minutes before the change's
            )
            groups = await asyncio.to_thread(self.monitoring.errors, f.deployment, start, until)
        except (OSError, ValueError):
            return watch_flags(plan, samples_from_rows(rows, start, until, now), [], set())
        seen = {group_name(g) for g in known}
        return watch_flags(plan, samples_from_rows(rows, start, until, now), groups, seen)

    def _flagged(self, f: Facts, vid: UUID, flags: list[WatchFlag], now: datetime) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text("SELECT 1 FROM incidents WHERE incident_id = :i FOR UPDATE"),
                {"i": f.incident_id},
            )
            for flag in flags:
                event_id = uuid5(NAMESPACE, f"{vid}:watch:{flag.key}")
                if conn.execute(
                    text("SELECT 1 FROM timeline_events WHERE event_id = :e"), {"e": event_id}
                ).first():
                    continue
                append_event(
                    conn,
                    f.incident_id,
                    "aisre.verification.watch_flagged.v1",
                    VERIFIER,
                    VerificationWatchFlaggedData(
                        verification_id=vid,
                        action_id=f.action_id,
                        kind=flag.kind,
                        detail=flag.detail,
                        observed_at=now,
                    ),
                    occurred_at=now,
                    source=SOURCE,
                    event_id=event_id,
                )

    def _ended(self, f: Facts, vid: UUID, start: datetime, end: datetime, flags: int) -> None:
        event_id = uuid5(NAMESPACE, f"{vid}:watch:ended")
        with self.engine.begin() as conn:
            conn.execute(
                text("SELECT 1 FROM incidents WHERE incident_id = :i FOR UPDATE"),
                {"i": f.incident_id},
            )
            if conn.execute(
                text("SELECT 1 FROM timeline_events WHERE event_id = :e"), {"e": event_id}
            ).first():
                return
            append_event(
                conn,
                f.incident_id,
                "aisre.verification.watch_ended.v1",
                VERIFIER,
                VerificationWatchEndedData(
                    verification_id=vid,
                    action_id=f.action_id,
                    started_at=start,
                    ended_at=end,
                    flags=flags,
                ),
                occurred_at=self.clock(),
                source=SOURCE,
                event_id=event_id,
            )


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _verdict_of(conn: Connection, vid: UUID) -> tuple[str, datetime] | None:
    r = conn.execute(
        text("SELECT result, window_end FROM verifications WHERE verification_id = :v"),
        {"v": vid},
    ).first()
    return None if r is None else (str(r[0]), r[1])
