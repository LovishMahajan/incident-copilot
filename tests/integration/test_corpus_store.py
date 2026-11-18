"""Artifact 10.9, 10.13, 13.5 T1.17: the corpus in PostgreSQL, as aisre_app (WR-04).

Ingestion stores documents, chunks and an immutable revision with its evaluation; the active
revision reloads to the same search results; a revision that misses the gate is kept but not
used; the real `retrieve_knowledge` body stores passages as evidence. Migration 0005's columns
are checked here (schema-tests.sql runs against the baseline). ST-22.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from monitoring_scenarios import CUT_OFF, FIXTURES, serving, sources
from sqlalchemy import func, insert, select, text, update

from aisre.contracts import IncidentCreate
from aisre.record import corpus as store
from aisre.record import create_incident, uuid7
from aisre.record.evidence import list_evidence
from aisre.record.tables import environments, outbox, services
from aisre.retrieval.corpus import QuerySet, load_queries, read_folder
from aisre.workflow import activities as act
from aisre.workflow.record_activities import EvidenceActivities

FOLDER = Path(__file__).parents[1] / "fixtures/runbooks"
NOW = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)


def ingest(engine, org_id, docs=None, queries="default", now=NOW):
    q = load_queries(FOLDER / "queries-v1.json") if queries == "default" else queries
    with engine.begin() as conn:
        return store.ingest(conn, org_id, docs or read_folder(FOLDER), q, now)


def test_ingest_stores_an_active_revision_that_reloads_to_the_same_results(app_engine):
    org_id = uuid7()
    result = ingest(app_engine, org_id)
    assert result["activated"] and result["documents"] == 15 and result["misses"] == []
    print("\n10.13 on the synthetic set, stored:", json.dumps(result["report"]["all"]))
    with app_engine.connect() as conn:
        idx = store.active_revision(conn, org_id)
    from aisre.retrieval.retrieval import IndexRevision

    fresh = IndexRevision.build(read_folder(FOLDER), now=NOW)
    assert idx is not None and idx.revision_id == fresh.revision_id == result["revision"]
    for q, service, groups in [
        ("ORDER_TAX_RATE_BPS", "orders-api", {"orders-team"}),
        ("PAYMENTS_TIMEOUT_MS", "payments-api", {"payments-team"}),
        ("FR-ORD-9", "orders-api", {"security"}),
    ]:
        assert [(h.chunk_id, h.score) for h in idx.search(q, service=service, groups=groups)] == [
            (h.chunk_id, h.score) for h in fresh.search(q, service=service, groups=groups)
        ]


def test_a_rebuild_shares_document_versions_and_keeps_the_old_revision(app_engine):
    org_id = uuid7()
    first = ingest(app_engine, org_id)
    second = ingest(app_engine, org_id, now=NOW + timedelta(days=1))
    assert first["revision"] != second["revision"]
    with app_engine.connect() as conn:
        docs = conn.execute(
            select(func.count())
            .select_from(store.documents)
            .where(store.documents.c.org_id == org_id)
        ).scalar()
        revisions = (
            conn.execute(
                select(store.index_revisions.c.index_revision_id).where(
                    store.index_revisions.c.org_id == org_id
                )
            )
            .scalars()
            .all()
        )
        assert docs == 15 and len(revisions) == 2
        assert store.active_revision(conn, org_id).revision_id == second["revision"]
        old = [
            r for r in revisions if store.load_revision(conn, r).revision_id == first["revision"]
        ]
        assert len(old) == 1  # still loadable, for incidents that recorded it


def test_a_revision_that_misses_the_gate_is_stored_but_not_used(app_engine):
    org_id = uuid7()
    first = ingest(app_engine, org_id)
    queries = load_queries(FOLDER / "queries-v1.json")
    worse = QuerySet("v1", [q for q in queries.queries if q.kind == "paraphrase"][:1])
    later = ingest(app_engine, org_id, queries=worse, now=NOW + timedelta(days=1))
    # one paraphrase query: its exact-identifier recall is missing, so it can't keep up
    assert not later["activated"] and "exact_identifier_recall" in later["misses"]
    with app_engine.connect() as conn:
        assert store.active_revision(conn, org_id).revision_id == first["revision"]


def test_without_a_query_set_nothing_activates(app_engine):
    org_id = uuid7()
    result = ingest(app_engine, org_id, queries=None)
    assert not result["activated"]
    with app_engine.connect() as conn:
        assert store.active_revision(conn, org_id) is None


def test_0005_a_revision_row_is_written_once_and_activation_needs_a_score(app_engine):
    org_id = uuid7()
    ingest(app_engine, org_id)
    with pytest.raises(Exception, match="permission denied|append-only"):
        with app_engine.begin() as conn:
            conn.execute(
                text("UPDATE index_revisions SET activated = false WHERE org_id = :o"),
                {"o": org_id},
            )
    with pytest.raises(Exception, match="index_revisions_scored"):
        with app_engine.begin() as conn:
            conn.execute(
                insert(store.index_revisions).values(
                    index_revision_id=uuid7(),
                    org_id=org_id,
                    revision_hash="a" * 64,
                    built_at=NOW,
                    evaluation={},
                    activated=True,
                )
            )


# ---------------------------------------------------------------- retrieve_knowledge
@pytest.fixture
def orders_incident(app_engine):
    """orders-api owned by orders-team, the corpus ingested for the organisation, and the
    reference alert as an incident."""
    org_id = uuid7()
    with app_engine.begin() as conn:
        conn.execute(
            insert(services).values(
                service_id=uuid7(),
                org_id=org_id,
                slug="orders-api",
                display_name="orders-api",
                owner_group="orders-team",
                critical=False,
                hold_seconds=600,
            )
        )
        conn.execute(
            insert(environments).values(environment_id=uuid7(), org_id=org_id, slug="production")
        )
        alert = json.loads((FIXTURES / "reference-incident/alert.json").read_text())
        intake = create_incident(
            conn,
            org_id=org_id,
            caller_id="monitoring",
            body=IncidentCreate.model_validate(alert["incident_create"]),
            grouping_window=timedelta(0),
        )
        # The Compose worker collects evidence too (compose.yaml): its relay never starts this
        # incident, so the rows here are this test's alone.
        conn.execute(
            update(outbox)
            .where(outbox.c.incident_id == intake.incident_id)
            .values(delivered_at=func.now())
        )
    return org_id, intake.incident_id


def run(engine, url, inp):
    acts = EvidenceActivities(engine, sources(url, replay_as_of=CUT_OFF.isoformat()))
    fn = acts.collect_evidence if isinstance(inp, act.EvidenceIn) else acts.retrieve_knowledge
    return asyncio.run(fn(inp))


def test_retrieve_knowledge_stores_pinned_passages_for_the_baseline_terms(
    app_engine, orders_incident
):
    org_id, incident_id = orders_incident
    revision = ingest(app_engine, org_id)["revision"]
    with serving() as url:
        base = run(app_engine, url, act.EvidenceIn(str(incident_id), "base")).output
        inp = act.SkillIn(str(incident_id), "knowledge", evidence_ids=base.evidence_ids)
        result = run(app_engine, url, inp)
        again = run(app_engine, url, inp)
    out = result.output
    assert out.index_revision == revision and out.gap_count == 0 and out.evidence_ids
    assert sorted(again.output.evidence_ids) == sorted(out.evidence_ids)
    with app_engine.connect() as conn:
        rows = list_evidence(conn, incident_id, org_id, ids=out.evidence_ids)
        skills = conn.execute(
            text("SELECT skill, count(*) FROM tool_calls WHERE incident_id = :i GROUP BY skill"),
            {"i": incident_id},
        ).all()
    docs = {r.attributes["doc_id"] for r in rows}
    assert {"rb-orders-api", "pi-2026-031"} <= docs
    assert not docs & {"rb-orders-fraud", "rb-orders-api-legacy", "rb-payments-api"}  # ST-22
    assert {r.kind.value for r in rows} == {"runbook", "past_incident"}
    assert all(r.attributes["index_revision"] == revision and r.strength_tier is None for r in rows)
    assert dict(skills) == {"collect_evidence": 8, "retrieve_knowledge": 2}
    assert result.usage.tool_calls == 2


def test_no_active_revision_is_a_gap_and_the_investigation_goes_on(app_engine, orders_incident):
    _, incident_id = orders_incident
    with serving() as url:
        result = run(app_engine, url, act.SkillIn(str(incident_id), "knowledge", evidence_ids=[]))
    out = result.output
    assert result.failure is None and out.gap_count == 1 and out.index_revision == "none"
    assert len(out.evidence_ids) == 1
