"""M4 T4.11 (WR-36, Q10.55, Artifact 12.7): runbook text can be removed from PostgreSQL by ID.

`documents` and `chunks` were append-only with no path out, so a document's text stayed after a
legal or safety deletion. Whatever the legal policy decides (who asks, which versions, what
backups do), the text must be removable while the record of what was there stays: so one
document version's body and every chunk of it can be blanked once, by `aisre_retention`, through
`purge_document_text`, which keeps `content_sha256` on each chunk, records the body's own hash,
the time and the reason on the document, and marks the evidence citing that version "content
removed" (protect_evidence then keeps only the document reference). Nothing else may change
either table; DELETE stays refused. Whether and when it is used is the Security lead's decision
(aisre.retrieval.retrieval.purge_document_text refuses until it is recorded).

Revision ID: 0012_m4_document_text_purge
Revises: 0011_m4_schema_version_read
Create Date: 2026-11-19
"""

from __future__ import annotations

from alembic import op

revision: str = "0012_m4_document_text_purge"
down_revision: str | None = "0011_m4_schema_version_read"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
ALTER TABLE documents
  ADD COLUMN body_removed_at timestamptz,
  ADD COLUMN body_removed_reason text,
  ADD COLUMN body_sha256 sha256_hex,
  ADD CONSTRAINT documents_body_removed CHECK (
    (body_removed_at IS NULL) = (body_removed_reason IS NULL)
    AND (body_removed_at IS NULL) = (body_sha256 IS NULL));

-- The one change either table allows: blanking the text, once, as aisre_retention. Every other
-- column is put back from OLD whatever the caller sent.
CREATE FUNCTION protect_document_text() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  v_reason text;
BEGIN
  IF current_user <> 'aisre_retention' THEN
    RAISE EXCEPTION '% is append-only: % rejected', TG_TABLE_NAME, TG_OP
      USING ERRCODE = 'restrict_violation';
  END IF;
  IF TG_TABLE_NAME = 'documents' THEN
    v_reason := NEW.body_removed_reason;
    IF OLD.body_removed_at IS NOT NULL OR COALESCE(v_reason, '') = '' THEN
      RAISE EXCEPTION 'documents: text is removed once, with a reason'
        USING ERRCODE = 'restrict_violation';
    END IF;
    NEW := OLD;
    NEW.body := '';
    NEW.body_sha256 := encode(sha256(convert_to(OLD.body, 'UTF8')), 'hex');
    NEW.body_removed_at := now();
    NEW.body_removed_reason := v_reason;
    RETURN NEW;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM documents d WHERE d.document_id = OLD.document_id
                 AND d.body_removed_at IS NOT NULL) THEN
    RAISE EXCEPTION 'chunks: remove the document''s text first'
      USING ERRCODE = 'restrict_violation';
  END IF;
  NEW := OLD;
  NEW.body := '';
  RETURN NEW;
END $$;

DROP TRIGGER t10_append_only ON documents;
DROP TRIGGER t10_append_only ON chunks;
CREATE TRIGGER t10_append_only BEFORE DELETE ON documents
  FOR EACH ROW EXECUTE FUNCTION protect_row();
CREATE TRIGGER t10_append_only BEFORE DELETE ON chunks
  FOR EACH ROW EXECUTE FUNCTION protect_row();
CREATE TRIGGER t11_text_removal BEFORE UPDATE ON documents
  FOR EACH ROW EXECUTE FUNCTION protect_document_text();
CREATE TRIGGER t11_text_removal BEFORE UPDATE ON chunks
  FOR EACH ROW EXECUTE FUNCTION protect_document_text();

-- Runs as the caller: aisre_retention's grants and the trigger above apply. Returns how many
-- evidence records were marked.
CREATE FUNCTION purge_document_text(p_document_id uuid, p_reason text) RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
  d documents%ROWTYPE;
  v_marked integer;
BEGIN
  SELECT * INTO d FROM documents WHERE document_id = p_document_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'no document %', p_document_id USING ERRCODE = 'no_data_found';
  END IF;
  IF d.body_removed_at IS NULL THEN
    UPDATE documents SET body_removed_reason = p_reason WHERE document_id = p_document_id;
  END IF;
  UPDATE chunks SET body = '' WHERE document_id = p_document_id AND body <> '';
  UPDATE evidence e SET content_removed_at = now(), content_removed_reason = p_reason
    FROM incidents i
   WHERE i.incident_id = e.incident_id AND i.org_id = d.org_id
     AND e.content_removed_at IS NULL
     AND e.attributes ->> 'doc_id' = d.doc_ref
     AND e.attributes ->> 'doc_version' = d.doc_version;
  GET DIAGNOSTICS v_marked = ROW_COUNT;
  RETURN v_marked;
END $$;

GRANT UPDATE (body, body_removed_reason) ON documents TO aisre_retention;
GRANT UPDATE (body) ON chunks TO aisre_retention;
GRANT UPDATE (content_removed_at, content_removed_reason) ON evidence TO aisre_retention;
GRANT EXECUTE ON FUNCTION purge_document_text(uuid, text) TO aisre_retention;
REVOKE EXECUTE ON FUNCTION purge_document_text(uuid, text) FROM PUBLIC;
"""
    )


def downgrade() -> None:
    op.execute(
        """
DROP FUNCTION purge_document_text(uuid, text);
DROP TRIGGER t11_text_removal ON documents;
DROP TRIGGER t11_text_removal ON chunks;
DROP TRIGGER t10_append_only ON documents;
DROP TRIGGER t10_append_only ON chunks;
CREATE TRIGGER t10_append_only BEFORE UPDATE OR DELETE ON documents
  FOR EACH ROW EXECUTE FUNCTION protect_row();
CREATE TRIGGER t10_append_only BEFORE UPDATE OR DELETE ON chunks
  FOR EACH ROW EXECUTE FUNCTION protect_row();
DROP FUNCTION protect_document_text();
REVOKE UPDATE (body, body_removed_reason) ON documents FROM aisre_retention;
REVOKE UPDATE (body) ON chunks FROM aisre_retention;
REVOKE UPDATE (content_removed_at, content_removed_reason) ON evidence FROM aisre_retention;
ALTER TABLE documents DROP CONSTRAINT documents_body_removed,
  DROP COLUMN body_sha256, DROP COLUMN body_removed_reason, DROP COLUMN body_removed_at;
"""
    )
