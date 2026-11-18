"""M2 T2.8 (ADR-72): a patch's pull request is recorded once, when the review system opens it.

`patches` stays append-only except `review_ref`, which goes from null to the draft pull request's
reference once (`freeze_except('review_ref?')`), because the pull request is opened only after the
patch's test passed, well after the row was written. Every other column is frozen, DELETE stays
refused (retention excepted, as before), and `aisre_app` may update that one column only.

Revision ID: 0008_m2_review_ref
Revises: 0007_m2_sandbox
Create Date: 2026-11-18
"""

from __future__ import annotations

from alembic import op

revision: str = "0008_m2_review_ref"
down_revision: str | None = "0007_m2_sandbox"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("DROP TRIGGER t10_append_only ON patches")
    op.execute(
        "CREATE TRIGGER t10_no_delete BEFORE DELETE ON patches"
        " FOR EACH ROW EXECUTE FUNCTION protect_row('action_id')"
    )
    op.execute(
        "CREATE TRIGGER t20_freeze BEFORE UPDATE ON patches"
        " FOR EACH ROW EXECUTE FUNCTION freeze_except('review_ref?')"
    )
    op.execute("GRANT UPDATE (review_ref) ON patches TO aisre_app")


def downgrade() -> None:
    op.execute("REVOKE UPDATE (review_ref) ON patches FROM aisre_app")
    op.execute("DROP TRIGGER t20_freeze ON patches")
    op.execute("DROP TRIGGER t10_no_delete ON patches")
    op.execute(
        "CREATE TRIGGER t10_append_only BEFORE UPDATE OR DELETE ON patches"
        " FOR EACH ROW EXECUTE FUNCTION protect_row('action_id')"
    )
