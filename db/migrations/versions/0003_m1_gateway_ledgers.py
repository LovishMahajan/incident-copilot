"""M1 T1.7: an index for the fleet's daily and monthly ledgers (WR-01).

The fleet ledgers are derived from settled reservations by `settled_at` (aisre.record.budget,
ADR-55, ADR-56), and the gateway reads them before every call, so they need an index on the
settled rows' time. No table changes: the reservation tables and their triggers are the
baseline's.

Revision ID: 0003_m1_gateway_ledgers
Revises: 0001_baseline
Create Date: 2026-11-18
"""

from __future__ import annotations

from alembic import op

revision: str = "0003_m1_gateway_ledgers"
down_revision: str | None = "0001_baseline"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX ix_reservations_settled ON budget_reservations (settled_at)"
        " WHERE status = 'settled'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_reservations_settled")
