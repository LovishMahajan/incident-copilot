"""Artifact 13.5 T0.9: the API's offline checks. Routes against PostgreSQL are in
tests/integration/test_api.py.

The import test is T0.9's named test: `aisre.api` never reaches `temporalio`, so the API can
only start a workflow through the outbox (ADR-19, Artifact 13.1), in the Verifier test's style
(ST-39) plus a run-time check of what importing the app loads.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import create_engine

import aisre.api
import aisre.record
from aisre.api.alerts import to_incident_create
from aisre.api.app import create_app
from aisre.api.auth import DEV_CALLER_HEADER
from aisre.config import Settings
from aisre.contracts import IncidentCreate

ROOT = Path(__file__).parents[2]
ALERT = ROOT / "tests/fixtures/reference-incident/alert.json"
UNREACHABLE = "postgresql+psycopg://aisre_app@127.0.0.1:1/aisre?connect_timeout=1"


def imported_modules(package) -> set[str]:
    found: set[str] = set()
    for path in Path(package.__file__).parent.rglob("*.py"):
        for n in ast.walk(ast.parse(path.read_text())):
            if isinstance(n, ast.ImportFrom) and n.module:
                found |= {n.module} | {f"{n.module}.{a.name}" for a in n.names}
            elif isinstance(n, ast.Import):
                found |= {a.name for a in n.names}
    return found


@pytest.mark.parametrize("package", [aisre.api, aisre.record])
def test_api_and_record_never_import_temporalio(package):
    modules = imported_modules(package)
    assert not {m for m in modules if m.split(".")[0] == "temporalio"}
    assert not {m for m in modules if m.startswith("aisre.workflow")}
    assert modules & {"aisre.contracts"}  # it reads real imports


def test_importing_the_api_loads_no_temporalio():
    code = (
        "import sys, aisre.api.app; "
        "print(sorted(m for m in sys.modules if m.split('.')[0] == 'temporalio'))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


@pytest.fixture
def client():
    settings = Settings(env="test", database_url=SecretStr(UNREACHABLE))
    with TestClient(create_app(settings, create_engine(UNREACHABLE))) as c:
        yield c


def test_the_app_refuses_to_start_in_production():
    with pytest.raises(RuntimeError, match="development-only"):
        create_app(Settings(env="production"), create_engine(UNREACHABLE))


def test_reference_alert_maps_to_its_incident_create():
    doc = json.loads(ALERT.read_text())
    mapped = to_incident_create(doc)
    assert mapped == IncidentCreate.model_validate(doc["incident_create"])
    assert (mapped.service, mapped.fingerprint) == (
        "orders-api",
        "orders-api:OrdersApiHighErrorRate",
    )
    assert to_incident_create(doc["incident_create"]) == mapped  # a plain body passes through


def test_an_envelope_with_other_keys_is_not_unwrapped():
    doc = json.loads(ALERT.read_text()) | {"extra": 1}
    with pytest.raises(ValidationError):
        to_incident_create(doc)


def test_livez(client):
    r = client.get("/livez", headers={"X-Request-ID": "req-1"})
    assert r.status_code == 200 and r.headers["X-Request-ID"] == "req-1"


def test_readyz_is_503_without_the_database(client):
    r = client.get("/readyz")
    assert r.status_code == 503
    assert r.headers["content-type"] == "application/problem+json"
    assert r.json()["code"] == "not_ready"


@pytest.mark.parametrize(
    ("headers", "status", "code"),
    [
        ({"Idempotency-Key": "key-00000001"}, 401, "unauthenticated"),
        (
            {DEV_CALLER_HEADER: "Not A Name", "Idempotency-Key": "key-00000001"},
            401,
            "unauthenticated",
        ),
        ({DEV_CALLER_HEADER: "alice"}, 400, "idempotency_key_required"),
        ({DEV_CALLER_HEADER: "alice", "Idempotency-Key": "short"}, 400, "idempotency_key_invalid"),
    ],
)
def test_intake_refusals_are_problem_details(client, headers, status, code):
    body = json.loads(ALERT.read_text())
    r = client.post("/v1/incidents", json=body, headers=headers)
    assert r.status_code == status
    problem = r.json()
    assert problem["code"] == code and problem["status"] == status
    assert problem["type"].endswith(code) and problem["request_id"]


def test_an_invalid_body_is_422_with_field_errors(client):
    r = client.post(
        "/v1/incidents",
        json={"service": "orders-api"},
        headers={DEV_CALLER_HEADER: "alice", "Idempotency-Key": "key-00000001"},
    )
    assert r.status_code == 422
    problem = r.json()
    assert problem["code"] == "validation_failed"
    assert {tuple(e["loc"]) for e in problem["errors"]} >= {("environment",), ("title",)}
    assert all("input" not in e for e in problem["errors"])


def test_unknown_route_is_a_problem(client):
    r = client.get("/v1/nothing")
    assert r.status_code == 404 and r.json()["code"] == "not_found"
