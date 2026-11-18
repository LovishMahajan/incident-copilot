"""M1 T1.17: an index revision carries its evaluation and its activation (Artifact 10.13).

A new revision is scored on the labelled query set before it is used, and activated only if
every target holds (or at once, for a deletion). Both are decided when the revision is built,
so they are written with it: `index_revisions` stays append-only, and activating a revision is
inserting it with `activated`. New investigations search the organisation's newest activated
revision. No new table: the 37 stay.

Revision ID: 0005_m1_evidence
Revises: 0004_m1_result_reuse
Create Date: 2026-11-18
"""

from __future__ import annotations

from alembic import op

revision: str = "0005_m1_evidence"
down_revision: str | None = "0004_m1_result_reuse"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE index_revisions"
        " ADD COLUMN query_set_version text,"
        " ADD COLUMN evaluation jsonb NOT NULL DEFAULT '{}'::jsonb,"
        " ADD COLUMN activated boolean NOT NULL DEFAULT false,"
        " ADD CONSTRAINT index_revisions_scored CHECK (NOT activated OR evaluation <> '{}'::jsonb)"
    )
    op.execute(
        "CREATE INDEX ix_index_revisions_active ON index_revisions (org_id, built_at DESC)"
        " WHERE activated"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_index_revisions_active")
    op.execute(
        "ALTER TABLE index_revisions DROP COLUMN activated, DROP COLUMN evaluation,"
        " DROP COLUMN query_set_version"
    )
