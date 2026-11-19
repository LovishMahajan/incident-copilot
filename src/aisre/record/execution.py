"""Artifact 7.2, 11.9–11.11, 12.5, 13.5 T3.4 (FR-27 to FR-29, ST-03 to ST-05, ST-37, ADR-47,
ADR-51, ADR-69): the dispatcher's ledger, trusted store and write fence, in PostgreSQL.

Connects as `aisre_dispatcher`, the only role that writes `execution_attempts`. Each class
implements one of Layer 11's protocols, so `dispatcher.Dispatcher` runs unchanged over them:

- `PgTrustedStore` (dispatcher.TrustedStore): the immutable action rebuilt from its row
  (record.approvals.load_action, so its digest is the one approvers signed), its signed
  decisions, status, ADR-49's facts, the latest verification verdict, a stop raised on its
  incident since it was created, and the patch's stored bytes from the object store.
- `PgLedger` (dispatcher.Ledger): `begin` is the intent transaction, the action
  `approved -> applying` and the `started` attempt, in which the database re-checks the
  approval count, rejections, revocations, stops and the per-deployment block under the
  deployment's advisory lock (check_execution_attempt). Its refusal is returned, never raised.
  `put` finishes an attempt once (`started -> outcome`), or adds the reconciliation row, and
  moves the action's status with it. Blocks follow attempts through the database's trigger;
  a late apply's block and a person's resolution of it are written here. Every write has its
  `aisre.execution.*` event, the only events the role may write.
- `PgFence` (dispatcher.WriteFence): the epoch and the active reasons, read at each check.

Database calls are synchronous; the dispatcher's activities run them in a thread.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from aisre.contracts import (
    ExecutionCompletedData,
    ExecutionOutcome,
    ExecutionOutcomeResolvedData,
    ExecutionRefusedData,
    ExecutionStartedData,
    Strict,
)
from aisre.record import approvals as rec
from aisre.record.incidents import uuid7
from aisre.remediation.action import Decision, ImmutableAction, ImpactFacts
from aisre.remediation.dispatcher import (
    ACTION_STATUS,
    LATE_APPLY,
    DeploymentBlock,
    LedgerEntry,
    LedgerState,
    OutcomeResolution,
)

ACTOR = "system:dispatcher"
SOURCE = "/aisre/dispatcher"
REFUSED_STATES = {"23514", "23001", "42501"}  # check, restrict, insufficient_privilege


class BlobReader(Protocol):
    def get(self, key: str) -> bytes: ...


def _event(
    conn: Connection, action_id: str, event_type: str, data: Strict, actor: str = ACTOR
) -> None:
    """One `aisre.execution.*` event. No RETURNING: the role may insert events, not read them.
    assign_timeline_event sets seq, prev_hash and hash."""
    conn.execute(
        text(
            "INSERT INTO timeline_events (event_id, incident_id, type, source, actor, "
            "occurred_at, correlation_id, data) SELECT :e, a.incident_id, :t, :src, "
            ":actor, now(), a.incident_id, CAST(:d AS jsonb) FROM actions a "
            "WHERE a.action_id = :a"
        ),
        {
            "e": uuid7(),
            "t": event_type,
            "src": SOURCE,
            "actor": actor,
            "d": data.model_dump_json(),
            "a": UUID(action_id),
        },
    )


class PgTrustedStore:
    def __init__(self, engine: Engine, blobs: BlobReader | None) -> None:
        self.engine, self.blobs = engine, blobs

    def _one(self, sql: str, **params: Any) -> Any:
        with self.engine.connect() as conn:
            return conn.execute(text(sql), params).scalar()

    def action(self, action_id: str) -> ImmutableAction:
        with self.engine.connect() as conn:
            found = rec.load_action(conn, UUID(action_id))
        if found is None:
            raise KeyError(action_id)
        return found

    def decisions(self, action_id: str) -> list[Decision]:
        with self.engine.connect() as conn:
            return rec.decisions(conn, UUID(action_id))

    def requested_at(self, action_id: str) -> datetime:
        with self.engine.connect() as conn:
            req = rec.latest_request(conn, UUID(action_id))
        if req is None:
            raise KeyError(f"no approval request for {action_id}")
        return req.requested_at

    def verdict(self, action_id: str) -> str | None:
        found = self._one(
            "SELECT result FROM verifications WHERE action_id = :a "
            "ORDER BY created_at DESC LIMIT 1",
            a=UUID(action_id),
        )
        return None if found is None else str(found)

    def status(self, action_id: str) -> str:
        return str(self._one("SELECT status FROM actions WHERE action_id = :a", a=UUID(action_id)))

    def impact(self, action_id: str) -> ImpactFacts:
        with self.engine.connect() as conn:
            return rec.impact(conn, UUID(action_id))

    def stop(self, action_id: str) -> str | None:
        found = self._one(
            "SELECT s.requested_by FROM stop_requests s JOIN actions a "
            "ON a.incident_id = s.incident_id WHERE a.action_id = :a "
            "AND s.requested_at >= a.created_at ORDER BY s.requested_at LIMIT 1",
            a=UUID(action_id),
        )
        return None if found is None else str(found)

    def patch_content(self, patch_sha256: str) -> bytes:
        """The bytes the artifact store holds for this patch. Missing anywhere is a KeyError."""
        with self.engine.connect() as conn:
            key = conn.execute(
                text(
                    "SELECT r.storage_key FROM patches p JOIN artifacts r "
                    "ON r.artifact_id = p.diff_artifact_id WHERE p.patch_sha256 = :s "
                    "ORDER BY p.created_at DESC LIMIT 1"
                ),
                {"s": patch_sha256},
            ).scalar()
        if key is None or self.blobs is None:
            raise KeyError(patch_sha256)
        try:
            return self.blobs.get(str(key))
        except Exception as e:  # the store's own error: unreadable is a refusal, never a pass
            raise KeyError(patch_sha256) from e


def _entry(r: Any) -> LedgerEntry:
    outcome = str(r["outcome"])
    resolution = (
        OutcomeResolution(
            str(r["resolved_by"]), outcome, str(r["observed_revision"]), str(r["resolution_reason"])
        )
        if r["resolved_by"]
        else None
    )
    return LedgerEntry(
        execution_key=str(r["execution_key"]),
        action_digest=str(r["action_digest"]).strip(),
        deployment=str(r["target_deployment"]),
        approval_id=str(r["approval_id"]),
        attempt_no=int(r["attempt_no"]),
        state=LedgerState(outcome if outcome != "failed" else "not_applied"),
        reconciliation=bool(r["reconciliation"]),
        resolution=resolution,
        started_at=r["started_at"],
        external_ref=r["external_ref"],
        notes=[r["error"]] if r["error"] else [],
        attempt_id=str(r["attempt_id"]),
    )


class _Events:
    """`Ledger.events`: the dispatcher appends its refusals here; each is written at once."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def append(self, event: dict[str, Any]) -> None:
        data = event["data"]
        with self.engine.begin() as conn:
            _event(
                conn,
                data["action_id"],
                event["type"],
                ExecutionRefusedData(
                    action_id=UUID(data["action_id"]),
                    execution_key=data["execution_key"],
                    reason=str(data["reason"])[:1000],
                ),
            )


