"""Artifact 13.5 T0.9: the M0 routes against PostgreSQL, as aisre_app."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from aisre.api.app import create_app
from aisre.api.auth import DEV_CALLER_HEADER, DEV_ORG_HEADER
from aisre.record import uuid7
from aisre.record.tables import incidents, outbox

ALERT = json.loads(
    (Path(__file__).parents[1] / "fixtures/reference-incident/alert.json").read_text()
)


@pytest.fixture
def api(app_engine, settings):
    with TestClient(create_app(settings, app_engine)) as c:
        yield c


def headers(org, key: str | None = None, caller: str = "alice") -> dict[str, str]:
    """Keys are per caller, and the database outlives a run: suffix each with the org."""
    h = {DEV_CALLER_HEADER: caller, DEV_ORG_HEADER: str(org)}
    return h | ({"Idempotency-Key": f"{key}-{org.hex[-12:]}"} if key else {})


def body_for(service: str, **changes) -> dict:
    return ALERT["incident_create"] | {"service": service} | changes


def test_the_reference_alert_opens_an_incident(api, add_service):
    org = uuid7()
    add_service(org, "orders-api")
    r = api.post("/v1/incidents", json=ALERT, headers=headers(org, "ref-alert-0001"))
    assert r.status_code == 201, r.text
    made = r.json()
    assert made["status"] == "open" and made["deduplicated"] is False
    assert r.headers["Location"] == made["links"]["self"]

    view = api.get(made["links"]["self"], headers=headers(org))
    assert view.status_code == 200 and view.headers["ETag"] == '"1"'
    assert view.json()["service"] == "orders-api" and view.json()["last_event_seq"] == 1

    page = api.get(made["links"]["timeline"], headers=headers(org)).json()
    assert [e["type"] for e in page["items"]] == ["aisre.incident.created.v1"]
    assert page["items"][0]["subject"] == f"incidents/{made['incident_id']}"
    assert page["next_after_seq"] is None


def test_keys_replay_conflict_and_deduplicate(api, app_engine, catalogue):
    org, service, _ = catalogue
    first = api.post("/v1/incidents", json=body_for(service), headers=headers(org, "key-a-00001"))
    assert first.status_code == 201
    replay = api.post("/v1/incidents", json=body_for(service), headers=headers(org, "key-a-00001"))
    assert (replay.status_code, replay.json()) == (201, first.json())

    reused = api.post(
        "/v1/incidents", json=body_for(service, title="Other"), headers=headers(org, "key-a-00001")
    )
    assert reused.status_code == 409 and reused.json()["code"] == "idempotency_key_reused"

    dup = api.post("/v1/incidents", json=body_for(service), headers=headers(org, "key-b-00001"))
    assert dup.status_code == 200
    assert dup.json()["deduplicated"] is True
    assert dup.json()["incident_id"] == first.json()["incident_id"]

    # A key is per caller: bob's same key is a new request, and also deduplicates.
    bob = api.post(
        "/v1/incidents", json=body_for(service), headers=headers(org, "key-a-00001", "bob")
    )
    assert bob.status_code == 200

    with app_engine.connect() as conn:
        assert (
            conn.execute(
                select(func.count()).select_from(incidents).where(incidents.c.org_id == org)
            ).scalar_one()
            == 1
        )
        assert (
            conn.execute(
                select(func.count())
                .select_from(outbox)
                .where(outbox.c.incident_id == first.json()["incident_id"])
            ).scalar_one()
            == 1
        )

    page = api.get(
        f"/v1/incidents/{first.json()['incident_id']}/timeline?after_seq=1&limit=1",
        headers=headers(org),
    ).json()
    assert [e["sequence"] for e in page["items"]] == [2] and page["next_after_seq"] == 2


def test_unknown_service_is_404_and_leaves_the_key_unused(api, catalogue):
    org, service, _ = catalogue
    r = api.post("/v1/incidents", json=body_for("no-such-svc"), headers=headers(org, "key-c-00001"))
    assert r.status_code == 404 and r.json()["code"] == "unknown_service"
    ok = api.post("/v1/incidents", json=body_for(service), headers=headers(org, "key-c-00001"))
    assert ok.status_code == 201


def test_another_org_sees_404(api, catalogue):
    org, service, _ = catalogue
    made = api.post("/v1/incidents", json=body_for(service), headers=headers(org, "key-d-00001"))
    other = headers(uuid7())
    assert api.get(made.json()["links"]["self"], headers=other).status_code == 404
    assert api.get(made.json()["links"]["timeline"], headers=other).status_code == 404


def test_readyz(api):
    assert api.get("/readyz").json() == {"status": "ready"}
