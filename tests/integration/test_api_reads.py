"""Artifact 13.5 T1.5 (FR-36): evidence, diagnosis and action reads, the live timeline over SSE
(Artifact 7.9) and the signed export, against PostgreSQL as aisre_app."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text

from aisre.api.app import create_app
from aisre.api.auth import DEV_CALLER_HEADER, DEV_GROUPS_HEADER, DEV_ORG_HEADER
from aisre.api.export import verify
from aisre.api.stream import Timing
from aisre.config import Settings
from aisre.record import uuid7
from aisre.record.transitions import Transition, record_transition

ALERT = json.loads(
    (Path(__file__).parents[1] / "fixtures/reference-incident/alert.json").read_text()
)
FAST = Timing(poll_s=0.05, heartbeat_s=0.1, recheck_s=0.2)


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


@pytest.fixture
def api(app_engine, settings):
    with TestClient(create_app(settings, app_engine, stream_timing=FAST)) as c:
        yield c


def as_(org, groups: str = "aisre:viewer", caller: str = "vera") -> dict[str, str]:
    return {DEV_CALLER_HEADER: caller, DEV_ORG_HEADER: str(org), DEV_GROUPS_HEADER: groups}


def opened(api, org, service: str, quiet=None) -> str:
    if quiet is not None:
        return quiet(org, service)
    r = api.post(
        "/v1/incidents",
        json=ALERT["incident_create"] | {"service": service},
        headers=as_(org, "aisre:monitoring", "monitoring") | {"Idempotency-Key": f"rd-{org.hex}"},
    )
    assert r.status_code == 201, r.text
    return str(r.json()["incident_id"])


def move(engine, incident_id: str, *path: str) -> None:
    for to in path:
        with engine.begin() as conn:
            status, version = conn.execute(
                text("SELECT status, version FROM incidents WHERE incident_id = :i"),
                {"i": incident_id},
            ).one()
            record_transition(
                conn, Transition(incident_id, str(uuid7()), status, to, version, "test")
            )


def seed(engine, org, incident_id: str) -> dict[str, UUID]:
    """Two evidence rows, two hypotheses, an established diagnosis and a tested action."""
    ids = {k: uuid7() for k in ("art", "ev1", "ev2", "h1", "h2", "dia", "act", "diff")}
    p = {k: str(v) for k, v in ids.items()} | {"inc": incident_id, "org": str(org)}
    p |= {"ha": sha("a"), "hb": sha("b"), "hc": sha("c")}
    statements = [
        """INSERT INTO artifacts (artifact_id, org_id, incident_id, kind, storage_key, content_sha256,
             size_bytes, media_type, redacted, retain_until)
           VALUES (:art, :org, :inc, 'evidence_snapshot', 'obj/' || gen_random_uuid(), :ha, 10, 'text/plain',
             true, now() + interval '30 days')""",
        """INSERT INTO evidence (evidence_id, incident_id, kind, source_system, tool, collected_by,
             summary, artifact_id, content_sha256, strength_tier, attributes)
           VALUES (:ev1, :inc, 'change', 'release-log', 'read_changes', 'evidence-collector',
             'Deploy at 09:10', :art, :ha, 2, '{"revision": "r42"}')""",
        """INSERT INTO evidence (evidence_id, incident_id, kind, source_system, tool, collected_by,
             summary, content_sha256, gap_reason)
           VALUES (:ev2, :inc, 'gap', 'traces', 'read_traces', 'evidence-collector', 'No traces',
             :hb, 'source unavailable')""",
        """INSERT INTO hypotheses (hypothesis_id, incident_id, ordinal, statement, category,
             confirm_test, refute_test, confidence)
           VALUES (:h1, :inc, 1, 'Config change', 'deployment_config', 'revert clears',
             'still fails', 'medium'),
                  (:h2, :inc, 2, 'Dependency down', 'dependency', 'dep errors', 'dep healthy', 'low')""",
        "INSERT INTO hypothesis_evidence (hypothesis_id, evidence_id, relation) VALUES (:h1, :ev1, 'supports')",
        """INSERT INTO diagnoses (diagnosis_id, incident_id, revision, outcome, leading_hypothesis_id,
             category, confidence, summary, ruled_out, produced_by)
           VALUES (:dia, :inc, 1, 'established', :h1, 'deployment_config', 'medium', 'Bad config',
             '{H2: dep healthy}', 'diagnosis-engine')""",
        """INSERT INTO actions (action_id, incident_id, diagnosis_id, kind, target_service_id,
             target_environment_id, target_deployment, base_revision, expected_after_revision,
             description, expected_effect, risk, undo_plan, recovery_criteria,
             recovery_predicates_digest, hold_seconds, high_impact, policy_version)
           SELECT :act, :inc, :dia, 'config_revert', i.service_id, i.environment_id,
             'orders-prod-eu1', 'r42', 'r43', 'Revert', 'Errors stop', 'Low', 'Roll back',
             '[{"metric": "http_5xx_rate", "comparison": "<=", "threshold": 0.01}]', :hb, 600,
             false, 'p@1'
           FROM incidents i WHERE i.incident_id = :inc""",
        """INSERT INTO artifacts (artifact_id, org_id, incident_id, kind, storage_key, content_sha256,
             size_bytes, media_type, redacted, retain_until)
           VALUES (:diff, :org, :inc, 'patch_diff', 'obj/' || gen_random_uuid(), :hc, 10, 'text/x-diff', true,
             now() + interval '1 year')""",
        """INSERT INTO patches (patch_id, action_id, base_revision, diff_artifact_id, patch_sha256,
             files_changed, lines_added, lines_removed)
           VALUES (gen_random_uuid(), :act, 'r42', :diff, :hc, 1, 1, 1)""",
        "UPDATE actions SET status = 'testing', version = version + 1 WHERE action_id = :act",
        """INSERT INTO test_runs (test_run_id, action_id, patch_sha256, reproduced, outcome,
             started_at, finished_at, isolation)
           VALUES (gen_random_uuid(), :act, :hc, true, 'passed', now(), now(), 'development-runc')""",
    ]
    with engine.begin() as conn:
        for s in statements:
            conn.execute(text(s), p)
    return ids


@pytest.fixture
def seeded(api, app_engine, catalogue, quiet):
    org, service, _ = catalogue
    incident_id = opened(api, org, service, quiet)
    move(app_engine, incident_id, "investigating")
    return org, incident_id, seed(app_engine, org, incident_id)


def test_evidence_pages_and_filters_by_kind(api, seeded, caplog):
    org, incident_id, ids = seeded
    with caplog.at_level("INFO", logger="aisre.audit"):
        page = api.get(f"/v1/incidents/{incident_id}/evidence?limit=1", headers=as_(org)).json()
    first = min(ids["ev1"], ids["ev2"])  # evidence_id order: UUIDv7, so collection order
    assert [e["evidence_id"] for e in page["items"]] == [str(first)]
    assert page["next_cursor"] == str(first)
    rest = api.get(
        f"/v1/incidents/{incident_id}/evidence?cursor={page['next_cursor']}", headers=as_(org)
    ).json()
    assert [e["evidence_id"] for e in rest["items"]] == [str(max(ids["ev1"], ids["ev2"]))]
    assert rest["next_cursor"] is None
    gaps = api.get(f"/v1/incidents/{incident_id}/evidence?kind=gap", headers=as_(org)).json()
    assert [e["gap_reason"] for e in gaps["items"]] == ["source unavailable"]
    reads = [json.loads(m) for m in caplog.messages if '"aisre.content.read"' in m]
    assert reads and reads[0]["content"] == "evidence"


def test_the_current_diagnosis_with_its_hypotheses(api, seeded):
    org, incident_id, ids = seeded
    d = api.get(f"/v1/incidents/{incident_id}/diagnoses/current", headers=as_(org))
    assert d.status_code == 200, d.text
    body = d.json()
    assert body["diagnosis_id"] == str(ids["dia"]) and body["outcome"] == "established"
    assert [h["ordinal"] for h in body["hypotheses"]] == [1, 2]
    assert body["hypotheses"][0]["evidence"] == [
        {"evidence_id": str(ids["ev1"]), "relation": "supports", "note": None}
    ]


def test_no_diagnosis_yet_is_404(api, app_engine, catalogue, quiet):
    org, service, _ = catalogue
    incident_id = opened(api, org, service, quiet)
    r = api.get(f"/v1/incidents/{incident_id}/diagnoses/current", headers=as_(org))
    assert r.status_code == 404 and r.json()["code"] == "no_diagnosis"


def test_actions_with_patch_and_latest_test(api, seeded):
    org, incident_id, ids = seeded
    (action,) = api.get(f"/v1/incidents/{incident_id}/actions", headers=as_(org)).json()
    assert action["action_id"] == str(ids["act"]) and action["status"] == "testing"
    assert action["patch"]["patch_sha256"] == sha("c")
    assert action["latest_test"]["outcome"] == "passed"
    one = api.get(f"/v1/actions/{ids['act']}", headers=as_(org))
    assert one.status_code == 200 and one.json() == action


def test_another_service_reads_nothing(api, seeded):
    org, incident_id, ids = seeded
    other = as_(org, "aisre:viewer:another-svc")
    for path in (
        f"/v1/incidents/{incident_id}/evidence",
        f"/v1/incidents/{incident_id}/diagnoses/current",
        f"/v1/incidents/{incident_id}/actions",
        f"/v1/incidents/{incident_id}/timeline/export",
        f"/v1/incidents/{incident_id}/timeline/stream",
        f"/v1/actions/{ids['act']}",
    ):
        r = api.get(path, headers=other)
        assert r.status_code == 404, path


def sse(lines) -> tuple[list[dict], int]:
    """Parse an SSE body: the messages, and how many heartbeat comments came."""
    messages, current, beats = [], {}, 0
    for line in lines:
        if line == "":
            if current:
                messages.append(current)
            current = {}
        elif line.startswith(":"):
            beats += 1
        else:
            name, _, value = line.partition(": ")
            current[name] = value
    if current:
        messages.append(current)
    return messages, beats


def test_the_stream_replays_follows_beats_and_ends_on_close(api, app_engine, catalogue, quiet):
    org, service, _ = catalogue
    incident_id = opened(api, org, service, quiet)
    move(app_engine, incident_id, "investigating", "inconclusive", "handed_over")

    def close_later() -> None:
        time.sleep(0.4)
        with app_engine.begin() as conn:
            version = conn.execute(
                text("SELECT version FROM incidents WHERE incident_id = :i"), {"i": incident_id}
            ).scalar_one()
        r = api.post(
            f"/v1/incidents/{incident_id}/closure",
            json={},
            headers=as_(org, "aisre:incident_lead", "lena")
            | {"Idempotency-Key": f"close-{incident_id}", "If-Match": str(version)},
        )
        assert r.status_code == 200, r.text

    closer = threading.Thread(target=close_later)
    closer.start()
    url = f"/v1/incidents/{incident_id}/timeline/stream"
    with api.stream("GET", url, headers=as_(org) | {"Last-Event-ID": "1"}) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        messages, beats = sse(r.iter_lines())
    closer.join()
    assert messages[0] == {"retry": "3000"}
    events = messages[1:]
    assert [int(m["id"]) for m in events] == list(range(2, 2 + len(events)))
    assert events[-1]["event"] == "aisre.incident.closed.v1"
    assert json.loads(events[-1]["data"])["sequence"] == int(events[-1]["id"])
    assert beats >= 1


def test_a_finished_incident_replays_after_seq_and_ends(api, app_engine, catalogue, quiet):
    org, service, _ = catalogue
    incident_id = opened(api, org, service, quiet)
    move(app_engine, incident_id, "cancelled")
    url = f"/v1/incidents/{incident_id}/timeline/stream?after_seq=1"
    with api.stream("GET", url, headers=as_(org)) as r:
        messages, _ = sse(r.iter_lines())
    assert [m.get("event") for m in messages[1:]] == ["aisre.incident.status_changed.v1"]


def test_the_stream_ends_when_the_token_expires(app_engine, catalogue, token_issuer, settings):
    issuer, jwks_url = token_issuer
    signed_in = Settings(
        env="test",
        database_url=SecretStr(settings.database_url.get_secret_value()),
        oidc_issuer=issuer.url,
        oidc_audience=issuer.audience,
        oidc_jwks_url=jwks_url,
    )
    org, service, _ = catalogue
    with TestClient(create_app(signed_in, app_engine, stream_timing=FAST)) as c:
        mon = issuer.token("mon", ["aisre:monitoring"], org_id=str(org))
        made = c.post(
            "/v1/incidents",
            json=ALERT["incident_create"] | {"service": service},
            headers={"Authorization": f"Bearer {mon}", "Idempotency-Key": f"tok-{org.hex}"},
        )
        assert made.status_code == 201, made.text
        # Valid for about a second more, given the verifier's 30 s leeway (aisre.api.tokens).
        now = int(time.time()) - 30
        short = issuer.sign(
            issuer.claims("vera", ["aisre:viewer"], org_id=str(org), ttl_seconds=2, now=now)
        )
        started = time.monotonic()
        url = f"/v1/incidents/{made.json()['incident_id']}/timeline/stream"
        with c.stream("GET", url, headers={"Authorization": f"Bearer {short}"}) as r:
            assert r.status_code == 200
            messages, _ = sse(r.iter_lines())
        # The Compose worker may move the incident on meanwhile; what matters is the close.
        assert messages[1].get("event") == "aisre.incident.created.v1"
        assert time.monotonic() - started < 10  # closed by the re-check, not left open


def test_the_export_is_signed_and_carries_the_chain(api, app_engine, catalogue, quiet):
    org, service, _ = catalogue
    incident_id = opened(api, org, service, quiet)
    move(app_engine, incident_id, "investigating")
    r = api.get(f"/v1/incidents/{incident_id}/timeline/export", headers=as_(org))
    assert r.status_code == 200
    assert r.headers["content-disposition"].startswith("attachment;")
    doc = r.json()
    assert [e["sequence"] for e in doc["events"]] == [1, 2]
    assert (
        doc["events"][1]["prev_hash"] == doc["events"][0]["hash"] == doc["events"][1]["prev_hash"]
    )
    assert doc["chain_head"] == doc["events"][1]["hash"] and doc["chain_faults"] == []
    key = doc["signature"]["public_key"]
    assert verify(doc, key)
    doc["events"][0]["data"]["title"] = "edited"
    assert not verify(doc, key)


# ---------------------------------------------------------------- T1.24, Artifact 10.9
def test_a_cited_runbook_shows_its_text_only_to_its_groups(api, app_engine, catalogue, quiet):
    """A reader outside a runbook's permitted groups sees "restricted runbook (owner: ...)" in
    place of the passage, never its text; a reader in one of them sees it."""
    org, service, _ = catalogue
    incident_id = opened(api, org, service, quiet)
    passage = "Revert ORDER_TAX_RATE_BPS to the last good value through the config pipeline."
    p = {"org": str(org), "inc": incident_id, "h": sha(passage), "d": str(uuid7())}
    p |= {"ev": str(uuid7()), "ev2": str(uuid7()), "gone": str(uuid7()), "text": passage}
    attrs = {"doc_id": "rb-test", "doc_version": "v3", "heading_path": ["Orders", "Revert"]}
    with app_engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO documents (document_id, org_id, doc_ref, doc_version, doc_type,
                     title, body, services, allowed_groups, reviewed_at, owner, deprecated)
                   VALUES (:d, :org, 'rb-test', 'v3', 'runbook', 'Orders', :text, '{*}',
                     '{orders-team,sre}', now(), 'orders-team (synthetic)', false)"""
            ),
            p,
        )
        for ev, doc in (("ev", attrs), ("gone", attrs | {"doc_id": "rb-deleted"})):
            conn.execute(
                text(
                    """INSERT INTO evidence (evidence_id, incident_id, kind, source_system, tool,
                         collected_by, source_link, summary, content_sha256, attributes)
                       VALUES (CAST(:id AS uuid), :inc, 'runbook', 'runbook-corpus',
                         'search_runbooks', 'evidence-collector', 'corpus:rb-test@v3#1', :text,
                         :h, CAST(:a AS jsonb))"""
                ),
                p | {"id": p[ev], "a": json.dumps(doc)},
            )
        conn.execute(
            text(
                """INSERT INTO evidence (evidence_id, incident_id, kind, source_system, tool,
                     collected_by, source_link, summary, content_sha256)
                   VALUES (:ev2, :inc, 'log', 'logs', 'get_logs', 'evidence-collector', 'l',
                     'ValueError on POST /v1/orders', :h)"""
            ),
            p,
        )
    url = f"/v1/incidents/{incident_id}/evidence"
    outside = api.get(url, headers=as_(org, "aisre:viewer")).json()["items"]
    by_id = {e["evidence_id"]: e for e in outside}
    shown = by_id[p["ev"]]
    assert shown["summary"] == "restricted runbook (owner: orders-team (synthetic))"
    assert passage not in json.dumps(outside) and "Revert" not in json.dumps(shown)
    assert shown["attributes"] == {
        "doc_id": "rb-test",
        "doc_version": "v3",
        "restricted": True,
        "owner": "orders-team (synthetic)",
    }
    assert by_id[p["gone"]]["summary"] == "restricted runbook (owner: unknown)"
    assert by_id[p["ev2"]]["summary"] == "ValueError on POST /v1/orders"  # not a document
    inside = api.get(url, headers=as_(org, "aisre:viewer,sre")).json()["items"]
    assert {e["evidence_id"]: e for e in inside}[p["ev"]]["summary"] == passage
