"""Artifact 8.1, 12.5, 13.5 T4.3 (NFR-16, NFR-38, NFR-39, NFR-44; WR-13): the investigation queue.

`acquire` is one short transaction under one advisory lock, so two workers can't both take the
fifth slot. It queues the incident under its workflow run (one row per incident; a later run
takes the row over, ADR-69), then grants by may_grant: fewer than 5 running, severity
first (confirmed, else proposed, else last), oldest first, and no service holding more than 4
(NFR-44). Nothing is ever dropped: a row stays until its incident leaves `investigating` (or is
cancelled while open) in record_transition, or until the reconciliation job finds its run ended.

`snapshot` reads depth and oldest age per severity, for the queue gauges.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from uuid import UUID

from sqlalchemy import Connection, text

LOCK = "aisre:investigation_queue"
MAX_INVESTIGATIONS = 5  # running at once, across all workers (8.1)
MAX_SLOTS_PER_SERVICE = 4  # NFR-44, provisional: one service can't take every slot


@dataclass(frozen=True)
class QueueRow:
    """One investigation_queue row, with its incident's service. `severity` is the confirmed one,
    else the proposed one, else None, which sorts last as NULL does in the table. `run_id` is the
    workflow run that queued it (ADR-69)."""

    incident_id: str
    service: str
    severity: Optional[str]
    queued_at: datetime
    granted: bool = False
    run_id: str = ""


def may_grant(incident_id: str, rows: list[QueueRow]) -> bool:
    """Inside acquire_investigation_slot's locked transaction: fewer than 5 granted, and this
    incident first in order, severity then oldest. A service holding MAX_SLOTS_PER_SERVICE waits, and
    the next eligible incident takes the slot (NFR-44), the one exception to severity order."""
    if sum(r.granted for r in rows) >= MAX_INVESTIGATIONS:
        return False
    held = Counter(r.service for r in rows if r.granted)
    waiting = sorted(
        (r for r in rows if not r.granted and held[r.service] < MAX_SLOTS_PER_SERVICE),
        key=lambda r: (r.severity is None, r.severity or "", r.queued_at),
    )
    return bool(waiting) and waiting[0].incident_id == incident_id


@dataclass(frozen=True)
class Grant:
    granted: bool
    just_granted: bool  # this call took the slot
    newly_queued: bool  # this call added the row (or took it over for a new run)
    waiting: int  # rows not yet granted, after this call
    queued_at: datetime
    severity: str | None


def acquire(conn: Connection, incident_id: UUID, run_id: str) -> Grant:
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": LOCK})
    sev = conn.execute(
        text(
            "SELECT COALESCE(confirmed_severity, proposed_severity) FROM incidents "
            "WHERE incident_id = :i"
        ),
        {"i": incident_id},
    ).scalar_one()
    row = (
        conn.execute(
            text("SELECT run_id, granted_at FROM investigation_queue WHERE incident_id = :i"),
            {"i": incident_id},
        )
        .mappings()
        .first()
    )
    newly = row is None or row["run_id"] != run_id
    if row is None:
        conn.execute(
            text(
                "INSERT INTO investigation_queue (incident_id, run_id, severity) "
                "VALUES (:i, :r, CAST(:s AS severity))"
            ),
            {"i": incident_id, "r": run_id, "s": sev},
        )
    elif newly:  # an earlier run's row: this run queues afresh (ADR-69)
        conn.execute(
            text(
                "UPDATE investigation_queue SET run_id = :r, severity = CAST(:s AS severity), "
                "queued_at = now(), granted_at = NULL WHERE incident_id = :i"
            ),
            {"i": incident_id, "r": run_id, "s": sev},
        )
    rows = _rows(conn)
    me = next(r for r in rows if r.incident_id == str(incident_id))
    just = not me.granted and may_grant(str(incident_id), rows)
    granted = me.granted or just
    if just:
        conn.execute(
            text("UPDATE investigation_queue SET granted_at = now() WHERE incident_id = :i"),
            {"i": incident_id},
        )
    waiting = sum(not r.granted for r in rows) - (1 if just else 0)
    return Grant(granted, just, newly, waiting, me.queued_at, me.severity)


def _rows(conn: Connection) -> list[QueueRow]:
    return [
        QueueRow(
            str(r["incident_id"]),
            str(r["slug"]),
            None if r["severity"] is None else str(r["severity"]),
            r["queued_at"],
            r["granted_at"] is not None,
            str(r["run_id"]),
        )
        for r in conn.execute(
            text(
                "SELECT q.incident_id, s.slug, q.severity, q.queued_at, q.granted_at, q.run_id "
                "FROM investigation_queue q JOIN incidents i USING (incident_id) "
                "JOIN services s ON s.service_id = i.service_id"
            )
        ).mappings()
    ]


def snapshot(conn: Connection) -> dict[str, tuple[int, float]]:
    """Severity ('none' for unset) -> (waiting, oldest wait in seconds)."""
    return {
        str(sev or "none"): (int(n), float(age or 0))
        for sev, n, age in conn.execute(
            text(
                "SELECT severity, count(*), EXTRACT(EPOCH FROM now() - min(queued_at)) "
                "FROM investigation_queue WHERE granted_at IS NULL GROUP BY severity"
            )
        )
    }
