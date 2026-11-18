"""Artifact 7.2, 10.5, 10.8: the budget ledgers in PostgreSQL (T1.7, WR-01, WR-03).

The durable form of gateway.BudgetLedger. `incident_budgets` holds each incident's caps and
use; a `budget_reservations` row holds money and tokens while a call is out, and the schema's
triggers move them in the budget row when it settles or is released. The CHECK constraints on
`incident_budgets` reject any overrun, so a reservation over a cap rolls back: the limit holds
before the call (invariant 8, NFR-13, NFR-36, NFR-42), across every worker.

The fleet's daily and monthly ledgers (ADR-55, ADR-56) are derived, not stored: the sum of
settled reservations by the UTC day and month they settled in, with a reconciliation
correction replacing the charged amount in the period it was charged, as the in-memory
FleetBudget does. Every worker reads the same totals, and there is no counter to drift.

`model_attempts` gets one row per attempt (NFR-15: actual cost per incident; ADR-64).

aisre_app has no UPDATE on `incident_budgets`, so it can't lock the row; the trigger's UPDATE
takes the lock, and the CHECK decides. The reason given for a refusal comes from reading the row.
Functions take a Connection and never commit (see aisre.record).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Connection,
    Integer,
    MetaData,
    SmallInteger,
    Table,
    Text,
    and_,
    func,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB, TIMESTAMP, insert
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.exc import IntegrityError

from aisre.gateway.gateway_contracts import ResultStatus, StopReason

# Its own MetaData: these tables are declared here in full, apart from aisre.record.tables.
metadata = MetaData()
Timestamp = TIMESTAMP(timezone=True)
reservation_status = ENUM(
    "reserved", "settled", "released", name="reservation_status", create_type=False
)
model_task = ENUM(
    "hypothesise", "diagnose", "critique", "draft_patch", name="model_task", create_type=False
)
model_attempt_status = ENUM(
    *(s.value for s in ResultStatus), name="model_attempt_status", create_type=False
)
model_stop_reason = ENUM(
    *(s.value for s in StopReason), name="model_stop_reason", create_type=False
)

incident_budgets = Table(
    "incident_budgets",
    metadata,
    Column("incident_id", PgUUID, primary_key=True),
    Column("max_calls", Integer, nullable=False),
    Column("max_input_tokens", Integer, nullable=False),
    Column("max_output_tokens", Integer, nullable=False),
    Column("max_cost_micros", BigInteger, nullable=False),
    Column("used_calls", Integer, nullable=False),
    Column("reserved_cost_micros", BigInteger, nullable=False),
    Column("spent_cost_micros", BigInteger, nullable=False),
    Column("used_input_tokens", Integer, nullable=False),
    Column("used_output_tokens", Integer, nullable=False),
)

budget_reservations = Table(
    "budget_reservations",
    metadata,
    Column("reservation_id", PgUUID, primary_key=True),
    Column("incident_id", PgUUID, nullable=False),
    Column("reserved_cost_micros", BigInteger, nullable=False),
    Column("reserved_input_tokens", Integer, nullable=False),
    Column("reserved_output_tokens", Integer, nullable=False),
    Column("status", reservation_status, nullable=False),
    Column("actual_cost_micros", BigInteger),
    Column("actual_input_tokens", Integer),
    Column("actual_output_tokens", Integer),
    Column("created_at", Timestamp, nullable=False),
    Column("expires_at", Timestamp, nullable=False),
    Column("sent_at", Timestamp),
    Column("settled_at", Timestamp),
)

reservation_corrections = Table(
    "reservation_corrections",
    metadata,
    Column("reservation_id", PgUUID, primary_key=True),
    Column("incident_id", PgUUID, nullable=False),
    Column("actual_cost_micros", BigInteger, nullable=False),
    Column("actual_input_tokens", Integer, nullable=False),
    Column("actual_output_tokens", Integer, nullable=False),
    Column("corrected_by", Text, nullable=False),
    Column("reason", Text, nullable=False),
    Column("corrected_at", Timestamp, nullable=False),
)

model_attempts = Table(
    "model_attempts",
    metadata,
    Column("model_attempt_id", PgUUID, primary_key=True),
    Column("incident_id", PgUUID, nullable=False),
    Column("reservation_id", PgUUID),
    Column("task", model_task, nullable=False),
    Column("step_attempt_no", SmallInteger, nullable=False),
    Column("provider", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("checkpoint", Text),
    Column("adapter_version", Text, nullable=False),
    Column("prompt_template_version", Text, nullable=False),
    Column("prompt_sha256", Text, nullable=False),
    Column("output_schema_sha256", Text),
    Column("input_sha256", Text, nullable=False),
    Column("status", model_attempt_status, nullable=False),
    Column("stop_reason", model_stop_reason, nullable=False),
    Column("validation_errors", JSONB(none_as_null=True)),
    Column("provider_request_id", Text),
    Column("input_tokens", Integer, nullable=False),
    Column("output_tokens", Integer, nullable=False),
    Column("cache_read_input_tokens", Integer, nullable=False),
    Column("cache_write_input_tokens", Integer, nullable=False),
    Column("usage_reported", Boolean, nullable=False),
    Column("cost_micros", BigInteger, nullable=False),
    Column("latency_ms", Integer),
    Column("started_at", Timestamp, nullable=False),
    Column("finished_at", Timestamp),
    Column(
        "result", JSONB(none_as_null=True)
    ),  # migration 0004: the reusable result, `ok` rows only (ADR-61)
)

CHECK_VIOLATION = "23514"
RECONCILER = "spend-reconciliation"


@dataclass(frozen=True)
class Caps:
    """An incident's caps, from the one configuration object (ADR-52, ADR-56)."""

    max_calls: int
    max_input_tokens: int
    max_output_tokens: int
    max_cost_micros: int


