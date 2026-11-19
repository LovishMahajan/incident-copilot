"""Artifact 10.9 (ADR-62), 13.5 T1.17: the retrieval corpus in PostgreSQL (WR-04).

`documents`, `chunks`, `index_revisions` and `index_revision_documents` hold the corpus and
every revision built from it; all four are append-only, so a revision never changes once
written. A document version is stored once and shared by every revision that includes it, its
chunks with it. Each revision row also holds its 10.13 evaluation, the query set version it
was scored on and whether it was activated, all written with it (migration 0005_m1_evidence):
activating is building, never an update. Investigations search the newest activated revision
of their organisation; a revision an incident recorded stays loadable (`load_revision`).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    Column,
    Connection,
    Integer,
    MetaData,
    Table,
    Text,
    insert,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.dialects.postgresql import insert as pg_insert

from aisre.record.incidents import uuid7
from aisre.retrieval.corpus import Build, QuerySet, build
from aisre.retrieval.retrieval import Document, IndexRevision

Timestamp = TIMESTAMP(timezone=True)
metadata = MetaData()

documents = Table(
    "documents",
    metadata,
    Column("document_id", PgUUID, primary_key=True),
    Column("org_id", PgUUID, nullable=False),
    Column("doc_ref", Text, nullable=False),
    Column("doc_version", Text, nullable=False),
    Column("doc_type", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("body", Text, nullable=False),
    Column("services", ARRAY(Text), nullable=False),
    Column("allowed_groups", ARRAY(Text), nullable=False),
    Column("reviewed_at", Timestamp, nullable=False),
    Column("owner", Text, nullable=False),
    Column("deprecated", Boolean, nullable=False),
)
chunks = Table(
    "chunks",
    metadata,
    Column("chunk_id", PgUUID, primary_key=True),
    Column("document_id", PgUUID, nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("heading_path", ARRAY(Text), nullable=False),
    Column("body", Text, nullable=False),
    Column("span_start", Integer, nullable=False),
    Column("span_end", Integer, nullable=False),
    Column("content_sha256", Text, nullable=False),
)
index_revisions = Table(
    "index_revisions",
    metadata,
    Column("index_revision_id", PgUUID, primary_key=True),
    Column("org_id", PgUUID, nullable=False),
    Column("revision_hash", Text, nullable=False),
    Column("built_at", Timestamp, nullable=False),
    Column("query_set_version", Text),
    Column("evaluation", JSONB, nullable=False),
    Column("activated", Boolean, nullable=False),
)
index_revision_documents = Table(
    "index_revision_documents",
    metadata,
    Column("index_revision_id", PgUUID, primary_key=True),
    Column("document_id", PgUUID, primary_key=True),
    Column("stale", Boolean, nullable=False),
)


def _document_id(conn: Connection, org_id: UUID, doc: Document) -> tuple[UUID, bool]:
    """The stored version's ID, inserting it first if new. Returns (ID, inserted)."""
    new_id = uuid7()
    inserted = conn.execute(
        pg_insert(documents)
        .values(
            document_id=new_id,
            org_id=org_id,
            doc_ref=doc.doc_id,
            doc_version=doc.version,
            doc_type=doc.doc_type,
            title=doc.title,
            body=doc.text,
            services=sorted(doc.services),
            allowed_groups=sorted(doc.allowed_groups),
            reviewed_at=doc.reviewed_at,
            owner=doc.owner,
            deprecated=doc.deprecated,
        )
        .on_conflict_do_nothing(index_elements=["org_id", "doc_ref", "doc_version"])
        .returning(documents.c.document_id)
    ).scalar()
    if inserted is not None:
        return inserted, True
    found = conn.execute(
        select(documents.c.document_id).where(
            documents.c.org_id == org_id,
            documents.c.doc_ref == doc.doc_id,
            documents.c.doc_version == doc.version,
        )
    ).scalar_one()
    return found, False


