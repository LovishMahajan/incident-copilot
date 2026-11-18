"""Artifact 13.5 T1.4, ST-20, ST-22: the ADR-50 matrix at the routes, per service, against
PostgreSQL. Callers sign in with the development header and its groups (aisre.api.auth);
tokens carry the same groups (tests/integration/test_api_auth.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aisre.api.app import create_app
from aisre.api.auth import DEV_CALLER_HEADER, DEV_GROUPS_HEADER, DEV_ORG_HEADER

ALERT = json.loads(
    (Path(__file__).parents[1] / "fixtures/reference-incident/alert.json").read_text()
)


@pytest.fixture
def api(app_engine, settings):
    with TestClient(create_app(settings, app_engine)) as c:
        yield c


def as_(org, groups: str, caller: str = "alice", key: str | None = None) -> dict[str, str]:
    h = {DEV_CALLER_HEADER: caller, DEV_ORG_HEADER: str(org), DEV_GROUPS_HEADER: groups}
    return h | ({"Idempotency-Key": f"{key}-{org.hex[-12:]}"} if key else {})


@pytest.fixture
def incident(api, catalogue):
    """An incident on the catalogue's service; returns (org, service, links)."""
    org, service, _ = catalogue
    r = api.post(
        "/v1/incidents",
        json=ALERT["incident_create"] | {"service": service},
        headers=as_(org, "aisre:monitoring", "monitoring", "authz-0001"),
    )
    assert r.status_code == 201, r.text
    return org, service, r.json()["links"]


def events(caplog) -> list[dict]:
    return [json.loads(m) for m in caplog.messages if m.startswith("{")]


@pytest.mark.parametrize(
    "groups", ["aisre:viewer", "aisre:responder", "aisre:platform_admin", "aisre:viewer:{svc}"]
)
def test_readers_read_and_every_read_is_logged(api, incident, caplog, groups):
    org, service, links = incident
    with caplog.at_level("INFO", logger="aisre.audit"):
        assert (
            api.get(links["self"], headers=as_(org, groups.format(svc=service))).status_code == 200
        )
        r = api.get(links["timeline"], headers=as_(org, groups.format(svc=service)))
    assert r.status_code == 200
    reads = [e for e in events(caplog) if e["event"] == "aisre.content.read"]
    assert [e["content"] for e in reads] == ["incident", "timeline"]
    assert {e["incident_id"] for e in reads} == {links["self"].rsplit("/", 1)[1]}
    assert all(e["caller"] == "dev:alice" for e in reads)


def test_st22_a_role_on_another_service_reads_nothing_and_is_logged(api, incident, caplog):
    org, service, links = incident
    other = as_(org, "aisre:incident_lead:payments")
    with caplog.at_level("INFO", logger="aisre.audit"):
        for path in (links["self"], links["timeline"]):
            r = api.get(path, headers=other)
            assert r.status_code == 404 and r.json()["code"] == "not_found"
    logged = events(caplog)
    assert [e["event"] for e in logged] == ["aisre.authz.denied"] * 2
    assert {(e["permission"], e["service"]) for e in logged} == {("incident:read", service)}
    assert not [e for e in logged if e["event"] == "aisre.content.read"]


def test_st20_monitoring_creates_but_cannot_read(api, incident, caplog):
    org, _, links = incident
    with caplog.at_level("INFO", logger="aisre.audit"):
        r = api.get(links["timeline"], headers=as_(org, "aisre:monitoring", "monitoring"))
    assert r.status_code == 403
    assert events(caplog)[0]["event"] == "aisre.authz.denied"


def test_st22_creating_for_a_service_outside_your_roles_is_the_unknown_service_404(
    api, catalogue, caplog
):
    org, service, _ = catalogue
    body = ALERT["incident_create"] | {"service": service}
    with caplog.at_level("INFO", logger="aisre.audit"):
        forbidden = api.post(
            "/v1/incidents", json=body, headers=as_(org, "aisre:responder:payments", key="f-001")
        )
        unknown = api.post(
            "/v1/incidents",
            json=body | {"service": "no-such-svc"},
            headers=as_(org, "aisre:responder", key="f-002"),
        )
    assert forbidden.status_code == unknown.status_code == 404
    assert forbidden.json()["code"] == unknown.json()["code"] == "unknown_service"
    denied = [e for e in events(caplog) if e["event"] == "aisre.authz.denied"]
    assert [(e["permission"], e["service"]) for e in denied] == [("incident:create", service)]