@dataclass(frozen=True)
class Use:
    """An incident's budget row; `used_*` include what open reservations hold."""

    caps: Caps
    calls: int
    reserved_cost_micros: int
    spent_cost_micros: int
    input_tokens: int
    output_tokens: int


class OverBudget(Exception):
    """A reservation the incident's caps refuse. `str()` names the cap."""


@dataclass(frozen=True)
class Expired:
    reservation_id: UUID
    sent: bool  # sent: settled at the reserved maximum; never sent: released (ADR-61)


@dataclass(frozen=True)
class Attempt:
    """One `model_attempts` row: every attempt, refused ones included (ADR-64)."""

    model_attempt_id: UUID
    incident_id: UUID
    reservation_id: UUID | None
    task: str
    step_attempt_no: int
    provider: str
    model: str
    checkpoint: str | None
    adapter_version: str
    prompt_template_version: str
    prompt_sha256: str
    output_schema_sha256: str | None
    input_sha256: str
    status: str
    stop_reason: str
    validation_errors: list[str] | None
    provider_request_id: str | None
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_write_input_tokens: int
    usage_reported: bool
    cost_micros: int
    latency_ms: int | None
    started_at: datetime
    finished_at: datetime
    result: dict[str, Any] | None = None  # kept for reuse on `ok` rows only


def _sqlstate(e: IntegrityError) -> str | None:
    return getattr(e.orig, "sqlstate", None)


def use(conn: Connection, incident_id: UUID) -> Use | None:
    row = conn.execute(
        select(incident_budgets).where(incident_budgets.c.incident_id == incident_id)
    ).first()
    if row is None:
        return None
    return Use(
        Caps(row.max_calls, row.max_input_tokens, row.max_output_tokens, row.max_cost_micros),
        row.used_calls,
        row.reserved_cost_micros,
        row.spent_cost_micros,
        row.used_input_tokens,
        row.used_output_tokens,
    )


def refusal(u: Use, input_tokens: int, output_tokens: int, cost_micros: int) -> str | None:
    """The cap a new reservation would break, in gateway.BudgetLedger's words, or None."""
    if u.calls + 1 > u.caps.max_calls:
        return "model call cap"
    if (
        u.input_tokens + input_tokens > u.caps.max_input_tokens
        or u.output_tokens + output_tokens > u.caps.max_output_tokens
    ):
        return "token cap"
    if u.spent_cost_micros + u.reserved_cost_micros + cost_micros > u.caps.max_cost_micros:
        return "spend cap"
    return None


