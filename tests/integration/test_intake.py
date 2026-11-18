"""Artifact 13.5 T1.2: full intake against PostgreSQL, as aisre_app.

Q13.31's repeats (same key and body; same key, different body; new key while open; after
close; no fingerprint; two at once), FR-01, FR-03 to FR-06, FR-41 to FR-43 and ST-21. Each
asserts one incident and one outbox row where one is due. "Relay twice" is in test_relay.py:
a repeated start row is success, and Temporal holds one workflow per ID.

The Compose worker's relay starts workflows for the incidents opened here, so their status
moves on; none of that touches what intake decides, which only `closed` and `cancelled` end.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from aisre.api.app import create_app
from aisre.api.auth import DEV_CALLER_HEADER, DEV_GROUPS_HEADER, DEV_ORG_HEADER
from aisre.contracts import IncidentCreate
from aisre.record import create_incident
from aisre.record.dead_letters import dead_letters
from aisre.record.tables import incident_alerts, incidents, outbox, timeline_events

ALERT = json.loads(
    (Path(__file__).parents[1] / "fixtures/reference-incident/alert.json").read_text()
)
MANDATORY = ("service", "environment", "started_at", "title", "source")  # FR-41


@pytest.fixture
def api(app_engine, settings):
    with TestClient(create_app(settings, app_engine)) as c:
        yield c


def who(org, groups="aisre:monitoring", caller="monitoring", key: str | None = None) -> dict:
    h = {DEV_CALLER_HEADER: caller, DEV_ORG_HEADER: str(org), DEV_GROUPS_HEADER: groups}
    return h | ({"Idempotency-Key": f"{key}-{org.hex[-12:]}"} if key else {})


def body_for(service: str, **changes) -> dict:
    return ALERT["incident_create"] | {"service": service} | changes


def count(engine, table, *where) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table).where(*where)).scalar_one()


def add_environment(engine, org, slug: str) -> None:
    from sqlalchemy import insert

    from aisre.record import uuid7
    from aisre.record.tables import environments

    with engine.begin() as conn:
        conn.execute(insert(environments).values(environment_id=uuid7(), org_id=org, slug=slug))


def org_incidents(engine, org) -> int:
    return count(engine, incidents, incidents.c.org_id == org)


def outbox_rows(engine, incident_id) -> int:
    return count(engine, outbox, outbox.c.incident_id == incident_id)


def letters(engine, org) -> list:
    with engine.connect() as conn:
        return conn.execute(
            select(dead_letters)
            .where(dead_letters.c.org_id == org)
            .order_by(dead_letters.c.dead_letter_id)
        ).all()


# ---------------------------------------------------------------- FR-01, FR-04, Q13.31
def test_fr01_a_valid_alert_gets_an_id_and_one_workflow_start(api, app_engine, catalogue):
    org, service, _ = catalogue
    r = api.post("/v1/incidents", json=body_for(service), headers=who(org, key="fr01-0001"))
    assert r.status_code == 201 and r.json()["incident_id"]
    assert outbox_rows(app_engine, r.json()["incident_id"]) == 1


def test_q1331_same_key_same_body_replays_and_different_body_is_409(api, app_engine, catalogue):
    org, service, _ = catalogue
    first = api.post("/v1/incidents", json=body_for(service), headers=who(org, key="rep-0001"))
    again = api.post("/v1/incidents", json=body_for(service), headers=who(org, key="rep-0001"))
    assert first.status_code == again.status_code == 201 and again.json() == first.json()
    other = api.post(
        "/v1/incidents",
        json=body_for(service, title="Something else"),
        headers=who(org, key="rep-0001"),
    )
    assert other.status_code == 409 and other.json()["code"] == "idempotency_key_reused"
    assert org_incidents(app_engine, org) == 1
    assert outbox_rows(app_engine, first.json()["incident_id"]) == 1


def test_fr03_the_same_alert_ten_times_is_one_investigation_and_ten_entries(
    api, app_engine, catalogue
):
    org, service, _ = catalogue
    answers = [
        api.post("/v1/incidents", json=body_for(service), headers=who(org, key=f"ten-{i:04d}"))
        for i in range(10)
    ]
    assert [a.status_code for a in answers] == [201] + [200] * 9
    assert all(a.json()["deduplicated"] for a in answers[1:])
    iid = answers[0].json()["incident_id"]
    assert {a.json()["incident_id"] for a in answers} == {iid}
    assert org_incidents(app_engine, org) == 1 and outbox_rows(app_engine, iid) == 1
    assert count(app_engine, incident_alerts, incident_alerts.c.incident_id == iid) == 10
    with app_engine.connect() as conn:
        types = conn.execute(
            select(timeline_events.c.type).where(
                timeline_events.c.incident_id == iid,
                timeline_events.c.type.in_(
                    ["aisre.incident.created.v1", "aisre.incident.alert_attached.v1"]
                ),
            )
        ).scalars()
        assert sorted(types) == ["aisre.incident.alert_attached.v1"] * 9 + [
            "aisre.incident.created.v1"
        ]


def test_fr42_an_alert_with_no_fingerprint_deduplicates_on_service_and_name(
    api, app_engine, catalogue
):
    org, service, _ = catalogue
    bare = body_for(service, fingerprint=None, source_alert_id=None)
    a = api.post("/v1/incidents", json=bare, headers=who(org, key="nofp-0001"))
    b = api.post("/v1/incidents", json=bare, headers=who(org, key="nofp-0002"))
    assert (a.status_code, b.status_code) == (201, 200)
    assert a.json()["incident_id"] == b.json()["incident_id"]
    with app_engine.connect() as conn:
        key = conn.execute(
            select(incidents.c.dedup_key).where(incidents.c.org_id == org)
        ).scalar_one()
    assert key == f"{service}:{bare['title']}"


def test_q1331_after_close_the_same_alert_opens_a_new_incident(api, app_engine, catalogue):
    org, service, _ = catalogue
    body = IncidentCreate.model_validate(body_for(service))
    with app_engine.begin() as conn:  # an incident no workflow will touch, then cancelled
        first = create_incident(conn, org_id=org, caller_id="dev:test", body=body)
        conn.execute(
            update(outbox)
            .where(outbox.c.incident_id == first.incident_id)
            .values(delivered_at=func.now())
        )
    with app_engine.begin() as conn:
        conn.execute(
            update(incidents)
            .where(incidents.c.incident_id == first.incident_id)
            .values(status="cancelled", version=incidents.c.version + 1)
        )
    r = api.post("/v1/incidents", json=body_for(service), headers=who(org, key="close-0001"))
    assert r.status_code == 201 and r.json()["incident_id"] != str(first.incident_id)
    assert org_incidents(app_engine, org) == 2


def test_q1331_two_at_once_give_one_incident(app_engine, catalogue):
    org, service, _ = catalogue
    body = IncidentCreate.model_validate(body_for(service))
    results, start = [], threading.Barrier(2)

    def submit():
        start.wait()
        with app_engine.begin() as conn:
            results.append(create_incident(conn, org_id=org, caller_id="dev:t", body=body))

    threads = [threading.Thread(target=submit) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r.deduplicated for r in results) == [False, True]
    assert len({r.incident_id for r in results}) == 1
    assert org_incidents(app_engine, org) == 1


# ---------------------------------------------------------------- FR-43 grouping
def test_fr43_another_alert_for_the_service_within_10_minutes_joins_the_incident(
    api, app_engine, catalogue
):
    org, service, _ = catalogue
    first = api.post("/v1/incidents", json=body_for(service), headers=who(org, key="grp-0001"))
    other = body_for(service, title="Latency high", fingerprint=f"{service}:LatencyHigh")
    joined = api.post("/v1/incidents", json=other, headers=who(org, key="grp-0002"))
    assert joined.status_code == 200 and joined.json()["deduplicated"] is True
    assert joined.json()["incident_id"] == first.json()["incident_id"]
    # Another environment of the same service is another incident.
    add_environment(app_engine, org, "staging")
    staging = api.post(
        "/v1/incidents",
        json=body_for(service, environment="staging", fingerprint=f"{service}:staging"),
        headers=who(org, key="grp-0003"),
    )
    assert staging.status_code == 201
    assert org_incidents(app_engine, org) == 2


def test_fr43_after_the_window_a_new_alert_opens_a_new_incident(app_engine, catalogue):
    org, service, _ = catalogue
    window = timedelta(seconds=1)
    a = IncidentCreate.model_validate(body_for(service))
    b = IncidentCreate.model_validate(body_for(service, fingerprint="later", title="Later"))
    with app_engine.begin() as conn:
        first = create_incident(conn, org_id=org, caller_id="dev:t", body=a, grouping_window=window)
    time.sleep(1.2)
    with app_engine.begin() as conn:
        later = create_incident(conn, org_id=org, caller_id="dev:t", body=b, grouping_window=window)
    assert not later.deduplicated and later.incident_id != first.incident_id


# ---------------------------------------------------------------- FR-05, FR-41 dead letters
@pytest.mark.parametrize("field", MANDATORY)
def test_fr41_a_missing_mandatory_field_is_named_and_dead_lettered(
    api, app_engine, catalogue, caplog, field
):
    org, service, _ = catalogue
    body = body_for(service)
    del body[field]
    with caplog.at_level("INFO", logger="aisre.audit"):
        r = api.post("/v1/incidents", json=body, headers=who(org, key="mand-0001"))
    assert r.status_code == 422 and r.json()["code"] == "validation_failed"
    assert [e["loc"] for e in r.json()["errors"]] == [[field]]
    assert org_incidents(app_engine, org) == 0
    (letter,) = letters(app_engine, org)
    assert letter.reason == "validation_failed" and field in letter.detail
    assert letter.raw_payload == body and letter.caller_id == "dev:monitoring"
    assert letter.retain_until > letter.received_at
    logged = [json.loads(m) for m in caplog.messages]
    assert [(e["event"], e["dead_letter_id"], e["notify"]) for e in logged] == [
        ("aisre.intake.dead_lettered", str(letter.dead_letter_id), "incident_lead")
    ]


@pytest.mark.parametrize("raw", [b"not json at all", b"[1, 2]", b'"text"'])
def test_fr05_a_body_that_is_not_an_object_is_dead_lettered_as_text(
    api, app_engine, catalogue, raw
):
    org, _, _ = catalogue
    headers = who(org, key="raw-0001") | {"Content-Type": "application/json"}
    r = api.post("/v1/incidents", content=raw, headers=headers)
    assert r.status_code == 422 and r.json()["errors"][0]["loc"] == ["body"]
    (letter,) = letters(app_engine, org)
    assert letter.raw_payload == {"unparsed": raw.decode()}


def test_a_rejected_alert_leaves_its_key_unused(api, app_engine, catalogue):
    org, service, _ = catalogue
    bad = api.post("/v1/incidents", json=body_for("no-such-svc"), headers=who(org, key="unused-01"))
    assert bad.status_code == 422
    ok = api.post("/v1/incidents", json=body_for(service), headers=who(org, key="unused-01"))
    assert ok.status_code == 201


# ---------------------------------------------------------------- FR-06, ST-21
def test_st21_existence_probe(api, app_engine, catalogue, add_service, caplog):
    """A person gets the same 404 for an unknown and a forbidden service; Monitoring gets 422.
    All three are dead-lettered, each with a line for the incident lead."""
    org, service, _ = catalogue
    add_service(org, "payments", "staging")  # a service this person may not create for
    person = "aisre:responder:" + service  # may create for `service` only
    with caplog.at_level("INFO", logger="aisre.audit"):
        unknown = api.post(
            "/v1/incidents",
            json=body_for("ghost-svc"),
            headers=who(org, "aisre:responder", "alice", "st21-0001"),
        )
        forbidden = api.post(
            "/v1/incidents", json=body_for("payments"), headers=who(org, person, "alice", "st21-2")
        )
        monitoring = api.post(
            "/v1/incidents", json=body_for("ghost-svc"), headers=who(org, key="st21-0003")
        )
    assert unknown.status_code == forbidden.status_code == 404
    strip = ("detail", "request_id")
    same = [{k: v for k, v in r.json().items() if k not in strip} for r in (unknown, forbidden)]
    assert same[0] == same[1] and same[0]["code"] == "unknown_service"
    assert monitoring.status_code == 422 and monitoring.json()["code"] == "unknown_service"

    assert org_incidents(app_engine, org) == 0
    got = [(x.reason, x.caller_id, x.service_slug) for x in letters(app_engine, org)]
    assert got == [
        ("unknown_service", "dev:alice", "ghost-svc"),
        ("forbidden_service", "dev:alice", "payments"),
        ("unknown_service", "dev:monitoring", "ghost-svc"),
    ]
    notices = [json.loads(m) for m in caplog.messages]
    notices = [e for e in notices if e["event"] == "aisre.intake.dead_lettered"]
    assert [e["reason"] for e in notices] == [g[0] for g in got]
    assert {e["notify"] for e in notices} == {"incident_lead"}


def test_st21_dead_letters_are_append_only_for_the_app(app_engine, api, catalogue):
    from sqlalchemy.exc import DBAPIError

    org, _, _ = catalogue
    api.post("/v1/incidents", json=body_for("ghost-svc"), headers=who(org, key="app-0001"))
    with pytest.raises(DBAPIError), app_engine.begin() as conn:
        conn.execute(
            update(dead_letters).where(dead_letters.c.org_id == org).values(reason="tampered")
        )
