"""Artifact 13.5 T2.8 (FR-18, FR-19, ADR-72): a drafted patch in PostgreSQL, and its draft pull
request through the review system.

`store_patch` writes the action, the two artifacts, the patch and `aisre.action.proposed.v1` once;
`open_draft_pull_request` refuses an action whose test didn't pass, opens a draft for one that did,
records `patches.review_ref` once (migration 0008) and `aisre.action.review_opened.v1` with the
isolation label, and gives the same pull request on a retry. The review system is Compose's fake,
in-process.
"""

from __future__ import annotations

import asyncio
import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from aisre.evidence.snapshots import MemorySnapshots
from aisre.record import patches as rec
from aisre.record import uuid7
from aisre.record.transitions import Transition, record_transition
from aisre.remediation.patch import build_patch_from_contents
from aisre.remediation.review import ReviewActivities, ReviewClient
from aisre.workflow import activities as act

ROOT = Path(__file__).parents[2]
ENV = "deploy/production.env"


def _fake_review():
    spec = importlib.util.spec_from_file_location("fake_review", ROOT / "ops/fakes/review.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def review(reference_repo):
    fake = _fake_review()
    repo, tags = reference_repo
    server = fake.serve(fake.Review(fake.Repo(repo, "main")))
    yield f"http://127.0.0.1:{server.server_address[1]}", repo, tags
    server.shutdown()


@pytest.fixture
def drafted(app_engine, catalogue, quiet, review):
    """An incident with an established diagnosis and the reference fix stored as its action."""
    org, service, _ = catalogue
    _, repo, tags = review
    incident_id = quiet(org, service)
    with app_engine.begin() as conn:
        status, version = conn.execute(
            text("SELECT status, version FROM incidents WHERE incident_id = :i"),
            {"i": incident_id},
        ).one()
        record_transition(
            conn, Transition(incident_id, str(uuid7()), status, "investigating", version, "test")
        )
        h, d = uuid7(), uuid7()
        conn.execute(
            text(
                "INSERT INTO hypotheses (hypothesis_id, incident_id, ordinal, statement, category,"
                " confirm_test, refute_test, confidence) VALUES (:h, :i, 1,"
                " 'ORDER_TAX_RATE_BPS went from 825 to 8.25', 'deployment_config', 'revert clears',"
                " 'still fails', 'high')"
            ),
            {"h": h, "i": incident_id},
        )
        conn.execute(
            text(
                "INSERT INTO diagnoses (diagnosis_id, incident_id, revision, outcome,"
                " leading_hypothesis_id, category, confidence, summary, ruled_out, produced_by)"
                " VALUES (:d, :i, 1, 'established', :h, 'deployment_config', 'high', 'Tax rate',"
                " '{}', 'diagnosis-engine')"
            ),
            {"d": d, "h": h, "i": incident_id},
        )
    before = (repo / ENV).read_text()
    action_id = uuid7()
    patch = build_patch_from_contents(
        old={ENV: before},
        new={ENV: before.replace("=8.25", "=825")},
        rationale="the rate is in basis points; 8.25 fails int()",
        evidence=[str(uuid7())],
        incident_id=incident_id,
        diagnosis_id=str(d),
        candidate_no=1,
        action_kind="config_revert",
        repository="orders-api",
        base_revision=tags["cfg-42"],
        target_service="orders-api",
        target_environment="production",
        summary="Revert ORDER_TAX_RATE_BPS to 825",
        reverted_commit=tags["cfg-42"],
    )
    new = rec.NewPatch(
        action_id=action_id,
        incident_id=UUID(incident_id),
        diagnosis_id=d,
        kind="config_revert",
        target_deployment="orders-api-production",
        base_revision=patch.base_revision,
        expected_after_revision=f"{patch.base_revision}+patch:{patch.digest[:16]}",
        description="Revert ORDER_TAX_RATE_BPS from 8.25 to 825.",
        expected_effect="Orders stop failing with HTTP 500.",
        risk="Low: one configuration value, restored.",
        undo_plan="Revert the pull request's merge.",
        recovery_criteria=[{"metric": "http_5xx_rate", "comparison": "<=", "threshold": 0.01}],
        policy_version="edit-policy@2|approval-policy@1",
        patch_sha256=patch.digest,
        canonical=patch.canonical_bytes(),
        review=patch.model_dump_json().encode(),
        files_changed=1,
        lines_added=1,
        lines_removed=1,
        change_author="synthetic-engineer-a",  # the reverted commit's author (ADR-49)
    )
    blobs = MemorySnapshots()
    canonical_key, review_key = rec.patch_keys(new.incident_id, action_id)
    blobs.put(canonical_key, new.canonical, "application/json")
    blobs.put(review_key, new.review, "application/json")
    with app_engine.begin() as conn:
        stored = rec.store_patch(conn, new, datetime.now(UTC))
    return incident_id, action_id, patch, new, blobs, stored


def events(app_engine, incident_id, kind):
    with app_engine.connect() as conn:
        return [
            r.data
            for r in conn.execute(
                text("SELECT data FROM timeline_events WHERE incident_id = :i AND type = :t"),
                {"i": incident_id, "t": kind},
            )
        ]


def to_testing(app_engine, incident_id, action_id):
    with app_engine.begin() as conn:
        status, version = conn.execute(
            text("SELECT status, version FROM incidents WHERE incident_id = :i"),
            {"i": incident_id},
        ).one()
        record_transition(
            conn,
            Transition(
                incident_id, str(uuid7()), status, "diagnosed", version, "reviewed diagnosis"
            ),
        )
        record_transition(
            conn,
            Transition(
                incident_id,
                str(uuid7()),
                "diagnosed",
                "testing",
                version + 1,
                "patch drafted",
                action_id=str(action_id),
                action_from="proposed",
                action_to="testing",
            ),
        )


def a_test_run(app_engine, action_id, patch_sha256, outcome="passed", reproduced=True) -> str:
    run_id = uuid7()
    with app_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO test_runs (test_run_id, action_id, patch_sha256, reproduced, outcome,"
                " started_at, finished_at, isolation, test_key) VALUES (:t, :a, :p, :r,"
                " CAST(:o AS test_outcome), now(), now(), 'development-runc', :k)"
            ),
            {"t": run_id, "a": action_id, "p": patch_sha256, "r": reproduced, "o": outcome,
             "k": f"{run_id}:k"},
        )  # fmt: skip
    return str(run_id)


