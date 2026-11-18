"""M1 T1.19, T1.20: the Notifier's record and the cursors of the worker's own consumers.

`notifications` is the Notifier's own record (Artifact 7.8): one row per message sent, keyed
by the sender's idempotency key, so a retried send is dropped and the first message for an
incident or a rejected alert can be told apart (aisre_time_to_first_notification_seconds).
`incident_id` has no foreign key: purge_incident deletes an incident's tables by name, and a
reference would block it; the retention role deletes these rows by age.

`consumer_cursors` holds each consumer's position (Artifact 7.6: "consumers keep a cursor"):
the Notifier's last dead letter, by `dead_letter_id`, and the audit copy's last copied event
per incident (`audit_copy:<incident_id>`, the event's seq).

The API and worker (aisre_app) may read and add notifications, and read, add and move
cursors. Nothing may change a notification.

Revision ID: 0006_m1_ops
Revises: 0004_m1_result_reuse
Create Date: 2026-11-18
"""

from __future__ import annotations

from alembic import op

revision: str = "0006_m1_ops"
down_revision: str | None = "0004_m1_result_reuse"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE notifications (
          notification_key text PRIMARY KEY CHECK (notification_key <> ''),
          incident_id      uuid,
          dead_letter_id   uuid,
          kind             text NOT NULL,
          audience         text[] NOT NULL,
          channel          text NOT NULL,
          detail           text,
          sent_at          timestamptz NOT NULL DEFAULT now(),
          CHECK (num_nonnulls(incident_id, dead_letter_id) = 1)
        );
        CREATE INDEX ix_notifications_incident ON notifications (incident_id, sent_at)
          WHERE incident_id IS NOT NULL;
        CREATE INDEX ix_notifications_time ON notifications (sent_at);
        CREATE TRIGGER t10_append_only BEFORE UPDATE ON notifications
          FOR EACH ROW EXECUTE FUNCTION protect_row();

        CREATE TABLE consumer_cursors (
          consumer   text PRIMARY KEY,
          position   text NOT NULL,
          updated_at timestamptz NOT NULL DEFAULT now()
        );

        GRANT SELECT, INSERT ON notifications TO aisre_app;
        GRANT SELECT, INSERT, UPDATE ON consumer_cursors TO aisre_app;
        GRANT SELECT, DELETE ON notifications, consumer_cursors TO aisre_retention;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE consumer_cursors; DROP TABLE notifications;")
