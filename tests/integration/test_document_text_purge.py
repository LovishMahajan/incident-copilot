"""T4.11 (WR-36, Q10.55; migration 0012): runbook body text gone from PostgreSQL after a purge,
by document ID, with the hashes kept. The mechanism only: the legal deletion policy is not
decided (HG-08), so the CLI refuses without the recorded decision.

A document version with two chunks and an evidence record citing it are planted in a fresh
organisation. Before: nobody may change either table, the retention role included, except
through the purge. After `purge_document_text` as aisre_retention: the body and both chunks are
empty, each chunk keeps its `content_sha256` and the document gains the body's hash, the
reason and the time; the evidence says "content removed" and keeps only the document
reference; the text appears in none of the three tables. A second purge changes nothing.
"""

from __future__ import annotations

import hashlib
import io
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, text, update
from sqlalchemy.exc import DBAPIError

from aisre.cli import main
from aisre.contracts import IncidentCreate
from aisre.record import uuid7
from aisre.record.corpus import purge_document_text
from aisre.record.incidents import create_incident
from aisre.record.tables import outbox

RETENTION = "postgresql+psycopg://aisre_retention:fake-retention-password@127.0.0.1:55432/aisre"
SECRET_TEXT = "Restart the ledger with the break-glass phrase"


@pytest.fixture
def retention_engine(app_engine):
    with app_engine.connect() as conn:
        head = conn.execute(text("SELECT version_num FROM aisre_migrations.alembic_version"))
        if head.scalar_one() < "0012":
            pytest.skip("the stack's schema is older than migration 0012")
    engine = create_engine(RETENTION)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except DBAPIError as e:
        pytest.skip(f"needs aisre_retention's password (README): {e.orig}")
    yield engine
    engine.dispose()


def plant(app_engine, catalogue) -> tuple[uuid.UUID, str]:
    org, service, _ = catalogue
    doc, ref = uuid7(), f"rb-{uuid.uuid4().hex[:8]}"
    body = f"# Ledger\n\n{SECRET_TEXT}.\n\n## Then\n\nCheck the ledger."
    with app_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO documents (document_id, org_id, doc_ref, doc_version, doc_type, "
                "title, body, services, allowed_groups, reviewed_at, owner) VALUES "
                "(:d, :o, :r, 'v1', 'runbook', 'Ledger', :b, ARRAY[:s], ARRAY['ops'], now(), 'ops')"
            ),
            {"d": doc, "o": org, "r": ref, "b": body, "s": service},
        )
        for n, part in enumerate((SECRET_TEXT, "Check the ledger.")):
            conn.execute(
                text(
                    "INSERT INTO chunks (chunk_id, document_id, ordinal, heading_path, body, "
                    "span_start, span_end, content_sha256) VALUES "
                    "(:c, :d, :n, ARRAY['Ledger'], :b, 0, :e, :h)"
                ),
                {
                    "c": uuid7(),
                    "d": doc,
                    "n": n,
                    "b": part,
                    "e": len(part),
                    "h": hashlib.sha256(part.encode()).hexdigest(),
                },
            )
        incident = create_incident(
            conn,
            org_id=org,
            caller_id="dev:purge",
            body=IncidentCreate(
                service=service,
                environment="production",
                started_at=datetime.now(UTC),
                title="purge test",
                source="monitoring",
                fingerprint=f"purge-{uuid.uuid4()}",
            ),
            grouping_window=timedelta(0),
        ).incident_id
        conn.execute(
            update(outbox).where(outbox.c.incident_id == incident).values(delivered_at=func.now())
        )
        conn.execute(
            text(
                "INSERT INTO evidence (evidence_id, incident_id, kind, source_system, tool, "
                "collected_by, summary, attributes, content_sha256) VALUES "
                "(:e, :i, 'runbook', 'corpus', 'retrieve_knowledge', 'test', :s, "
                "jsonb_build_object('doc_id', CAST(:r AS text), 'doc_version', 'v1', 'text', CAST(:s AS text)), :f)"
            ),
            {"e": uuid7(), "i": incident, "s": SECRET_TEXT, "r": ref, "f": "0" * 64},
        )
    return doc, ref


def found(app_engine, doc) -> int:
    with app_engine.connect() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT (SELECT count(*) FROM documents WHERE document_id = :d "
                    "        AND body LIKE '%' || :t || '%') "
                    "     + (SELECT count(*) FROM chunks WHERE document_id = :d "
                    "        AND body LIKE '%' || :t || '%') "
                    "     + (SELECT count(*) FROM evidence e JOIN documents d "
                    "        ON e.attributes ->> 'doc_id' = d.doc_ref AND d.document_id = :d "
                    "        WHERE e.summary LIKE '%' || :t || '%' "
                    "        OR e.attributes::text LIKE '%' || :t || '%')"
                ),
                {"d": doc, "t": SECRET_TEXT},
            ).scalar_one()
        )


def test_document_text_is_removed_by_id_and_the_hashes_stay(
    app_engine, retention_engine, catalogue
):
    doc, _ = plant(app_engine, catalogue)
    assert found(app_engine, doc) == 3  # the body, one chunk, the evidence citing it
    for engine in (app_engine, retention_engine):  # no other change, whoever asks
        with pytest.raises(DBAPIError), engine.begin() as conn:
            conn.execute(text("UPDATE chunks SET body = 'x' WHERE document_id = :d"), {"d": doc})
    with pytest.raises(DBAPIError, match="permission denied"), app_engine.begin() as conn:
        purge_document_text(conn, doc, "legal request")

    out, env = io.StringIO(), {"AISRE_ENV": "test"}
    assert main(["document", "purge-text", str(doc), "--reason", "legal"], env, out) == 1

    with retention_engine.begin() as conn:
        marked = purge_document_text(conn, doc, "legal request (test)")
    assert marked == 1
    assert found(app_engine, doc) == 0
    with app_engine.connect() as conn:
        d = conn.execute(
            text(
                "SELECT body, body_sha256, body_removed_reason FROM documents WHERE document_id = :d"
            ),
            {"d": doc},
        ).one()
        chunks = conn.execute(
            text("SELECT body, content_sha256 FROM chunks WHERE document_id = :d ORDER BY ordinal"),
            {"d": doc},
        ).all()
    assert d.body == "" and len(d.body_sha256) == 64 and d.body_removed_reason
    assert [c.body for c in chunks] == ["", ""]
    assert chunks[0].content_sha256 == hashlib.sha256(SECRET_TEXT.encode()).hexdigest()
    with retention_engine.begin() as conn:
        assert purge_document_text(conn, doc, "again") == 0
    with app_engine.connect() as conn:
        reason = conn.execute(
            text("SELECT body_removed_reason FROM documents WHERE document_id = :d"), {"d": doc}
        ).scalar_one()
    assert reason == "legal request (test)"  # the first purge's record stands
