"""Artifact 13.5 T0.8: the Incident Record against PostgreSQL, as aisre_app.

The key test fails the third write of a new incident and shows the first two are gone too:
incident, timeline event, alert and outbox row commit together or not at all.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import func, select, text

from aisre.contracts import IncidentCreate
from aisre.record import (
    START_WORKFLOW,
    UnknownService,
    create_incident,
    load_incident,
    timeline,
    uuid7,
)
from aisre.record import idempotency as idem
from aisre.record import incidents as record_incidents
from aisre.record.tables import incident_alerts, incidents, outbox, timeline_events


def alert(service: str, **overrides) -> IncidentCreate:
    values = {
        "service": service,
        "environment": "production",
        "started_at": datetime(2026, 10, 20, 14, 7, tzinfo=UTC),
        "title": "OrdersApiHighErrorRate: HTTP 500 rate above 5%",
        "source": "monitoring",
        "source_alert_id": "alrt-1",
        "fingerprint": f"{service}:OrdersApiHighErrorRate",
    } | overrides
    return IncidentCreate.model_validate(values)


def count(conn, table, incident_id: UUID) -> int:
    return conn.execute(
        select(func.count()).select_from(table).where(table.c.incident_id == incident_id)
    ).scalar_one()


def test_uuid7_has_version_and_variant():
    u = uuid7()
    assert u.version == 7 and u.variant == "specified in RFC 4122"


def test_create_writes_incident_event_alert_and_outbox(app_engine, catalogue):
    org, service, _ = catalogue
    with app_engine.begin() as conn:
        made = create_incident(conn, org_id=org, caller_id="dev:test", body=alert(service))
    assert not made.deduplicated and made.status.value == "open"
    assert made.incident_id.version == 7
    assert made.display_ref.startswith(f"INC-{datetime.now(UTC).year}-")
    with app_engine.connect() as conn:
        row = conn.execute(
            select(incidents).where(incidents.c.incident_id == made.incident_id)
        ).one()
        assert (row.status, row.last_event_seq, row.dedup_key) == (
            "open",
            1,
            f"{service}:OrdersApiHighErrorRate",
        )
        event = conn.execute(
            select(timeline_events).where(timeline_events.c.incident_id == made.incident_id)
        ).one()
        assert (event.seq, event.type, event.prev_hash) == (1, "aisre.incident.created.v1", None)
        assert len(event.hash) == 64
        box = conn.execute(select(outbox).where(outbox.c.incident_id == made.incident_id)).one()
        assert box.kind == START_WORKFLOW
        assert box.workflow_id == f"incident-{made.incident_id}"
        assert box.payload == {"incident_id": str(made.incident_id), "run": 1}
        assert box.delivered_at is None
        assert count(conn, incident_alerts, made.incident_id) == 1
        assert not conn.execute(
            text("SELECT * FROM verify_timeline_chain(:i)"), {"i": made.incident_id}
        ).all()


def test_a_duplicate_attaches_to_the_open_incident(app_engine, catalogue):
    org, service, _ = catalogue
    with app_engine.begin() as conn:
        first = create_incident(conn, org_id=org, caller_id="dev:test", body=alert(service))
    with app_engine.begin() as conn:
        again = create_incident(
            conn, org_id=org, caller_id="dev:test", body=alert(service, source_alert_id="alrt-2")
        )
    assert again.deduplicated and again.incident_id == first.incident_id
    assert again.display_ref == first.display_ref
    with app_engine.connect() as conn:
        assert count(conn, outbox, first.incident_id) == 1  # one workflow
        assert count(conn, incident_alerts, first.incident_id) == 2  # every alert kept
        events = timeline(conn, first.incident_id, org)
    assert [(e.sequence, e.type) for e in events] == [
        (1, "aisre.incident.created.v1"),
        (2, "aisre.incident.alert_attached.v1"),
    ]


def test_no_fingerprint_falls_back_to_service_and_title(app_engine, catalogue):
    org, service, _ = catalogue
    body = alert(service, fingerprint=None, source="engineer", source_alert_id=None)
    with app_engine.begin() as conn:
        a = create_incident(conn, org_id=org, caller_id="dev:test", body=body)
        b = create_incident(conn, org_id=org, caller_id="dev:test", body=body)
        key = conn.execute(
            select(incidents.c.dedup_key).where(incidents.c.incident_id == a.incident_id)
        ).scalar_one()
    assert b.incident_id == a.incident_id and b.deduplicated
    assert key == f"{service}:{body.title}"


def test_a_failure_after_the_second_write_leaves_nothing(app_engine, catalogue, monkeypatch):
    org, service, _ = catalogue
    seen: list[UUID] = []

    def fail_third_write(conn, incident_id, *args, **kwargs):
        seen.append(incident_id)  # incident and its first event are written by now
        assert count(conn, incidents, incident_id) == 1
        assert count(conn, timeline_events, incident_id) == 1
        raise RuntimeError("injected failure after the second write")

    monkeypatch.setattr(record_incidents, "_insert_alert", fail_third_write)
    with pytest.raises(RuntimeError, match="injected"):
        with app_engine.begin() as conn:
            create_incident(conn, org_id=org, caller_id="dev:test", body=alert(service))
    (incident_id,) = seen
    with app_engine.connect() as conn:
        for table in (incidents, timeline_events, incident_alerts, outbox):
            assert count(conn, table, incident_id) == 0, table.name
        assert not conn.execute(select(incidents).where(incidents.c.org_id == org)).first()


def test_unknown_service_writes_nothing(app_engine, catalogue):
    org, _, _ = catalogue
    with pytest.raises(UnknownService):
        with app_engine.begin() as conn:
            create_incident(conn, org_id=org, caller_id="dev:test", body=alert("no-such-service"))


def test_load_and_timeline_are_scoped_to_the_org(app_engine, catalogue):
    org, service, _ = catalogue
    with app_engine.begin() as conn:
        made = create_incident(conn, org_id=org, caller_id="dev:test", body=alert(service))
        create_incident(conn, org_id=org, caller_id="dev:test", body=alert(service))
    with app_engine.connect() as conn:
        view = load_incident(conn, made.incident_id, org)
        assert view is not None
        assert (view.service, view.environment, view.version, view.last_event_seq) == (
            service,
            "production",
            1,
            2,
        )
        assert view.open_action_ids == [] and view.current_diagnosis_id is None
        assert [e.sequence for e in timeline(conn, made.incident_id, org, after_seq=1)] == [2]
        assert load_incident(conn, made.incident_id, uuid7()) is None
        assert timeline(conn, made.incident_id, uuid7()) == []


def test_idempotency_key_replays_and_refuses_a_new_body(app_engine):
    caller = f"dev:{uuid7()}"
    op = "POST /v1/incidents"
    with app_engine.begin() as conn:
        assert idem.claim(conn, caller, op, "key-00000001", "a" * 64) is None
        idem.complete(conn, caller, op, "key-00000001", status=201, body={"x": 1}, resource_id=None)
    with app_engine.begin() as conn:
        stored = idem.claim(conn, caller, op, "key-00000001", "a" * 64)
        assert stored == idem.StoredResponse(201, {"x": 1})
        with pytest.raises(idem.KeyReused):
            idem.claim(conn, caller, op, "key-00000001", "b" * 64)


def test_a_failed_request_releases_its_key(app_engine):
    caller = f"dev:{uuid7()}"
    with pytest.raises(RuntimeError):
        with app_engine.begin() as conn:
            idem.claim(conn, caller, "op", "key-00000002", "a" * 64)
            raise RuntimeError("request failed")
    with app_engine.begin() as conn:
        assert idem.claim(conn, caller, "op", "key-00000002", "b" * 64) is None
