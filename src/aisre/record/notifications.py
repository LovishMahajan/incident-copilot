"""Artifact 7.6, 7.8 (T1.19, T1.20): the Notifier's record and consumer cursors (0006_m1_ops).

`claim` adds a notification row under its key, in the caller's transaction, or returns None if
the key is taken: the sender sends only after a claim, and a failed send rolls the claim back,
so a message is recorded once and retried until it goes. A second sender waits on the first's
uncommitted row, then sees it taken.

Cursors are a consumer's position, read and moved in the caller's transaction, `FOR UPDATE`, so
two workers running the same consumer take turns rather than both acting on a row.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import ARRAY, Column, Connection, Table, Text, func, select, update
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.dialects.postgresql import insert

from aisre.record.dead_letters import dead_letters
from aisre.record.tables import Timestamp, incidents, metadata

notifications = Table(
    "notifications",
    metadata,
    Column("notification_key", Text, primary_key=True),
    Column("incident_id", PgUUID),
    Column("dead_letter_id", PgUUID),
    Column("kind", Text, nullable=False),
    Column("audience", ARRAY(Text), nullable=False),
    Column("channel", Text, nullable=False),
    Column("detail", Text),
    Column("sent_at", Timestamp, nullable=False),
)

consumer_cursors = Table(
    "consumer_cursors",
    metadata,
    Column("consumer", Text, primary_key=True),
    Column("position", Text, nullable=False),
    Column("updated_at", Timestamp, nullable=False),
)


@dataclass(frozen=True)
class Claimed:
    """A new notification row. `first` when it is the first for its incident or alert;
    `alert_at` is when the alert arrived (the incident's creation, or the dead letter's)."""

    first: bool
    alert_at: datetime | None


def claim(
    conn: Connection,
    *,
    key: str,
    kind: str,
    audience: list[str],
    channel: str,
    incident_id: UUID | None = None,
    dead_letter_id: UUID | None = None,
    detail: str | None = None,
) -> Claimed | None:
    """Exactly one of `incident_id` and `dead_letter_id`. None if `key` was used already."""
    first = True
    alert_at: Any = None
    if incident_id is not None:
        first = not conn.execute(
            select(func.count())
            .select_from(notifications)
            .where(notifications.c.incident_id == incident_id)
        ).scalar_one()
        alert_at = conn.execute(
            select(incidents.c.created_at).where(incidents.c.incident_id == incident_id)
        ).scalar()
    elif dead_letter_id is not None:
        alert_at = conn.execute(
            select(dead_letters.c.received_at).where(
                dead_letters.c.dead_letter_id == dead_letter_id
            )
        ).scalar()
    taken = conn.execute(
        insert(notifications)
        .values(
            notification_key=key,
            incident_id=incident_id,
            dead_letter_id=dead_letter_id,
            kind=kind,
            audience=audience,
            channel=channel,
            detail=detail,
            sent_at=func.clock_timestamp(),
        )
        .on_conflict_do_nothing()
        .returning(notifications.c.notification_key)
    ).first()
    if taken is None:
        return None
    return Claimed(first, alert_at)


def cursor(conn: Connection, consumer: str) -> str | None:
    """The consumer's position, locked until the transaction ends; None before its first."""
    conn.execute(
        insert(consumer_cursors)
        .values(consumer=consumer, position="", updated_at=func.now())
        .on_conflict_do_nothing()
    )
    position = conn.execute(
        select(consumer_cursors.c.position)
        .where(consumer_cursors.c.consumer == consumer)
        .with_for_update()
    ).scalar_one()
    return str(position) or None


def advance(conn: Connection, consumer: str, position: str) -> None:
    conn.execute(
        update(consumer_cursors)
        .where(consumer_cursors.c.consumer == consumer)
        .values(position=position, updated_at=func.now())
    )


def cursors(conn: Connection, prefix: str) -> dict[str, str]:
    rows = conn.execute(
        select(consumer_cursors.c.consumer, consumer_cursors.c.position).where(
            consumer_cursors.c.consumer.startswith(prefix)
        )
    )
    return {str(r[0]): str(r[1]) for r in rows}


def dead_letters_after(
    conn: Connection, after: UUID | None, *, limit: int, settle: timedelta
) -> list[Any]:
    """Dead letters in ID order after `after`, received at least `settle` ago: an ID is taken
    before its row commits, so a row younger than that could still appear behind the cursor."""
    d = dead_letters
    query = (
        select(d.c.dead_letter_id, d.c.org_id, d.c.reason, d.c.service_slug, d.c.received_at)
        .where(d.c.received_at <= func.now() - settle)
        .order_by(d.c.dead_letter_id)
        .limit(limit)
    )
    if after is not None:
        query = query.where(d.c.dead_letter_id > after)
    return list(conn.execute(query))
