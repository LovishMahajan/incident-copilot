"""M4 T4.5 (Artifact 12.5, WR-22): `/readyz` checks the schema version.

Each process's readiness compares the database's Alembic version with the head its code was
built for (aisre.reliability.health.SCHEMA_HEAD). The API and worker (aisre_app) and the
dispatcher (aisre_dispatcher) may read that one row, and nothing else in `aisre_migrations`.

Revision ID: 0011_m4_schema_version_read
Revises: 0010_m3_change_author
Create Date: 2026-11-19
"""

from __future__ import annotations

from alembic import op

revision: str = "0011_m4_schema_version_read"
down_revision: str | None = "0010_m3_change_author"
branch_labels: str | None = None
depends_on: str | None = None

ROLES = "aisre_app, aisre_dispatcher"


def upgrade() -> None:
    op.execute(f"GRANT USAGE ON SCHEMA aisre_migrations TO {ROLES}")
    op.execute(f"GRANT SELECT (version_num) ON aisre_migrations.alembic_version TO {ROLES}")


def downgrade() -> None:
    op.execute(f"REVOKE SELECT (version_num) ON aisre_migrations.alembic_version FROM {ROLES}")
    op.execute(f"REVOKE USAGE ON SCHEMA aisre_migrations FROM {ROLES}")
