"""ADR-58, Artifact 7.4 (T1.2): rejected alerts, stored raw in `dead_letters`.

"No alert is ever dropped": an alert intake refuses (invalid, unknown service, forbidden
service) gets a dead-letter row with the reason, the caller and the payload as received, and
no incident. The table is append-only (schema.sql); the retention role deletes a row once
`retain_until` passes.

The incident lead is told through the Notifier (aisre.notify.notifier, T1.19), which reads new
rows by `dead_letter_id` (UUIDv7, so arrival order) after its stored cursor, as Artifact 7.6
asks of every consumer. The API's `aisre.intake.dead_lettered` log line is logged as well.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Column, Connection, Table, Text, func, insert
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID

from aisre.record.incidents import uuid7
from aisre.record.tables import Timestamp, metadata

# Raw alert content: the short-lived class (ADR-57's 30 days for raw collected data).
# Provisional, like every ADR-57 period.
RETENTION = timedelta(days=30)
MAX_TEXT = 64 * 1024  # a payload that isn't a JSON object is kept as text, cut to this

dead_letters = Table(
    "dead_letters",
    metadata,
    Column("dead_letter_id", PgUUID, primary_key=True),
    Column("org_id", PgUUID, nullable=False),
    Column("received_at", Timestamp, nullable=False),
    Column("caller_id", Text, nullable=False),
    Column("reason", Text, nullable=False),
    Column("detail", Text),
    Column("service_slug", Text),
    Column("environment_slug", Text),
    Column("raw_payload", JSONB, nullable=False),
    Column("retain_until", Timestamp, nullable=False),
)


def as_stored(raw: bytes, parsed: Any) -> dict[str, Any]:
    """The payload as `raw_payload` holds it: the object itself, else the text received."""
    if isinstance(parsed, dict):
        return parsed
    return {"unparsed": raw[:MAX_TEXT].decode("utf-8", errors="replace")}


def add_dead_letter(
    conn: Connection,
    *,
    org_id: UUID,
    caller_id: str,
    reason: str,
    raw_payload: dict[str, Any],
    detail: str | None = None,
    service_slug: str | None = None,
    environment_slug: str | None = None,
) -> UUID:
    """Store one rejected alert. Runs in the caller's transaction and doesn't commit."""
    dead_letter_id = uuid7()
    conn.execute(
        insert(dead_letters).values(
            dead_letter_id=dead_letter_id,
            org_id=org_id,
            received_at=func.now(),
            caller_id=caller_id,
            reason=reason,
            detail=detail,
            service_slug=service_slug,
            environment_slug=environment_slug,
            raw_payload=raw_payload,
            retain_until=func.now() + RETENTION,
        )
    )
    return dead_letter_id