def store_build(conn: Connection, org_id: UUID, b: Build) -> UUID:
    """Writes the build's new document versions and their chunks, then the revision with its
    evaluation and its document list. Returns the revision's row ID."""
    idx = b.revision
    revision_id = uuid7()
    conn.execute(
        insert(index_revisions).values(
            index_revision_id=revision_id,
            org_id=org_id,
            revision_hash=idx.revision_id,
            built_at=idx.built_at,
            query_set_version=b.query_set_version,
            evaluation={"report": b.report, "misses": b.misses},
            activated=b.activate,
        )
    )
    for doc in sorted(b.documents, key=lambda d: d.doc_id):
        document_id, inserted = _document_id(conn, org_id, doc)
        mine = [c for c in idx.chunks if c.doc_id == doc.doc_id]
        if inserted and mine:
            conn.execute(
                insert(chunks),
                [
                    {
                        "chunk_id": uuid7(),
                        "document_id": document_id,
                        "ordinal": c.ordinal,
                        "heading_path": list(c.heading_path),
                        "body": c.text,
                        "span_start": c.start,
                        "span_end": c.end,
                        "content_sha256": c.sha256,
                    }
                    for c in mine
                ],
            )
        stale = any(c.stale for c in mine)
        conn.execute(
            insert(index_revision_documents).values(
                index_revision_id=revision_id, document_id=document_id, stale=stale
            )
        )
    return revision_id


def _active_row(conn: Connection, org_id: UUID) -> Any:
    r = index_revisions
    return conn.execute(
        select(r)
        .where(r.c.org_id == org_id, r.c.activated)
        .order_by(r.c.built_at.desc(), r.c.index_revision_id.desc())
        .limit(1)
    ).first()


def load_revision(conn: Connection, index_revision_id: UUID) -> IndexRevision:
    """Rebuilds a stored revision for search (IndexRevision.load)."""
    r = index_revisions
    row = conn.execute(select(r).where(r.c.index_revision_id == index_revision_id)).one()
    d, ird = documents, index_revision_documents
    docs = [
        dict(m)
        for m in conn.execute(
            select(
                d.c.document_id,
                d.c.doc_ref,
                d.c.doc_version,
                d.c.doc_type,
                d.c.services,
                d.c.allowed_groups,
                ird.c.stale,
            )
            .join(ird, ird.c.document_id == d.c.document_id)
            .where(ird.c.index_revision_id == index_revision_id)
        ).mappings()
    ]
    rows = [
        dict(m) | {"content_sha256": m["content_sha256"].strip()}
        for m in conn.execute(
            select(chunks).where(chunks.c.document_id.in_([x["document_id"] for x in docs]))
        ).mappings()
    ]
    return IndexRevision.load(row.revision_hash.strip(), row.built_at, docs, rows)


def active_revision(conn: Connection, org_id: UUID) -> IndexRevision | None:
    """The revision new investigations search, or None before the first activation (10.13
    step 6: the Knowledge Retriever then records a gap)."""
    row = _active_row(conn, org_id)
    return None if row is None else load_revision(conn, row.index_revision_id)


def ingest(
    conn: Connection,
    org_id: UUID,
    docs: Sequence[Document],
    queries: QuerySet | None,
    now: datetime,
) -> dict[str, Any]:
    """10.9's steps 4 to 7 in the caller's transaction: build, score against the active revision,
    store, and activate if the gate passes. Returns what was decided."""
    active = _active_row(conn, org_id)
    exact, active_docs = None, []
    if active is not None:
        exact = (active.evaluation.get("report") or {}).get("exact_identifier", {}).get("recall")
        active_docs = [
            ref
            for ref in conn.execute(
                select(documents.c.doc_ref)
                .join(
                    index_revision_documents,
                    index_revision_documents.c.document_id == documents.c.document_id,
                )
                .where(index_revision_documents.c.index_revision_id == active.index_revision_id)
            ).scalars()
        ]
    b = build(docs, queries, now=now, active_exact_recall=exact, active_doc_ids=active_docs)
    store_build(conn, org_id, b)
    return {
        "revision": b.revision.revision_id,
        "documents": len(b.documents),
        "chunks": len(b.revision.chunks),
        "query_set_version": b.query_set_version,
        "activated": b.activate,
        "misses": b.misses,
        "report": b.report,
    }


def purge_document_text(conn: Connection, document_id: UUID, reason: str) -> int:
    """T4.11 (WR-36, Q10.55): one document version's text out of PostgreSQL, as aisre_retention
    (migration 0012): the body and every chunk blanked, the hashes kept, the evidence citing
    that version marked "content removed". Returns how many evidence records were marked; a
    second call marks none and changes nothing. Retrieval stops finding it at once, since its
    chunks are empty; a new revision without it is the ingest pipeline's (10.9)."""
    marked = conn.execute(
        text("SELECT purge_document_text(:d, :r)"), {"d": document_id, "r": reason}
    ).scalar_one()
    return int(marked)
