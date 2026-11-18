"""M1 T1.8: the recorded result on model_attempts, for result reuse (ADR-61).

A step whose input (`input_sha256`) already has a schema-valid result within the incident gets
that result back with no reservation and no call, also after a worker is lost and Temporal
re-runs the activity (ADR-27). The baseline keeps the output only as `output_artifact_id`, and
no artifact store exists yet, so the gateway's normalised result is kept here, on `ok` rows
only. It is deleted with the incident (purge_incident deletes model_attempts).

Revision ID: 0004_m1_result_reuse
Revises: 0003_m1_gateway_ledgers
Create Date: 2026-11-18
"""

from __future__ import annotations

from alembic import op

revision: str = "0004_m1_result_reuse"
down_revision: str | None = "0003_m1_gateway_ledgers"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE model_attempts ADD COLUMN result jsonb,"
        " ADD CONSTRAINT model_attempts_result_ok CHECK (result IS NULL OR status = 'ok')"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE model_attempts DROP COLUMN result")
