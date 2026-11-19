"""Artifact 9.4 (ADR-69), 13.5 T1.10: the PostgreSQL-backed ToolCallStore, on `tool_calls`.

The ToolGate counts an incident's budgets and repeats from these rows, so they hold across
activities, workers and restarts, and a repeated call is answered from the `evidence_ids` an
earlier one stored. Parallel calls share the budget, so counting and inserting happen in one
transaction per incident: `admit` takes a transaction-scoped advisory lock on the incident,
counts its rows again, refuses a call that would pass a limit (the same codes as the gate), and
inserts. `tool_calls` is append-only, so a row is written once, after its call ran, with its
result (schema.sql).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Column,
    Connection,
    Engine,
    Integer,
    MetaData,
    Table,
    Text,
    func,
    insert,
    select,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID as PgUUID

from aisre.agent.coordinator import Limits
from aisre.agent.tool_registry import REGISTRY, ToolRefused
from aisre.record.incidents import uuid7

MAX_REPEATS = 2  # an identical call runs once and is answered once from storage (9.4)
_LOCK_CLASS = 0x41495354  # pg_advisory_xact_lock(int, int) class: "AIST", tool calls

metadata = MetaData()
tool_calls = Table(
    "tool_calls",
    metadata,
    Column("tool_call_id", PgUUID, primary_key=True),
    Column("incident_id", PgUUID, nullable=False),
    Column("tool", Text, nullable=False),
    Column("skill", Text, nullable=False),
    Column("call_sha256", Text, nullable=False),
    Column("params", JSONB, nullable=False),
    Column("new_evidence", Integer, nullable=False),
    Column("evidence_ids", ARRAY(PgUUID), nullable=False),
    Column("called_at", TIMESTAMP(timezone=True), nullable=False),
)


def lock_incident(conn: Connection, incident_id: str) -> None:
    """Serialises tool-call writers for one incident until the transaction ends."""
    conn.execute(select(func.pg_advisory_xact_lock(_LOCK_CLASS, func.hashtext(str(incident_id)))))


def _row(r: Any) -> dict[str, Any]:
    return {
        "tool": r.tool,
        "hash": r.call_sha256.strip(),
        "new_evidence": r.new_evidence,
        "skill": r.skill,
        "params": r.params,
        "evidence_ids": [str(e) for e in r.evidence_ids],
    }


def rows_in(conn: Connection, incident_id: str) -> list[dict[str, Any]]:
    t = tool_calls
    found = conn.execute(
        select(t).where(t.c.incident_id == UUID(str(incident_id))).order_by(t.c.called_at)
    )
    return [_row(r) for r in found]


def refusal(
    rows: Sequence[Mapping[str, Any]], tool: str, call_hash: str, max_total: int
) -> ToolRefused | None:
    """The ToolGate's three counting checks, applied to the rows as stored now."""
    if len(rows) >= max_total:
        return ToolRefused("call_budget", f"{max_total} tool calls per incident")
    per_tool = REGISTRY[tool].max_calls_per_incident
    if sum(r["tool"] == tool for r in rows) >= per_tool:
        return ToolRefused("tool_budget", f"{per_tool} calls of {tool} per incident")
    if sum(r["hash"] == call_hash for r in rows) >= MAX_REPEATS:
        return ToolRefused("repeated_call", "same tool and parameters already called twice")
    return None


class PgToolCalls:
    """tool_registry.ToolCallStore over `tool_calls`. `rows` reads; `add` admits one row in its
    own transaction; `admit` does it in the caller's, which must hold `lock_incident`."""

    def __init__(self, engine: Engine, max_total_calls: int = Limits().max_tool_calls) -> None:
        self.engine = engine
        self.max_total_calls = max_total_calls

    def rows(self, incident_id: str) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            return rows_in(conn, incident_id)

    def add(self, incident_id: str, row: dict[str, Any]) -> None:
        with self.engine.begin() as conn:
            lock_incident(conn, incident_id)
            self.admit(conn, incident_id, row)

    def admit(self, conn: Connection, incident_id: str, row: Mapping[str, Any]) -> None:
        """Counts this incident's rows again and inserts, or raises ToolRefused."""
        refused = refusal(
            rows_in(conn, incident_id), row["tool"], row["hash"], self.max_total_calls
        )
        if refused is not None:
            raise refused
        conn.execute(
            insert(tool_calls).values(
                tool_call_id=uuid7(),
                incident_id=UUID(str(incident_id)),
                tool=row["tool"],
                skill=row.get("skill", "collect_evidence"),
                call_sha256=row["hash"],
                params=row.get("params") or {},
                new_evidence=row["new_evidence"],
                evidence_ids=[UUID(e) for e in row.get("evidence_ids", [])],
                called_at=func.clock_timestamp(),
            )
        )
