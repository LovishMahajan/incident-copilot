"""M3 T3.4 (ADR-51, ADR-73): what the dispatcher's role must read to re-check an action.

The dispatcher rebuilds the immutable action and its digest from trusted storage
(aisre.record.approvals.load_action), applies ADR-49's high-impact rule to trusted facts, and
hashes the stored patch bytes (ST-13). For that it reads, and only reads: a service's name,
criticality and hold period, an environment's name, an incident's confirmed severity, and a
patch artifact's storage key and hash. Column grants, so nothing else on those tables is
visible to it.

Revision ID: 0009_m3_dispatcher_reads
Revises: 0008_m2_review_ref
Create Date: 2026-11-19
"""

from __future__ import annotations

from alembic import op

revision: str = "0009_m3_dispatcher_reads"
down_revision: str | None = "0008_m2_review_ref"
branch_labels: str | None = None
depends_on: str | None = None

GRANTS = [
    "SELECT (service_id, slug, critical, hold_seconds) ON services",
    "SELECT (environment_id, slug) ON environments",
    "SELECT (incident_id, confirmed_severity) ON incidents",
    "SELECT (artifact_id, storage_key, content_sha256) ON artifacts",
]


def upgrade() -> None:
    for g in GRANTS:
        op.execute(f"GRANT {g} TO aisre_dispatcher")


def downgrade() -> None:
    for g in GRANTS:
        op.execute(f"REVOKE {g} FROM aisre_dispatcher")
