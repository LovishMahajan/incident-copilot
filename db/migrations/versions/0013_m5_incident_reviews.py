"""M5 T5.2, T5.3 (Artifact 1.6, ADR-65, ADR-75): the incident review's findings, and a reader.

Artifact 1.6's pilot measures need what only the incident review knows: the cause the
responders found and when, whether it confirms the platform's proposed cause, whether the
platform should have handed over instead of naming one (ADR-65's under-escalation), the
engineer hours, and whether a fix had to be reversed. Nothing recorded that. `incident_reviews`
is one row per incident, written once by the Incident lead through
`POST /v1/incidents/{id}/incident-review` (ADR-50: the row of "Confirm severity, close"), and
naming the diagnosis it judged. Rows go when the incident is purged (ON DELETE CASCADE, so
purge_incident needs no change); nothing else may change or delete one.

`aisre_reporting` is the reports' role (aisre.pilot.report): SELECT on the tables they read
and nothing else. Created without a password, like the others; set it outside this file.

Revision ID: 0013_m5_incident_reviews
Revises: 0012_m4_document_text_purge
Create Date: 2026-11-19
"""

from __future__ import annotations

from alembic import op

revision: str = "0013_m5_incident_reviews"
down_revision: str | None = "0012_m4_document_text_purge"
branch_labels: str | None = None
depends_on: str | None = None

# What the reports read: aisre.pilot.report and aisre.pilot.approval_review.
REPORTING_READS = (
    "services, environments, incidents, diagnoses, hypotheses, hypothesis_evidence, evidence, "
    "actions, patches, "
    "test_runs, approval_requests, approval_decisions, timeline_events, incident_budgets, "
    "incident_reviews"
)


def upgrade() -> None:
    op.execute(
        f"""
CREATE TABLE incident_reviews (
  incident_id      uuid PRIMARY KEY REFERENCES incidents ON DELETE CASCADE,
  diagnosis_id     uuid,                  -- the diagnosis judged; null if there was none
  cause            text NOT NULL CHECK (cause <> ''),
  cause_category   hypothesis_category NOT NULL,
  cause_identified_at timestamptz NOT NULL,  -- when the responders identified it
  proposed_cause_confirmed boolean,       -- null: the platform proposed no cause
  should_have_handed_over boolean NOT NULL,  -- the evidence didn't support naming a cause
  engineer_hours   numeric(7, 2) CHECK (engineer_hours >= 0),
  fix_reversed     boolean,               -- null: no fix was applied
  reviewed_by      text NOT NULL CHECK (reviewed_by NOT LIKE 'system:%'),
  recorded_at      timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (incident_id, diagnosis_id)
    REFERENCES diagnoses (incident_id, diagnosis_id) ON DELETE CASCADE,
  CHECK (diagnosis_id IS NOT NULL OR proposed_cause_confirmed IS NULL)
);
COMMENT ON TABLE incident_reviews IS
  'The incident review''s findings for Artifact 1.6''s pilot measures (ADR-75); written once';

GRANT SELECT, INSERT ON incident_reviews TO aisre_app;
GRANT SELECT ON incident_reviews TO aisre_retention;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aisre_reporting') THEN
    CREATE ROLE aisre_reporting LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
  END IF;
END $$;
GRANT USAGE ON SCHEMA public TO aisre_reporting;
GRANT SELECT ON {REPORTING_READS} TO aisre_reporting;
"""
    )


def downgrade() -> None:
    op.execute(
        f"""
REVOKE ALL ON {REPORTING_READS} FROM aisre_reporting;
REVOKE USAGE ON SCHEMA public FROM aisre_reporting;
DROP TABLE incident_reviews;
"""
    )
