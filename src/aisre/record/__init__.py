"""Artifact 7.2, 13.1: the Incident Record, the platform's one store of incidents.

SQLAlchemy Core over psycopg 3, with explicit queries and no ORM (ADR-04). Synchronous, on
purpose: SQLAlchemy's asyncio layer needs greenlet, which isn't in the lock (and isn't built
for every platform the team uses), and nothing at M0's load needs it. The API's routes are
plain `def`, so FastAPI runs them on its thread pool; async callers (the outbox relay, and the
activities in T0.11) call the record through `asyncio.to_thread`, one connection per call, so
the event loop never blocks on the database. Revisit if sustained concurrency outgrows the
thread pool (ADR-51 puts it at 5 or fewer).

Each process connects as its own role (ADR-51): the API and worker as `aisre_app`. Functions
take a `Connection` and never commit; the caller opens the transaction (`engine.begin()`), so
several writes are one unit.
"""

from __future__ import annotations

from pydantic import SecretStr
from sqlalchemy import Engine, create_engine

from aisre.record.incidents import (
    SIGNAL_WORKFLOW,
    START_WORKFLOW,
    Intake,
    UnknownService,
    create_incident,
    dedup_key,
    enqueue,
    load_incident,
    timeline,
    uuid7,
    workflow_id,
)

__all__ = [
    "SIGNAL_WORKFLOW",
    "START_WORKFLOW",
    "Intake",
    "UnknownService",
    "create_incident",
    "dedup_key",
    "enqueue",
    "load_incident",
    "make_engine",
    "timeline",
    "uuid7",
    "workflow_id",
]


def make_engine(url: SecretStr) -> Engine:
    """A pooled engine for one role's URL (`postgresql+psycopg://...`)."""
    return create_engine(url.get_secret_value(), pool_pre_ping=True, pool_size=5, max_overflow=5)