class PgLedger:
    def __init__(self, engine: Engine, clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.engine, self.clock = engine, clock
        self.events = _Events(engine)

    @staticmethod
    def _latest(conn: Connection, key: str, lock: bool = False) -> LedgerEntry | None:
        r = (
            conn.execute(
                text(
                    "SELECT e.*, a.target_deployment FROM execution_attempts e "
                    "JOIN actions a ON a.action_id = e.action_id WHERE e.execution_key = :k "
                    "ORDER BY e.attempt_no DESC LIMIT 1" + (" FOR UPDATE OF e" if lock else "")
                ),
                {"k": key},
            )
            .mappings()
            .first()
        )
        return None if r is None else _entry(r)

    def get(self, key: str) -> LedgerEntry | None:
        with self.engine.connect() as conn:
            return self._latest(conn, key)

    @staticmethod
    def _status(conn: Connection, action_id: str, status: str) -> None:
        conn.execute(
            text(
                "UPDATE actions SET status = CAST(:s AS action_status), version = version + 1 "
                "WHERE action_id = :a AND status <> CAST(:s AS action_status)"
            ),
            {"a": UUID(action_id), "s": status},
        )

    def begin(self, e: LedgerEntry) -> str | None:
        """The intent transaction (Artifact 8): `applying` and the `started` attempt, or the
        database's refusal, with nothing written."""
        try:
            with self.engine.begin() as conn:
                moved = conn.execute(
                    text(
                        "UPDATE actions SET status = 'applying', version = version + 1 "
                        "WHERE action_id = :a AND status = 'approved'"
                    ),
                    {"a": UUID(e.execution_key)},
                ).rowcount
                if moved != 1:
                    return "action is not approved"
                attempt_id = uuid7()
                conn.execute(
                    text(
                        "INSERT INTO execution_attempts (attempt_id, action_id, approval_id, "
                        "action_digest, execution_key, attempt_no, started_at) VALUES "
                        "(:id, :a, :ap, :d, :k, :n, :at)"
                    ),
                    {
                        "id": attempt_id,
                        "a": UUID(e.execution_key),
                        "ap": UUID(e.approval_id),
                        "d": e.action_digest,
                        "k": e.execution_key,
                        "n": e.attempt_no,
                        "at": e.started_at or self.clock(),
                    },
                )
                _event(
                    conn,
                    e.execution_key,
                    "aisre.execution.started.v1",
                    ExecutionStartedData(
                        attempt_id=attempt_id,
                        action_id=UUID(e.execution_key),
                        approval_id=UUID(e.approval_id),
                        action_digest=e.action_digest,
                        execution_key=e.execution_key,
                        attempt_no=e.attempt_no,
                    ),
                )
        except DBAPIError as err:
            if (getattr(err.orig, "sqlstate", None) or "") in REFUSED_STATES:
                return f"the record refused intent: {str(err.orig).splitlines()[0]}"
            raise
        return None

    def put(self, e: LedgerEntry) -> None:
        """Finish the `started` attempt, or add the next row (a reconciliation, or a person's
        resolution), and move the action's status with it. An attempt is final once it leaves
        `started`: a second finish raises, as dispatcher.Ledger does."""
        with self.engine.begin() as conn:
            latest = self._latest(conn, e.execution_key, lock=True)
            if latest is not None and latest.attempt_no == e.attempt_no:
                done = conn.execute(
                    text(
                        "UPDATE execution_attempts SET outcome = CAST(:o AS execution_outcome), "
                        "finished_at = now(), external_ref = :ref, error = :err "
                        "WHERE execution_key = :k AND attempt_no = :n AND outcome = 'started'"
                    ),
                    {
                        "o": e.state.value,
                        "ref": e.external_ref,
                        "err": "; ".join(e.notes)[:1000] or None,
                        "k": e.execution_key,
                        "n": e.attempt_no,
                    },
                ).rowcount
                if done != 1:
                    raise RuntimeError("an attempt is final once it leaves started")
                attempt_id = UUID(str(latest.attempt_id))
            else:
                attempt_id = uuid7()
                r = e.resolution
                conn.execute(
                    text(
                        "INSERT INTO execution_attempts (attempt_id, action_id, approval_id, "
                        "action_digest, execution_key, attempt_no, reconciliation, outcome, "
                        "resolved_by, observed_revision, resolution_reason) VALUES (:id, :a, "
                        ":ap, :d, :k, :n, :rec, CAST(:o AS execution_outcome), :by, :rev, :why)"
                    ),
                    {
                        "id": attempt_id,
                        "a": UUID(e.execution_key),
                        "ap": UUID(e.approval_id),
                        "d": e.action_digest,
                        "k": e.execution_key,
                        "n": e.attempt_no,
                        "rec": e.reconciliation,
                        "o": e.state.value,
                        "by": r.subject if r else None,
                        "rev": r.observed_revision if r else None,
                        "why": r.reason if r else None,
                    },
                )
            self._status(conn, e.execution_key, ACTION_STATUS[e.state])
            data: Strict
            if e.resolution is not None:
                event, actor = "aisre.execution.outcome_resolved.v1", e.resolution.subject
                data = ExecutionOutcomeResolvedData(
                    attempt_id=attempt_id,
                    action_id=UUID(e.execution_key),
                    execution_key=e.execution_key,
                    outcome=ExecutionOutcome(e.state.value),
                    observed_revision=e.resolution.observed_revision,
                    reason=e.resolution.reason,
                )
            else:
                event, actor = "aisre.execution.completed.v1", ACTOR
                data = ExecutionCompletedData(
                    attempt_id=attempt_id,
                    action_id=UUID(e.execution_key),
                    execution_key=e.execution_key,
                    attempt_no=e.attempt_no,
                    outcome=ExecutionOutcome(e.state.value),
                    external_ref=e.external_ref,
                    reconciliation=e.reconciliation,
                )
            _event(conn, e.execution_key, event, data, actor)

    def unresolved(self, deployment: str) -> list[DeploymentBlock]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT action_id, reason FROM deployment_blocks "
                    "WHERE target_deployment = :d AND cleared_at IS NULL ORDER BY opened_at"
                ),
                {"d": deployment},
            )
            return [DeploymentBlock(deployment, str(r[0]), str(r[1])) for r in rows]

    def block_late_apply(self, e: LedgerEntry) -> None:
        """T3.7, FR-55: a call answered after reconciliation recorded `not_applied`. The block goes
        back, once per key, on the attempt the late call belonged to; only a person clears it."""
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO deployment_blocks (attempt_id, action_id, reason, target_deployment) "
                    "SELECT e.attempt_id, e.action_id, :why, a.target_deployment "
                    "FROM execution_attempts e JOIN actions a ON a.action_id = e.action_id "
                    "WHERE e.execution_key = :k AND e.attempt_no = :n "
                    "ON CONFLICT (attempt_id, reason) DO NOTHING"
                ),
                {"why": LATE_APPLY, "k": e.execution_key, "n": e.attempt_no},
            )

    def clear_late_apply(self, execution_key: str, resolution: OutcomeResolution) -> bool:
        with self.engine.begin() as conn:
            done = conn.execute(
                text(
                    "UPDATE deployment_blocks SET cleared_at = now(), cleared_by = :by, "
                    "observed_revision = :rev, resolution_reason = :why "
                    "WHERE action_id = :a AND reason = :late AND cleared_at IS NULL"
                ),
                {
                    "by": resolution.subject,
                    "rev": resolution.observed_revision,
                    "why": resolution.reason,
                    "a": UUID(execution_key),
                    "late": LATE_APPLY,
                },
            ).rowcount
            return bool(done)


class PgFence:
    """fence.FenceState's read side, from fence_state and fence_reasons (Artifact 12.5)."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @property
    def epoch(self) -> int:
        with self.engine.connect() as conn:
            return int(
                conn.execute(text("SELECT epoch FROM fence_state WHERE id = 1")).scalar_one()
            )

    def writes_allowed(self, deployment: str, token_epoch: int) -> tuple[bool, str]:
        with self.engine.connect() as conn:
            epoch = int(
                conn.execute(text("SELECT epoch FROM fence_state WHERE id = 1")).scalar_one()
            )
            rows = conn.execute(
                text(
                    "SELECT scope, reason FROM fence_reasons WHERE cleared_at IS NULL "
                    "AND scope IN ('global', :d) ORDER BY scope, reason"
                ),
                {"d": deployment},
            ).all()
        if token_epoch != epoch:
            return False, f"stale fence token (epoch {token_epoch}, current {epoch})"
        found = [str(r[1]) for r in rows if r[0] == "global"]
        if found:
            return False, "writes disabled: " + ", ".join(found)
        found = [str(r[1]) for r in rows if r[0] == deployment]
        if found:
            return False, f"{deployment} fenced: " + ", ".join(found)
        return True, "ok"
