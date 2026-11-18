"""M2 T2.6 (ADR-71): a test run says what isolation it ran under, and is keyed.

`isolation` is `gvisor` (runsc) or `development-runc`; a `development-runc` result proves the
flow, never isolation, and says so wherever it is shown. `test_key` is the activity's
idempotency key (TestIn.test_key): a second write of the same run finds the first.
`bundle_digest` is the harness's result bundle digest (ResultBundle.digest), null when the run
produced none. No new table: the 39 stay. `test_runs` stays append-only.

Revision ID: 0007_m2_sandbox
Revises: 0006_m1_ops
Create Date: 2026-11-18
"""

from __future__ import annotations

from alembic import op

revision: str = "0007_m2_sandbox"
down_revision: str | None = "0006_m1_ops"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # No test has run before M2, so there is no row to label; one would make this fail, loudly.
    op.execute(
        "ALTER TABLE test_runs"
        " ADD COLUMN isolation text NOT NULL"
        "   CONSTRAINT test_runs_isolation CHECK (isolation IN ('gvisor', 'development-runc')),"
        " ADD COLUMN test_key text CONSTRAINT test_runs_test_key UNIQUE CHECK (test_key <> ''),"
        " ADD COLUMN bundle_digest sha256_hex"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE test_runs DROP COLUMN bundle_digest, DROP COLUMN test_key,"
        " DROP COLUMN isolation"
    )
