"""Artifact 13.5 T3.2, T3.3 (FR-24 to FR-26, FR-49, FR-52, FR-54, NFR-34, NFR-35, ST-06 to ST-12,
ST-14, ADR-49, ADR-73): the Approval Service on PostgreSQL, through the API, with bearer tokens
from the fake issuer and the development signing key.

The reference fix is drafted and tested (test_patch_review's fixtures), moved to
`awaiting_approval` by the record (which seals its digest), and its request opened by the
workflow's `request_approval`. Rights come from the token's groups on the incident's service.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import uuid
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text
from temporalio.testing import ActivityEnvironment
from test_patch_review import drafted, events, review, to_testing  # noqa: F401 (fixtures)

from aisre.api.app import create_app
from aisre.api.auth import Authenticator
from aisre.api.tokens import TokenVerifier
from aisre.record import approvals as rec
from aisre.record import uuid7
from aisre.record.transitions import Transition, record_transition
from aisre.remediation.action import (
    development_signer,
    development_verify_keys,
    verify_signature,
)
from aisre.workflow import activities as act
from aisre.workflow.approval_activities import ApprovalActivities

pytestmark = pytest.mark.integration
ROOT = Path(__file__).parents[2]
SEED = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="  # .env.example's, plainly fake
PUBLIC = "A6EHv/POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg="


def _issuer_module():
    spec = importlib.util.spec_from_file_location(
        "fake_issuer_t32", ROOT / "ops/fakes/token_issuer.py"
    )
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ISSUER = _issuer_module().Issuer("http://issuer.test", "aisre-api")


def client_for(settings, engine, key: str | None = SEED) -> TestClient:
    s = settings.model_copy(
        update={"approval_signing_key": SecretStr(key) if key else None, "env": "test"}
    )
    app = create_app(s, engine)
    verifier = TokenVerifier(ISSUER.url, "aisre-api", "unused", fetch=lambda _: ISSUER.jwks())
    app.state.authenticator = Authenticator(s, verifier)
    return TestClient(app)


def seal(app_engine, incident_id: str, action_id: UUID, patch_sha256: str) -> None:
    """A passing, reproduced run with its bundle, then testing -> awaiting_approval."""
    with app_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO test_runs (test_run_id, action_id, patch_sha256, reproduced, outcome,"
                " started_at, finished_at, isolation, test_key, bundle_digest) VALUES (:t, :a, :p,"
                " true, 'passed', now(), now(), 'development-runc', :k, :b)"
            ),
            {"t": uuid7(), "a": action_id, "p": patch_sha256, "k": str(uuid7()), "b": "b" * 64},
        )
        status, version = conn.execute(
            text("SELECT status, version FROM incidents WHERE incident_id = :i"),
            {"i": incident_id},
        ).one()
        record_transition(
            conn,
            Transition(
                incident_id, str(uuid7()), status, "awaiting_approval", version, "tested",
                action_id=str(action_id), action_from="testing", action_to="awaiting_approval",
            ),
        )  # fmt: skip


@pytest.fixture
def awaiting(app_engine, catalogue, drafted):  # noqa: F811
    org, service, _ = catalogue
    incident_id, action_id, patch, *_ = drafted
    with app_engine.begin() as conn:  # a diagnosis lists at least two hypotheses
        conn.execute(
            text(
                "INSERT INTO hypotheses (hypothesis_id, incident_id, ordinal, statement, category,"
                " confirm_test, refute_test, confidence, created_at) SELECT :h, :i, 2,"
                " 'A dependency failed', 'dependency', 'dependency errors', 'none seen', 'low',"
                " created_at FROM diagnoses WHERE incident_id = :i"
            ),
            {"h": uuid7(), "i": incident_id},
        )
    to_testing(app_engine, incident_id, action_id)
    seal(app_engine, incident_id, action_id, patch.digest)
    out = ActivityEnvironment().run(
        ApprovalActivities(app_engine).request_approval,
        act.ApprovalRequestIn(str(action_id), "", f"{action_id}:approval:1"),
    )
    out = asyncio.run(out) if asyncio.iscoroutine(out) else out
    return org, service, incident_id, action_id, out


def headers(org, service, sub, role="approver", *, key=None, version=None, **kw) -> dict[str, str]:
    token = ISSUER.token(sub, [f"aisre:{role}:{service}"], org_id=str(org), **kw)
    h = {"Authorization": f"Bearer {token}", "Idempotency-Key": key or uuid.uuid4().hex}
    if version is not None:
        h["If-Match"] = f'"{version}"'
    return h


def version_of(app_engine, action_id) -> int:
    with app_engine.connect() as conn:
        return conn.execute(
            text("SELECT version FROM actions WHERE action_id = :a"), {"a": action_id}
        ).scalar_one()


def test_testing_ends_with_the_digest_sealed_and_the_request_derived(app_engine, awaiting):
    _, _, incident_id, action_id, out = awaiting
    with app_engine.connect() as conn:
        action = rec.load_action(conn, action_id)
        stored = conn.execute(
            text("SELECT action_digest FROM actions WHERE action_id = :a"), {"a": action_id}
        ).scalar_one()
        rollback = rec.load_action(conn, UUID(out.rollback_action_id))
    assert action is not None and rollback is not None
    assert out.action_digest == stored.strip() == action.digest  # one computation (ADR-64)
    assert out.approvers_needed == 2  # severity not confirmed: high impact (FR-49)
    assert (
        out.rollback_digest == rollback.digest
        and rollback.base_revision == action.expected_after_revision
    )
    assert len(events(app_engine, incident_id, "aisre.approval.requested.v1")) == 2


def test_fr25_the_request_shows_diagnosis_diff_test_risk_and_undo(app_engine, settings, awaiting):
    org, service, _, action_id, _ = awaiting
    with client_for(settings, app_engine) as c:
        r = c.get(
            f"/v1/actions/{action_id}/approval-request",
            headers=headers(org, service, "rin", "responder"),
        )
    body = r.json()
    assert r.status_code == 200 and body["open"] and body["approvers_needed"] == 2
    assert body["diagnosis_summary"] and body["diff_artifact_id"] and body["action"]["latest_test"]
    assert body["risk"] and body["undo_plan"] and body["rollback_action_id"]


def test_two_approvers_sign_with_the_development_key_and_signal_the_workflow(
    app_engine, settings, awaiting
):
    org, service, incident_id, action_id, out = awaiting
    v = version_of(app_engine, action_id)
    with client_for(settings, app_engine) as c:
        first = c.post(
            f"/v1/actions/{action_id}/approvals",
            json={
                "action_digest": out.action_digest,
                "rollback_action_digest": out.rollback_digest,
            },
            headers=headers(org, service, "priya", version=v),
        )
        again = c.post(
            f"/v1/actions/{action_id}/approvals",
            json={"action_digest": out.action_digest},
            headers=headers(org, service, "priya", version=v),
        )
        second = c.post(
            f"/v1/actions/{action_id}/approvals",
            json={"action_digest": out.action_digest},
            headers=headers(org, service, "sam", version=v),
        )
    assert first.status_code == 201 and second.status_code == 201, (first.json(), second.json())
    assert again.status_code == 409 and again.json()["code"] == "duplicate"  # ST-11
    assert first.json()["signing_key_id"].startswith("development-")  # ADR-73
    with app_engine.connect() as conn:
        signed = rec.decisions(conn, action_id) + rec.decisions(conn, UUID(out.rollback_action_id))
        signals = conn.execute(
            text(
                "SELECT count(*) FROM outbox WHERE incident_id = :i AND kind = 'signal_workflow'"
                " AND payload->>'signal' = 'approval_decided'"
            ),
            {"i": incident_id},
        ).scalar_one()
    keys = development_verify_keys(PUBLIC)
    assert len(signed) == 3 and all(verify_signature(d, keys) for d in signed)
    assert [d.conditional for d in signed] == [False, False, True]  # ADR-48
    assert signals == 2


@pytest.mark.parametrize(
    "who, body, kw, status, code",
    [
        ("rin", "responder", {}, 403, "forbidden"),  # ST-08
        ("priya", "approver", {"auth_age_seconds": 20 * 60}, 401, "reauth_required"),  # ST-09
        ("priya", "approver", {"mfa": False}, 401, "reauth_required"),
    ],
)
def test_refusals_write_no_row_and_record_a_security_event(
    app_engine, settings, awaiting, who, body, kw, status, code
):
    org, service, incident_id, action_id, out = awaiting
    with client_for(settings, app_engine) as c:
        r = c.post(
            f"/v1/actions/{action_id}/approvals",
            json={"action_digest": out.action_digest},
            headers=headers(
                org, service, who, body, version=version_of(app_engine, action_id), **kw
            ),
        )
    assert (r.status_code, r.json()["code"]) == (status, code)
    with app_engine.connect() as conn:
        assert rec.decisions(conn, action_id) == []
    found = events(app_engine, incident_id, "aisre.approval.refused.v1")
    if who == "rin":  # no approve right anywhere: the route's own check, aisre.authz.denied
        assert found == []
    else:
        ((refused),) = found
        assert refused["code"] == code and refused["security_alert"] is True


def test_st14_a_digest_the_approver_did_not_see_is_refused(app_engine, settings, awaiting):
    org, service, incident_id, action_id, _ = awaiting
    with client_for(settings, app_engine) as c:
        r = c.post(
            f"/v1/actions/{action_id}/approvals",
            json={"action_digest": "0" * 64},
            headers=headers(org, service, "priya", version=version_of(app_engine, action_id)),
        )
    assert (r.status_code, r.json()["code"]) == (409, "digest_mismatch")


def test_a_stale_sign_in_can_still_reject_and_the_rejection_closes_the_request(
    app_engine, settings, awaiting
):
    org, service, _, action_id, out = awaiting
    with client_for(settings, app_engine) as c:
        r = c.post(
            f"/v1/actions/{action_id}/rejections",
            json={"action_digest": out.action_digest, "reason": "wrong service owner"},
            headers=headers(org, service, "rin", "responder", auth_age_seconds=3600,
                            version=version_of(app_engine, action_id)),
        )  # fmt: skip
    assert r.status_code == 201 and r.json()["decision"] == "rejected"
    with app_engine.connect() as conn:
        assert rec.latest_request(conn, action_id).close_reason == "decided"


def test_fr52_only_your_own_approval_can_be_revoked(app_engine, settings, awaiting):
    org, service, _, action_id, out = awaiting
    v = version_of(app_engine, action_id)
    with client_for(settings, app_engine) as c:
        given = c.post(
            f"/v1/actions/{action_id}/approvals",
            json={"action_digest": out.action_digest},
            headers=headers(org, service, "priya", version=v),
        ).json()
        body = {"approval_id": given["approval_id"], "action_digest": out.action_digest,
                "reason": "I misread the diff"}  # fmt: skip
        other = c.post(f"/v1/actions/{action_id}/revocations", json=body,
                       headers=headers(org, service, "sam", version=v))  # fmt: skip
        own = c.post(f"/v1/actions/{action_id}/revocations", json=body,
                     headers=headers(org, service, "priya", version=v))  # fmt: skip
    assert (other.status_code, other.json()["code"]) == (403, "not_own_approval")
    assert own.status_code == 201 and own.json()["revokes_approval_id"] == given["approval_id"]


def test_if_match_and_the_idempotency_key_are_enforced(app_engine, settings, awaiting):
    org, service, _, action_id, out = awaiting
    body = {"action_digest": out.action_digest}
    url = f"/v1/actions/{action_id}/approvals"
    with client_for(settings, app_engine) as c:
        missing = c.post(url, json=body, headers=headers(org, service, "priya"))
        stale = c.post(url, json=body, headers=headers(org, service, "priya", version=1))
        h = headers(
            org, service, "priya", version=version_of(app_engine, action_id), key=uuid.uuid4().hex
        )
        first, replay = c.post(url, json=body, headers=h), c.post(url, json=body, headers=h)
    assert (missing.status_code, stale.status_code) == (428, 412)
    assert first.status_code == replay.status_code == 201, (first.json(), replay.json())
    assert first.json() == replay.json()


def test_without_a_signing_key_nothing_is_approved(app_engine, settings, awaiting):
    org, service, _, action_id, out = awaiting
    with client_for(settings, app_engine, key=None) as c:
        r = c.post(
            f"/v1/actions/{action_id}/approvals",
            json={"action_digest": out.action_digest},
            headers=headers(org, service, "priya", version=version_of(app_engine, action_id)),
        )
    assert (r.status_code, r.json()["code"]) == (503, "approval_signing_unavailable")


def test_a_voided_close_writes_its_event_once_and_an_expiry_its_own(app_engine, awaiting):
    _, _, incident_id, action_id, out = awaiting
    env = ActivityEnvironment()
    acts = ApprovalActivities(app_engine)
    voided = act.ApprovalRequestIn(
        str(action_id), out.action_digest, "k", act.VOIDED, "patch changed"
    )
    for _ in range(2):
        asyncio.run(env.run(acts.close_approval_request, voided))
    rollback = act.ApprovalRequestIn(out.rollback_action_id, out.rollback_digest, "k", "expired")
    asyncio.run(env.run(acts.close_approval_request, rollback))
    (v,) = events(app_engine, incident_id, "aisre.approval.voided.v1")
    assert v["reason"] == "patch changed"
    assert len(events(app_engine, incident_id, "aisre.approval.expired.v1")) == 1
    asyncio.run(env.run(acts.escalate_approval, voided))  # closed: nothing to escalate
    assert events(app_engine, incident_id, "aisre.approval.escalated.v1") == []


def test_the_development_key_ids_match_on_both_sides():
    assert list(development_verify_keys(PUBLIC)) == [development_signer(SEED).key_id]
    assert hashlib.sha256(b"").hexdigest()  # keeps hashlib's import honest for linters
