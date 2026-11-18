"""Artifact 13.5 T1.3, ST-19: bearer-token sign-in against PostgreSQL, as aisre_app.

The API is built with the fake issuer's keys (ops/fakes/token_issuer.py, in-process); the
Compose stack runs the same issuer, and README.md shows the same calls with curl.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from aisre.api.app import create_app
from aisre.record import uuid7
from aisre.record.dead_letters import dead_letters
from aisre.record.tables import idempotency_keys, incidents

ALERT = json.loads(
    (Path(__file__).parents[1] / "fixtures/reference-incident/alert.json").read_text()
)


@pytest.fixture
def token_api(app_engine, settings, token_issuer):
    issuer, jwks_url = token_issuer
    oidc = settings.model_copy(
        update={
            "oidc_issuer": issuer.url,
            "oidc_audience": issuer.audience,
            "oidc_jwks_url": jwks_url,
        }
    )
    with TestClient(create_app(oidc, app_engine)) as c:
        yield c


def bearer(issuer, org, sub="alice", groups=("aisre:responder",), **kw) -> dict[str, str]:
    return {"Authorization": f"Bearer {issuer.token(sub, list(groups), org_id=str(org), **kw)}"}


def rows(engine, table, org) -> int:
    with engine.connect() as conn:
        return conn.execute(
            select(func.count()).select_from(table).where(table.c.org_id == org)
        ).scalar_one()


def test_a_signed_in_caller_opens_and_reads_an_incident(token_api, token_issuer, catalogue):
    issuer = token_issuer[0]
    org, service, _ = catalogue
    body = ALERT["incident_create"] | {"service": service}
    key = {"Idempotency-Key": f"tok-{org.hex[-12:]}"}
    r = token_api.post("/v1/incidents", json=body, headers=bearer(issuer, org) | key)
    assert r.status_code == 201, r.text
    view = token_api.get(r.json()["links"]["self"], headers=bearer(issuer, org, "bob"))
    assert view.status_code == 200 and view.json()["service"] == service
    events = token_api.get(r.json()["links"]["timeline"], headers=bearer(issuer, org)).json()
    assert events["items"][0]["actor"] == "user:alice"


def test_st19_refused_tokens_write_and_read_nothing(token_api, token_issuer, app_engine, catalogue):
    issuer = token_issuer[0]
    org, service, _ = catalogue
    now = int(datetime.now(UTC).timestamp())
    head, payload, sig = issuer.token("alice", ["aisre:responder"], org_id=str(org)).split(".")
    bad = {
        "expired": issuer.sign(
            issuer.claims("alice", ["aisre:responder"], org_id=str(org), now=now - 900)
        ),
        "wrong issuer": issuer.sign(issuer.claims("a", org_id=str(org)) | {"iss": "http://x"}),
        "wrong audience": issuer.sign(issuer.claims("a", org_id=str(org)) | {"aud": "x"}),
        "altered": f"{head}.{payload}.{sig[::-1]}",
    }
    body = ALERT["incident_create"] | {"service": service}
    for i, token in enumerate([None, *bad.values()]):
        headers = {"Idempotency-Key": f"st19-{i}-{org.hex[-12:]}"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        assert token_api.post("/v1/incidents", json=body, headers=headers).status_code == 401
    dev = {
        "X-Aisre-Dev-Caller": "alice",
        "X-Aisre-Dev-Org": str(org),
        "Idempotency-Key": "dev-12345",
    }
    assert token_api.post("/v1/incidents", json=body, headers=dev).status_code == 401
    assert rows(app_engine, incidents, org) == 0
    assert rows(app_engine, dead_letters, org) == 0  # refused callers leave no dead letter
    with app_engine.connect() as conn:
        assert not conn.execute(
            select(idempotency_keys.c.idem_key).where(
                idempotency_keys.c.idem_key.like(f"%{org.hex[-12:]}")
            )
        ).first()


def test_a_token_for_another_org_sees_nothing(token_api, token_issuer, catalogue):
    issuer = token_issuer[0]
    org, service, _ = catalogue
    body = ALERT["incident_create"] | {"service": service}
    made = token_api.post(
        "/v1/incidents",
        json=body,
        headers=bearer(issuer, org) | {"Idempotency-Key": f"org-{org.hex[-12:]}"},
    )
    other = bearer(issuer, uuid7(), groups=("aisre:incident_lead",))
    assert token_api.get(made.json()["links"]["self"], headers=other).status_code == 404
