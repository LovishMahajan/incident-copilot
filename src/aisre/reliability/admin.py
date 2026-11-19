"""Artifact 12.5, 12.6, 13.5 T4.4 (ADR-69; NFR-21 to NFR-23, NFR-46): the write fence, backups
and restores in PostgreSQL, for the Platform admin's CLI commands (aisre.cli: `fence`, `backup`,
`restore`).

ADR-69 gives fence and restore to the CLI, with no HTTP route in the first version, so unlike
`aisre incident` these connect to PostgreSQL directly, as aisre_app (which may write fence_state
and fence_reasons, and nothing else that is used here). Who acts is a verified token's subject and
roles on every service (`aisre:<role>`); a lead's sign-off is a second verified token, the
stand-in for a signed record (ADR-74). The rules are fence.FenceState's own: the state is read
from PostgreSQL, the FenceState method is applied (it raises PermissionError), and only then is
the change written, in the same transaction.

Backups (12.6, a stand-in for production point-in-time recovery, ADR-74): `take_backup` fences
writes (`backup_quiesce`), waits a bounded time for writes in flight to reach a final ledger
state, runs the configured base-backup command, writes a marker (time, files and their hashes,
the actions still in flight) beside the backup, and lifts the fence, whatever happened. WAL
archiving is PostgreSQL's own (`archive_command`, compose.yaml). `check_backups` reports the
days without a backup and prunes what is older than the 35 days kept (NFR-46, ADR-57).

Restores: `begin_restore` is the first write on a restored database (a new epoch, so every
older fence token is refused, and the restore fence); `restore_report` reconciles every action
not confirmed final against the release process with lookups only (fence.reconcile_after_restore);
`complete_restore` lifts the restore fence only with no action unresolved, an Incident lead's
sign-off and the Platform admin running it. A person's decision on an unresolved action is
named on the command and recorded in the report and the fence's clearing.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, text

from aisre.record.incidents import uuid7
from aisre.reliability.fence import (
    PLATFORM_ADMIN,
    ActionRecord,
    Actor,
    FenceReason,
    FenceState,
    ReconciliationReport,
    reconcile_after_restore,
)

GLOBAL = "global"
QUIESCE_WAIT = timedelta(minutes=10)  # 12.6 step 2
KEEP_DAYS = 35  # backups and WAL (ADR-57, NFR-46)
NOT_FINAL = ("approved", "applying", "outcome_unknown")  # 12.6 step 7: ask the target
MARKER = "marker.json"


class Refused(Exception):
    """A rule refused the command; nothing was written."""


def load(conn: Connection) -> FenceState:
    """fence.FenceState as PostgreSQL holds it (fence_state, the active fence_reasons)."""
    st = FenceState(epoch=int(conn.execute(text("SELECT epoch FROM fence_state")).scalar_one()))
    rows = conn.execute(
        text("SELECT scope, reason, set_by, why FROM fence_reasons WHERE cleared_at IS NULL")
    )
    for scope, reason, set_by, why in rows:
        r = FenceReason(reason)
        if scope == GLOBAL:
            st.global_reasons.add(r)
        else:
            st.deployment_reasons.setdefault(str(scope), set()).add(r)
        if r == FenceReason.MANUAL:
            st.manual[str(scope)] = (str(set_by), str(why or ""))
    return st


def _set(conn: Connection, reason: FenceReason, scope: str, by: str, why: str | None) -> bool:
    """True if set now, False if it was already set (the one-active index)."""
    made = conn.execute(
        text(
            "INSERT INTO fence_reasons (fence_id, scope, reason, why, set_by) "
            "VALUES (:i, :s, :r, :w, :b) ON CONFLICT DO NOTHING"
        ),
        {"i": uuid7(), "s": scope, "r": reason.value, "w": why, "b": by},
    ).rowcount
    return bool(made)


def _clear(conn: Connection, reason: FenceReason, scope: str, by: str) -> int:
    return int(
        conn.execute(
            text(
                "UPDATE fence_reasons SET cleared_by = :b, cleared_at = now() "
                "WHERE scope = :s AND reason = :r AND cleared_at IS NULL"
            ),
            {"b": by, "s": scope, "r": reason.value},
        ).rowcount
    )


def show(engine: Engine) -> dict[str, Any]:
    with engine.connect() as conn:
        epoch = int(conn.execute(text("SELECT epoch FROM fence_state")).scalar_one())
        active = [
            {"scope": s, "reason": r, "set_by": b, "why": w, "set_at": a.isoformat()}
            for s, r, b, w, a in conn.execute(
                text(
                    "SELECT scope, reason, set_by, why, set_at FROM fence_reasons "
                    "WHERE cleared_at IS NULL ORDER BY set_at"
                )
            )
        ]
    return {"epoch": epoch, "active": active, "writes_disabled": bool(active)}


def set_manual(engine: Engine, by: Actor, why: str, deployment: str | None = None) -> None:
    """An Incident lead or Platform admin fences writes, with a reason (ADR-50)."""
    with engine.begin() as conn:
        try:
            load(conn).fence(FenceReason.MANUAL, deployment, actor=by, why=why)
        except PermissionError as e:
            raise Refused(str(e)) from None
        if not _set(conn, FenceReason.MANUAL, deployment or GLOBAL, by.subject, why):
            raise Refused(f"a manual fence is already set on {deployment or GLOBAL}")


def lift_manual(engine: Engine, by: Actor, sign_off: Actor, deployment: str | None = None) -> None:
    """The Platform admin lifts a manual fence, with an Incident lead's sign-off (ADR-50)."""
    scope = deployment or GLOBAL
    with engine.begin() as conn:
        st = load(conn)
        if scope not in st.manual:
            raise Refused(f"no manual fence on {scope}")
        try:
            st.lift(FenceReason.MANUAL, deployment, actor=by, sign_off=sign_off)
        except PermissionError as e:
            raise Refused(str(e)) from None
        _clear(conn, FenceReason.MANUAL, scope, f"{by.subject}, signed off by {sign_off.subject}")


