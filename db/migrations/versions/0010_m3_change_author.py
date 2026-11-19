"""M3 (ADR-49, ADR-73, ST-07): who made the change a fix reverts, recorded with the action.

ADR-49: the author of the change being reverted can't be its only approver. The Approval Service
reads it from the record, never from the caller: the Patch Proposer writes the reverted commit's
author when it stores the action, and the action is frozen after that (freeze_except names no new
column). A rollback reverts the platform's own fix, so it has none. Until an identity system is
chosen, the author's name in the repository is taken as their identity subject, as the fake
issuer's personas are (ADR-73).

Revision ID: 0010_m3_change_author
Revises: 0009_m3_dispatcher_reads
Create Date: 2026-11-19
"""

from __future__ import annotations

from alembic import op

revision: str = "0010_m3_change_author"
down_revision: str | None = "0009_m3_dispatcher_reads"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE actions ADD COLUMN change_author text CHECK (change_author <> '')")
    op.execute(
        "COMMENT ON COLUMN actions.change_author IS "
        "'Author of the change this action reverts (ADR-49); null when unknown, and on a rollback'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE actions DROP COLUMN change_author")