def test_a_drafted_patch_is_stored_once_with_its_action_and_event(app_engine, drafted):
    incident_id, action_id, patch, new, _, stored = drafted
    assert (stored.action_id, stored.patch_sha256, stored.kind) == (
        action_id,
        patch.digest,
        "config_revert",
    )
    assert stored.hold_seconds == 600  # the service's (ADR-69)
    with app_engine.begin() as conn:
        again = rec.store_patch(conn, new, datetime.now(UTC))
        row = conn.execute(
            text(
                "SELECT a.status, a.action_digest, p.patch_sha256, p.review_ref, ar.content_sha256"
                " FROM actions a JOIN patches p USING (action_id)"
                " JOIN artifacts ar ON ar.artifact_id = p.diff_artifact_id WHERE a.action_id = :a"
            ),
            {"a": action_id},
        ).one()
        artifacts = conn.execute(
            text("SELECT count(*) FROM artifacts WHERE incident_id = :i AND kind = 'patch_diff'"),
            {"i": incident_id},
        ).scalar_one()
    assert again == stored and artifacts == 2
    assert row[0] == "proposed" and row[1] is None and row[3] is None
    assert row[2].strip() == patch.digest == row[4]  # the canonical artifact's hash is the patch's
    (proposed,) = events(app_engine, incident_id, "aisre.action.proposed.v1")
    assert proposed["action_id"] == str(action_id) and proposed["kind"] == "config_revert"


def test_no_pull_request_without_a_passing_test(app_engine, drafted, review):
    incident_id, action_id, patch, _, blobs, _ = drafted
    to_testing(app_engine, incident_id, action_id)
    url, _, _ = review
    activities = ReviewActivities(app_engine, blobs, ReviewClient(url, SecretStr("t")))
    for outcome, reproduced in (("failed", True), ("not_reproduced", False)):
        run_id = a_test_run(app_engine, action_id, patch.digest, outcome, reproduced)
        with pytest.raises(ApplicationError) as e:
            asyncio.run(
                ActivityEnvironment().run(
                    activities.open_draft_pull_request,
                    act.ReviewIn(str(action_id), run_id, f"{action_id}:review:1"),
                )
            )
        assert e.value.type == "NotTested"
    assert events(app_engine, incident_id, "aisre.action.review_opened.v1") == []


def test_a_passing_fix_becomes_one_draft_pull_request_recorded_once(app_engine, drafted, review):
    incident_id, action_id, patch, _, blobs, _ = drafted
    to_testing(app_engine, incident_id, action_id)
    run_id = a_test_run(app_engine, action_id, patch.digest)
    url, _, _ = review
    activities = ReviewActivities(app_engine, blobs, ReviewClient(url, SecretStr("t")))
    inp = act.ReviewIn(str(action_id), run_id, f"{action_id}:review:1")
    out = asyncio.run(ActivityEnvironment().run(activities.open_draft_pull_request, inp))
    again = asyncio.run(ActivityEnvironment().run(activities.open_draft_pull_request, inp))
    assert out.review_ref.startswith("orders-api#") and again.review_ref == out.review_ref
    (opened,) = events(app_engine, incident_id, "aisre.action.review_opened.v1")
    assert opened["review_ref"] == out.review_ref and opened["draft"] is True
    assert opened["isolation"] == "development-runc" and opened["test_run_id"] == run_id
    assert opened["required_reviewers"] == ["security-lead"]
    number = int(out.review_ref.split("#")[1])
    pr = ReviewClient(url, SecretStr("t")).pull("orders-api", number)
    assert pr is not None and pr.draft and not pr.merged
    import json
    import urllib.request

    with urllib.request.urlopen(f"{url}/_fake/pulls") as resp:
        body = next(p for p in json.loads(resp.read()) if p["number"] == number)["body"]
    for part in ("Hypothesis: ORDER_TAX_RATE_BPS went from 825 to 8.25", "Expected effect:",
                 "Risk:", "Undo plan:", "Evidence:", "-ORDER_TAX_RATE_BPS=8.25",
                 "isolation development-runc", "proves the flow, not isolation", run_id):  # fmt: skip
        assert part in body, part
    with app_engine.begin() as conn, pytest.raises(DBAPIError, match="write-once"):
        conn.execute(
            text("UPDATE patches SET review_ref = 'orders-api#999' WHERE action_id = :a"),
            {"a": action_id},
        )
    with app_engine.begin() as conn, pytest.raises(DBAPIError, match="permission denied|frozen"):
        conn.execute(
            text("UPDATE patches SET base_revision = 'x' WHERE action_id = :a"), {"a": action_id}
        )