# -- restore (12.6 steps 2, 7, 9, 10) -------------------------------------------------------
def begin_restore(engine: Engine, by: Actor) -> int:
    """The first write on a restored database: a new epoch and the restore fence."""
    if by.role != PLATFORM_ADMIN:
        raise Refused("only the platform admin runs a restore")
    with engine.begin() as conn:
        st = load(conn)
        epoch = st.begin_restore()
        conn.execute(
            text("UPDATE fence_state SET epoch = :e, updated_at = now() WHERE id = 1"),
            {"e": epoch},
        )
        _set(conn, FenceReason.RESTORED, GLOBAL, by.subject, "restore began")
    return epoch


def not_final(conn: Connection) -> list[ActionRecord]:
    """Every action the restored ledger doesn't show final, with what it last recorded."""
    rows = conn.execute(
        text(
            "SELECT a.action_id, a.target_deployment, a.expected_after_revision, a.base_revision, "
            "(SELECT e.outcome FROM execution_attempts e WHERE e.action_id = a.action_id "
            " ORDER BY e.attempt_no DESC LIMIT 1) "
            "FROM actions a WHERE a.status = ANY(:s) ORDER BY a.created_at"
        ),
        {"s": list(NOT_FINAL)},
    )
    return [
        ActionRecord(str(i), str(d), str(after), str(base), str(outcome or "pending"))
        for i, d, after, base, outcome in rows
    ]


def restore_report(
    engine: Engine,
    lookup: Callable[[str, str], str | None],
    revision: Callable[[str], str],
    sleep: Callable[[float], None] = time.sleep,
    recheck_limit_s: float = 900.0,
) -> ReconciliationReport:
    """A lookup that fails leaves its action unresolved: a person decides (12.6 step 9)."""
    with engine.connect() as conn:
        actions = not_final(conn)

    def ask(deployment: str, key: str) -> str | None:
        try:
            return lookup(deployment, key)
        except Exception:  # noqa: BLE001 (no answer is not an answer)
            return "lookup_failed"

    def at(deployment: str) -> str:
        try:
            return revision(deployment)
        except Exception:  # noqa: BLE001
            return ""

    return reconcile_after_restore(actions, ask, at, recheck_limit_s=recheck_limit_s, sleep=sleep)


def complete_restore(
    engine: Engine,
    report: ReconciliationReport,
    signed_by: Actor,
    run_by: Actor,
    decided: dict[str, str] | None = None,
) -> None:
    """Lifts the restore fence (12.6 step 10). `decided` is a person's decision on each
    unresolved action (step 9), made by the Incident lead who signs."""
    decided = decided or {}
    unknown = set(decided) - set(report.unresolved)
    if unknown:
        raise Refused(f"decisions name actions that aren't unresolved: {sorted(unknown)}")
    left = ReconciliationReport(
        report.resolved | decided, [a for a in report.unresolved if a not in decided]
    )
    with engine.begin() as conn:
        st = load(conn)
        if FenceReason.RESTORED not in st.global_reasons:
            raise Refused("no restore fence is set")
        try:
            st.complete_restore(left, signed_by, run_by)
        except PermissionError as e:
            raise Refused(str(e)) from None
        why = f"{run_by.subject}, signed off by {signed_by.subject}"
        if decided:
            why += "; decided " + ", ".join(f"{a}={o}" for a, o in sorted(decided.items()))
        _clear(conn, FenceReason.RESTORED, GLOBAL, why)