def reserve(
    conn: Connection,
    *,
    reservation_id: UUID,
    incident_id: UUID,
    caps: Caps,
    input_tokens: int,
    output_tokens: int,
    cost_micros: int,
    expires_at: datetime,
) -> None:
    """Hold the estimate, creating the incident's budget row at `caps` on its first call.
    Raises OverBudget, leaving the caller's transaction usable."""
    conn.execute(
        insert(incident_budgets)
        .values(
            incident_id=incident_id,
            max_calls=caps.max_calls,
            max_input_tokens=caps.max_input_tokens,
            max_output_tokens=caps.max_output_tokens,
            max_cost_micros=caps.max_cost_micros,
        )
        .on_conflict_do_nothing()
    )
    current = use(conn, incident_id)
    assert current is not None
    reason = refusal(current, input_tokens, output_tokens, cost_micros)
    if reason is not None:
        raise OverBudget(reason)
    try:
        with conn.begin_nested():  # the trigger's CHECK is the authority under concurrency
            conn.execute(
                insert(budget_reservations).values(
                    reservation_id=reservation_id,
                    incident_id=incident_id,
                    reserved_cost_micros=max(1, cost_micros),
                    reserved_input_tokens=input_tokens,
                    reserved_output_tokens=output_tokens,
                    expires_at=expires_at,
                )
            )
    except IntegrityError as e:
        if _sqlstate(e) != CHECK_VIOLATION:
            raise
        after = use(conn, incident_id)
        reason = refusal(after, input_tokens, output_tokens, cost_micros) if after else None
        raise OverBudget(reason or "budget cap") from None


def mark_sent(conn: Connection, reservation_id: UUID, at: datetime) -> bool:
    """Write `sent_at` before the call leaves. False if the reservation is no longer open
    (expiry released it): the call must not be sent."""
    r = budget_reservations.c
    done = conn.execute(
        update(budget_reservations)
        .where(r.reservation_id == reservation_id, r.status == "reserved", r.sent_at.is_(None))
        .values(sent_at=at)
        .returning(r.reservation_id)
    ).first()
    return done is not None


def settle(
    conn: Connection,
    reservation_id: UUID,
    *,
    cost_micros: int,
    input_tokens: int | None,
    output_tokens: int | None,
    at: datetime,
) -> bool:
    """Charge a sent call. Unknown usage (tokens None) keeps the held estimate and charges
    `cost_micros`, which the caller sets to the reservation (ADR-61); reconciliation corrects it.
    False if expiry got there first.

    Known usage above what the caps leave (the estimate was low) is charged up to the caps:
    money already spent can't be refused, and the incident is at its cap either way. The exact
    figures stay on the attempt's `model_attempts` row."""
    r = budget_reservations.c
    open_row = conn.execute(
        select(budget_reservations).where(
            r.reservation_id == reservation_id, r.status == "reserved"
        )
    ).first()
    if open_row is None:
        return False

    def write(cost: int, tin: int | None, tout: int | None) -> bool:
        done = conn.execute(
            update(budget_reservations)
            .where(r.reservation_id == reservation_id, r.status == "reserved")
            .values(
                status="settled",
                actual_cost_micros=cost,
                actual_input_tokens=tin,
                actual_output_tokens=tout,
                settled_at=at,
            )
            .returning(r.reservation_id)
        ).first()
        return done is not None

    try:
        with conn.begin_nested():
            return write(cost_micros, input_tokens, output_tokens)
    except IntegrityError as e:
        if _sqlstate(e) != CHECK_VIOLATION:
            raise
    u = use(conn, open_row.incident_id)
    assert u is not None
    room_cost = u.caps.max_cost_micros - u.spent_cost_micros - u.reserved_cost_micros
    room_in = u.caps.max_input_tokens - u.input_tokens
    room_out = u.caps.max_output_tokens - u.output_tokens
    return write(
        min(cost_micros, open_row.reserved_cost_micros + room_cost),
        None
        if input_tokens is None
        else min(input_tokens, open_row.reserved_input_tokens + room_in),
        None
        if output_tokens is None
        else min(output_tokens, open_row.reserved_output_tokens + room_out),
    )


def release(conn: Connection, reservation_id: UUID, at: datetime) -> bool:
    """Return a reservation whose call provably never left the process. False if not open."""
    r = budget_reservations.c
    done = conn.execute(
        update(budget_reservations)
        .where(r.reservation_id == reservation_id, r.status == "reserved", r.sent_at.is_(None))
        .values(status="released", settled_at=at)
        .returning(r.reservation_id)
    ).first()
    return done is not None


