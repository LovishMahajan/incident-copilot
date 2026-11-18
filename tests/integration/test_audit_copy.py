"""Artifact 13.5 T1.20 (FR-37, ST-31, ST-32): the write-once audit copy against PostgreSQL.

The copier fills the copy and the check finds nothing on an untouched record; the app role and
the owner can't change the timeline (ST-31); a privileged edit that keeps the chain whole is
still found by the check (ST-32); and the audit export reads back from the copy. The store is
the in-memory one behind the same interface as the object store.

These assume the Compose worker copies nothing (no AISRE_AUDIT_STORE_* there), so the copy
cursors are this test's.
"""

from __future__ import annotations

import json
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from aisre.api.app import create_app
from aisre.api.auth import DEV_CALLER_HEADER, DEV_GROUPS_HEADER, DEV_ORG_HEADER
from aisre.api.export import verify
from aisre.record.audit_copy import MemoryStore, check_chain, copy_timelines, event_key

ALERT = json.loads(
    (Path(__file__).parents[1] / "fixtures/reference-incident/alert.json").read_text()
)


@pytest.fixture(scope="module")
def owner_engine(postgres_url, settings):
    """The owning role (AISRE_MIGRATION_DATABASE_URL), which can switch a trigger off."""
    url = settings.migration_database_url.get_secret_value()
    try:
        psycopg.connect(url.replace("+psycopg", ""), connect_timeout=3).close()
    except psycopg.Error as e:
        pytest.skip(f"owner role unreachable with AISRE_MIGRATION_DATABASE_URL: {type(e).__name__}")
    engine = create_engine(url)
    yield engine
    engine.dispose()


@pytest.fixture
def store():
    return MemoryStore()


@pytest.fixture
def api(app_engine, settings, store):
    with TestClient(create_app(settings, app_engine, audit_store=store)) as c:
        yield c


def as_(org, groups: str, caller: str = "lena") -> dict[str, str]:
    return {DEV_CALLER_HEADER: caller, DEV_ORG_HEADER: str(org), DEV_GROUPS_HEADER: groups}


@pytest.fixture
def incident(api, catalogue, quiet):
    org, service, _ = catalogue
    # Its timeline: created, then the note below; nothing from the Compose worker.
    incident_id = quiet(org, service)
    note = api.post(
        f"/v1/incidents/{incident_id}/notes",
        json={"note": "looking"},
        headers=as_(org, "aisre:responder") | {"Idempotency-Key": f"acn-{org.hex}"},
    )
    assert note.status_code == 201
    return org, incident_id


def mine(faults, incident_id):
    return [f for f in faults if f.incident_id == incident_id]


def test_the_copy_holds_every_event_and_the_check_finds_nothing(app_engine, store, incident):
    _, incident_id = incident
    assert copy_timelines(app_engine, store) >= 2
    assert store.keys(f"timeline/{incident_id}/") == [
        event_key(incident_id, 1),
        event_key(incident_id, 2),
    ]
    copy_timelines(app_engine, store)  # other incidents may have moved on; this one hasn't
    assert len(store.keys(f"timeline/{incident_id}/")) == 2
    copied = json.loads(store.get(event_key(incident_id, 2)))
    assert copied["type"] == "aisre.incident.note_added.v1" and copied["data"] == {
        "note": "looking"
    }
    with app_engine.connect() as conn:
        assert mine(check_chain(conn, store), incident_id) == []


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE timeline_events SET actor = 'user:mallory' WHERE incident_id = :i",
        "DELETE FROM timeline_events WHERE incident_id = :i",
        "TRUNCATE timeline_events",
    ],
)
def test_st31_the_timeline_refuses_change_from_the_app_and_the_owner(
    app_engine, owner_engine, incident, statement
):
    _, incident_id = incident
    for engine in (app_engine, owner_engine):
        with pytest.raises(DBAPIError) as e:
            with engine.begin() as conn:
                conn.execute(text(statement), {"i": incident_id})
        assert e.value.orig.sqlstate in {"23001", "42501"}  # restrict_violation, no privilege


def test_st32_a_privileged_edit_is_found_by_the_check(app_engine, owner_engine, store, incident):
    """The owner switches the append-only trigger off and rewrites the last event and its hash,
    so the chain itself still verifies. The copy can't be rewritten, so the check finds it."""
    _, incident_id = incident
    copy_timelines(app_engine, store)
    with owner_engine.connect() as conn:
        tx = conn.begin()
        try:
            conn.execute(text("ALTER TABLE timeline_events DISABLE TRIGGER t10_append_only"))
            conn.execute(
                text(
                    'UPDATE timeline_events SET data = \'{"note": "nothing to see"}\' '
                    "WHERE incident_id = :i AND seq = 2"
                ),
                {"i": incident_id},
            )
            conn.execute(
                text(
                    "UPDATE timeline_events e SET hash = timeline_event_hash(e) "
                    "WHERE incident_id = :i AND seq = 2"
                ),
                {"i": incident_id},
            )
            chain = conn.execute(
                text("SELECT * FROM verify_timeline_chain(:i)"), {"i": incident_id}
            ).all()
            assert chain == []  # the chain alone can't tell
            (fault,) = mine(check_chain(conn, store), incident_id)
            assert (fault.at_seq, fault.problem) == (2, "differs from the write-once copy")
        finally:
            tx.rollback()


def test_the_audit_export_reads_back_from_the_copy(api, app_engine, store, incident):
    org, incident_id = incident
    lead = as_(org, "aisre:incident_lead")
    assert api.get(f"/v1/incidents/{incident_id}", headers=lead).status_code == 200
    assert api.get(f"/v1/incidents/{incident_id}").status_code == 401  # a failed sign-in
    path = f"/v1/incidents/{incident_id}/audit/export"
    assert api.get(path, headers=as_(org, "aisre:responder")).status_code == 403
    copy_timelines(app_engine, store)

    r = api.get(path, headers=lead)
    assert r.status_code == 200, r.text
    doc = r.json()
    assert doc["source"] == "write-once audit copy" and doc["copy_complete"] is True
    assert [e["seq"] for e in doc["timeline"]] == [1, 2]
    reads = [line for line in doc["access_log"] if line["event"] == "aisre.content.read"]
    assert {line["content"] for line in reads} >= {"incident", "audit_export"}
    assert any(line["reason"] == "missing_dev_caller" for line in doc["failed_sign_ins"])
    assert verify(doc, doc["signature"]["public_key"])


def test_without_a_store_the_audit_export_is_unavailable(app_engine, settings, catalogue):
    org, service, _ = catalogue
    with TestClient(create_app(settings, app_engine)) as c:
        made = c.post(
            "/v1/incidents",
            json=ALERT["incident_create"] | {"service": service},
            headers=as_(org, "aisre:monitoring", "monitoring")
            | {"Idempotency-Key": f"nx-{org.hex}"},
        )
        r = c.get(
            f"/v1/incidents/{made.json()['incident_id']}/audit/export",
            headers=as_(org, "aisre:incident_lead"),
        )
    assert r.status_code == 503 and r.json()["code"] == "audit_copy_unavailable"