# -- backups (12.6 nightly backup, NFR-46) ----------------------------------------------------
@dataclass
class Marker:
    backup_id: str
    started_at: str
    finished_at: str
    taken_by: str
    fenced_seconds: float
    in_flight: list[str]
    files: dict[str, str] = field(default_factory=dict)  # name -> sha256

    def save(self, folder: Path) -> None:
        (folder / MARKER).write_text(json.dumps(asdict(self), indent=2) + "\n")


def in_flight(conn: Connection) -> list[str]:
    """Actions with an attempt started and not yet finished: a write in flight (12.6 step 2)."""
    return [
        str(a)
        for a in conn.execute(
            text(
                "SELECT DISTINCT e.action_id FROM execution_attempts e "
                "JOIN actions a USING (action_id) "
                "WHERE e.outcome = 'started' AND a.status = 'applying'"
            )
        ).scalars()
    ]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def take_backup(
    engine: Engine,
    dest: Path,
    command: Sequence[str],
    by: Actor,
    backup_id: str | None = None,
    wait: timedelta = QUIESCE_WAIT,
    run: Callable[[Sequence[str]], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Marker:
    """12.6's quiesced backup. `command` takes `{id}` and `{dest}`; it must write the backup
    into dest/base/<id>/. The fence is lifted whatever happens."""
    if by.role != PLATFORM_ADMIN:
        raise Refused("only the platform admin takes a backup")
    bid = backup_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    folder = dest / "base" / bid
    started = datetime.now(UTC)
    with engine.begin() as conn:
        quiesced = _set(conn, FenceReason.BACKUP_QUIESCE, GLOBAL, by.subject, "nightly backup")
    t0 = time.monotonic()
    try:
        deadline = t0 + wait.total_seconds()
        while True:
            with engine.connect() as conn:
                flying = in_flight(conn)
            if not flying or time.monotonic() >= deadline:
                break
            sleep(5)
        argv = [a.format(id=bid, dest=str(dest)) for a in command]
        (run or _run)(argv)
    finally:
        if quiesced:
            with engine.begin() as conn:
                _clear(conn, FenceReason.BACKUP_QUIESCE, GLOBAL, by.subject)
    fenced = time.monotonic() - t0
    if not folder.is_dir():
        raise Refused(f"the backup command wrote nothing to {folder}")
    marker = Marker(
        bid,
        started.isoformat(),
        datetime.now(UTC).isoformat(),
        by.subject,
        round(fenced, 1),
        flying,  # recorded for reconciliation after any restore from it (12.6)
        {p.name: _sha256(p) for p in sorted(folder.iterdir()) if p.is_file()},
    )
    marker.save(folder)
    return marker


def _run(argv: Sequence[str]) -> None:
    out = subprocess.run(list(argv), capture_output=True, text=True, check=False)  # noqa: S603
    if out.returncode != 0:
        raise Refused(f"backup command failed ({out.returncode}): {out.stderr[-500:]}")


def markers(dest: Path) -> list[Marker]:
    found = []
    for path in sorted((dest / "base").glob(f"*/{MARKER}")):
        found.append(Marker(**json.loads(path.read_text())))
    return found


def check_backups(
    dest: Path, today: date, days: int = 7, keep_days: int = KEEP_DAYS, prune: bool = False
) -> dict[str, Any]:
    """NFR-46's audit: days in the last `days` with no backup, and backups past `keep_days`
    (deleted with `prune`, with the WAL older than the oldest backup kept)."""
    taken = markers(dest)
    have = {datetime.fromisoformat(m.started_at).date() for m in taken}
    missed = [
        (today - timedelta(days=n)).isoformat()
        for n in range(days)
        if today - timedelta(days=n) not in have
    ]
    cutoff = today - timedelta(days=keep_days)
    old = [m for m in taken if datetime.fromisoformat(m.started_at).date() < cutoff]
    removed_wal = 0
    if prune:
        for m in old:
            folder = dest / "base" / m.backup_id
            for p in folder.iterdir():
                p.unlink()
            folder.rmdir()
        kept = [m for m in taken if m not in old]
        oldest = min((datetime.fromisoformat(m.started_at) for m in kept), default=None)
        wal = dest / "wal"
        if oldest is not None and wal.is_dir():
            for p in wal.iterdir():
                if datetime.fromtimestamp(p.stat().st_mtime, UTC) < oldest - timedelta(hours=1):
                    p.unlink()
                    removed_wal += 1
    return {
        "backups": [m.backup_id for m in taken],
        "missed_days": missed,
        "past_retention": [m.backup_id for m in old],
        "pruned": prune,
        "wal_files_removed": removed_wal,
    }