def expire(conn: Connection, now: datetime) -> list[Expired]:
    """ADR-61, the sweep over expires_at: a sent reservation is charged at the reserved maximum
    and waits for reconciliation; only a never-sent one is released. Rows another sweep holds
    are skipped, so two sweeps never do one row twice."""
    r = budget_reservations.c
    due = conn.execute(
        select(r.reservation_id, r.sent_at, r.reserved_cost_micros)
        .where(r.status == "reserved", r.expires_at <= now)
        .order_by(r.expires_at)
        .with_for_update(skip_locked=True)
    ).all()
    out = []
    for row in due:
        values: dict[str, Any] = (
            {"status": "settled", "actual_cost_micros": row.reserved_cost_micros}
            if row.sent_at is not None
            else {"status": "released"}
        )
        conn.execute(
            update(budget_reservations)
            .where(r.reservation_id == row.reservation_id)
            .values(settled_at=now, **values)
        )
        out.append(Expired(row.reservation_id, row.sent_at is not None))
    return out


def unreconciled(conn: Connection) -> list[UUID]:
    """Settled with unknown usage and not yet corrected (Q10.35), oldest first."""
    r, c = budget_reservations.c, reservation_corrections.c
    return list(
        conn.execute(
            select(r.reservation_id)
            .outerjoin_from(
                budget_reservations,
                reservation_corrections,
                c.reservation_id == r.reservation_id,
            )
            .where(
                r.status == "settled",
                r.actual_input_tokens.is_(None),
                c.reservation_id.is_(None),
            )
            .order_by(r.settled_at)
        ).scalars()
    )


def correct(
    conn: Connection,
    reservation_id: UUID,
    *,
    cost_micros: int,
    input_tokens: int,
    output_tokens: int,
    reason: str,
    by: str = RECONCILER,
) -> None:
    """Record the provider's figures for a reservation settled with unknown usage; the
    schema's trigger moves the difference in the budget row."""
    incident_id = conn.execute(
        select(budget_reservations.c.incident_id).where(
            budget_reservations.c.reservation_id == reservation_id
        )
    ).scalar_one()
    conn.execute(
        insert(reservation_corrections).values(
            reservation_id=reservation_id,
            incident_id=incident_id,
            actual_cost_micros=cost_micros,
            actual_input_tokens=input_tokens,
            actual_output_tokens=output_tokens,
            corrected_by=by,
            reason=reason,
        )
    )


def period_starts(now: datetime) -> tuple[datetime, datetime]:
    """The UTC day and month `now` falls in (ADR-56)."""
    u = now.astimezone(UTC)
    return u.replace(hour=0, minute=0, second=0, microsecond=0), u.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )


def fleet_spend(conn: Connection, now: datetime) -> tuple[int, int]:
    """(today, this month) across all incidents, in millionths of a dollar. A correction
    replaces the amount charged, in the period it was charged."""
    r, c = budget_reservations.c, reservation_corrections.c
    day, month = period_starts(now)
    charged = func.coalesce(c.actual_cost_micros, r.actual_cost_micros)
    row = conn.execute(
        select(
            func.coalesce(func.sum(charged).filter(r.settled_at >= day), 0),
            func.coalesce(func.sum(charged), 0),
        )
        .select_from(
            budget_reservations.outerjoin(
                reservation_corrections, c.reservation_id == r.reservation_id
            )
        )
        .where(and_(r.status == "settled", r.settled_at >= month))
    ).one()
    return int(row[0]), int(row[1])


def record_attempt(conn: Connection, a: Attempt) -> None:
    conn.execute(insert(model_attempts).values(**a.__dict__))


def reusable_result(
    conn: Connection, incident_id: UUID, input_sha256: str
) -> dict[str, Any] | None:
    """The recorded result of the same step input within the incident, if one succeeded (ADR-61).
    Uses ix_model_attempts_reuse."""
    m = model_attempts.c
    found: dict[str, Any] | None = conn.execute(
        select(m.result)
        .where(
            m.incident_id == incident_id,
            m.input_sha256 == input_sha256,
            m.status == "ok",
            m.result.is_not(None),
        )
        .order_by(m.started_at.desc())
        .limit(1)
    ).scalar()
    return found
