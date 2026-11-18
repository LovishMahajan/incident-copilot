"""Artifact 7.8: the `Idempotency-Key` store for API writes.

A key is per caller and operation, and lives 24 hours. `claim` runs first in the write's
transaction and `complete` last, in the same one (Artifact 7.7's diagram), so a key is never
left `in_progress` by a failed request: the rollback takes the claim with it. A second
request with the same key waits on the first's row lock, then sees its stored response.

    same key, same body       -> the stored response
    same key, different body  -> KeyReused (409 idempotency_key_reused)
    same key, still running   -> KeyInProgress (409, retry later)
    expired key               -> claimed again, as new
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, func, select, update
from sqlalchemy.dialects.postgresql import insert

from aisre.record.tables import idempotency_keys as k

KEY_TTL = timedelta(hours=24)


class KeyReused(Exception):
    """The key was used for a different request body."""


class KeyInProgress(Exception):
    """The key's first request hasn't finished."""


@dataclass(frozen=True)
class StoredResponse:
    status: int
    body: dict[str, Any]


def request_fingerprint(body: Any) -> str:
    """SHA-256 of the canonical JSON of the request (sorted keys, no spaces)."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def claim(
    conn: Connection, caller_id: str, operation: str, key: str, request_sha256: str
) -> StoredResponse | None:
    """Take the key for this request, or return the response already stored for it."""
    ident = (k.c.caller_id == caller_id, k.c.operation == operation, k.c.idem_key == key)
    fresh = {
        "request_sha256": request_sha256,
        "state": "in_progress",
        "response_status": None,
        "response_body": None,
        "resource_id": None,
        "created_at": func.now(),
        "expires_at": func.now() + KEY_TTL,
    }
    inserted = conn.execute(
        insert(k)
        .values(caller_id=caller_id, operation=operation, idem_key=key, **fresh)
        .on_conflict_do_nothing()
        .returning(k.c.idem_key)
    ).first()
    if inserted is not None:
        return None
    row = conn.execute(
        select(k, (k.c.expires_at <= func.now()).label("expired")).where(*ident).with_for_update()
    ).one()
    if row.expired:
        conn.execute(update(k).where(*ident).values(**fresh))
        return None
    if row.request_sha256 != request_sha256:
        raise KeyReused(key)
    if row.state != "completed":
        raise KeyInProgress(key)
    return StoredResponse(int(row.response_status), dict(row.response_body))


def complete(
    conn: Connection,
    caller_id: str,
    operation: str,
    key: str,
    *,
    status: int,
    body: dict[str, Any],
    resource_id: UUID | None,
) -> None:
    """Store the response under a key this transaction claimed."""
    conn.execute(
        update(k)
        .where(k.c.caller_id == caller_id, k.c.operation == operation, k.c.idem_key == key)
        .values(
            state="completed", response_status=status, response_body=body, resource_id=resource_id
        )
    )
