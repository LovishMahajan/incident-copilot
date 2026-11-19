"""Artifact 13.5 T3.8, T3.17 (FR-30, FR-31, FR-50, Artifact 11.12): the Recovery Verifier over
PostgreSQL, as `aisre_app`, reading the fake monitoring source's live metrics, which follow the
fake release process, both in-process.

The reference fix is approved and applied by the real dispatcher (test_dispatch_ledger's
fixtures). Time is virtual: each of the verifier's sleeps moves its clock on, and the fake
monitoring source serves any minute from what the release process runs, so a 10-minute hold takes
no wall-clock time. An ineffective fix is planted as the demo plants it: a runtime override of
`ORDER_TAX_RATE_BPS` that the repository, so the sandbox, never sees (ops/fakes/release.py).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from temporalio.testing import ActivityEnvironment
from test_approvals import awaiting  # noqa: F401 (fixtures)
from test_dispatch_ledger import (  # noqa: F401 (fixtures)
    _fake,
    approved,
    client,
    dispatcher,
    dispatcher_engine,
    released,
    status,
)
from test_patch_review import drafted, events, review  # noqa: F401 (fixtures)

from aisre.record.execution import PgTrustedStore
from aisre.reliability import telemetry
from aisre.remediation.dispatcher import Outcome
from aisre.verification.verification import criteria_digest
from aisre.verification.verifier import LiveMonitoring, RecoveryVerifier
from aisre.workflow import activities as act

pytestmark = pytest.mark.integration
monitoring_fake = _fake("monitoring")


class Clock:
    """Real time plus an offset that each sleep moves on."""

    def __init__(self) -> None:
        self.offset = timedelta(0)
        self.sleeps = 0

    def now(self) -> datetime:
        return datetime.now(UTC) + self.offset

    async def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.offset += timedelta(seconds=seconds)


@pytest.fixture
def monitored(released):  # noqa: F811
    release, url = released
    server = monitoring_fake.serve(release_url=url)
    yield release, LiveMonitoring(f"http://127.0.0.1:{server.server_address[1]}")
    server.shutdown()


@pytest.fixture
def applied(app_engine, dispatcher_engine, approved, released):  # noqa: F811
    service, incident_id, action_id, blobs = approved
    _, url = released
    d = dispatcher(
        dispatcher_engine, service, client(url, PgTrustedStore(dispatcher_engine, blobs)), blobs
    )

    def apply():
        res = asyncio.run(d.execute(str(action_id)))
        assert res.outcome == Outcome.APPLIED, res.reason
        with app_engine.connect() as conn:
            return conn.execute(
                text("SELECT attempt_id::text FROM execution_attempts WHERE action_id = :a"),
                {"a": action_id},
            ).scalar_one()

    return incident_id, action_id, apply


def run(fn, *args):
    return asyncio.run(ActivityEnvironment().run(fn, *args))


def verifier(app_engine, monitoring, clock, **kw) -> RecoveryVerifier:
    return RecoveryVerifier(
        app_engine, monitoring, poll_seconds=30, clock=clock.now, sleep=clock.sleep, **kw
    )


def hold_of(app_engine, action_id) -> int:
    with app_engine.connect() as conn:
        return conn.execute(
            text("SELECT hold_seconds FROM actions WHERE action_id = :a"), {"a": action_id}
        ).scalar_one()


def verification(app_engine, vid):
    with app_engine.connect() as conn:
        return (
            conn.execute(text("SELECT * FROM verifications WHERE verification_id = :v"), {"v": vid})
            .mappings()
            .one()
        )


def outcomes(verdict: str) -> float:
    value = telemetry.REGISTRY.get_sample_value(
        "aisre_verification_outcomes_total",
        {"verdict": verdict, "action_kind": "config_revert", "category": "deployment_config"},
    )
    return value or 0.0


def test_fr30_fr50_an_effective_fix_is_verified_only_after_the_whole_hold(
    app_engine, applied, monitored
):
    incident_id, action_id, apply = applied
    _, monitoring = monitored
    attempt_id = apply()
    clock, before = Clock(), outcomes("verified")
    v = verifier(app_engine, monitoring, clock, watch_seconds=120)
    key = act.VerifyIn(str(action_id), attempt_id, 600, f"{action_id}:verify:1")
    out = run(v.verify_recovery, key)
    assert out.result == "verified" and out.watch_seconds == 120
    row = verification(app_engine, out.verification_id)
    hold = hold_of(app_engine, action_id)
    # FR-50: declared only once the whole hold had passed, minute by minute.
    assert row["window_end"] - row["window_start"] == timedelta(seconds=hold)
    assert clock.now() >= row["window_end"]
    assert len(row["measurements"]["intervals"]) >= hold // 60
    # FR-30: what was judged is what was recorded before execution, and approved through its digest.
    with app_engine.connect() as conn:
        recorded, digest, created = conn.execute(
            text(
                "SELECT recovery_criteria, recovery_predicates_digest, created_at FROM actions "
                "WHERE action_id = :a"
            ),
            {"a": action_id},
        ).one()
        started = conn.execute(
            text("SELECT started_at FROM execution_attempts WHERE action_id = :a"),
            {"a": action_id},
        ).scalar_one()
    assert row["criteria"] == recorded and criteria_digest(recorded) == digest.strip()
    assert created < started
    # FR-31: read from monitoring, by the verifier, never the dispatcher's word.
    assert row["verified_by"] == "system:recovery-verifier"
    assert "monitoring" in row["measurements"]["source"]
    (done,) = events(app_engine, incident_id, "aisre.verification.completed.v1")
    assert done["result"] == "verified" and outcomes("verified") == before + 1
    # Idempotent on the key: the same answer, no second row or event.
    second = run(v.verify_recovery, key)
    assert (second.verification_id, second.result) == (out.verification_id, "verified")
    assert len(events(app_engine, incident_id, "aisre.verification.completed.v1")) == 1


def test_fr32_an_ineffective_fix_fails_at_its_first_bad_minute(app_engine, applied, monitored):
    """The runtime override keeps 8.25 live: the revert passes in the sandbox and changes
    nothing here. One bad minute is `failed`, long before the hold ends."""
    incident_id, action_id, apply = applied
    release, monitoring = monitored
    release.override({"set": {"ORDER_TAX_RATE_BPS": "8.25"}})
    attempt_id = apply()
    clock = Clock()
    out = run(
        verifier(app_engine, monitoring, clock).verify_recovery,
        act.VerifyIn(str(action_id), attempt_id, 600, f"{action_id}:verify:1"),
    )
    assert out.result == "failed" and out.watch_seconds == 0
    row = verification(app_engine, out.verification_id)
    assert clock.now() < row["window_end"]
    assert row["measurements"]["per_predicate"]["http_5xx_rate"].startswith("violated")
    assert status(app_engine, action_id) == "executed"  # the verifier writes nothing else


def test_an_unreadable_source_is_inconclusive_never_verified(app_engine, applied):
    incident_id, action_id, apply = applied
    attempt_id = apply()
    out = run(
        verifier(
            app_engine, LiveMonitoring("http://127.0.0.1:9", timeout=0.2), Clock()
        ).verify_recovery,
        act.VerifyIn(str(action_id), attempt_id, 600, f"{action_id}:verify:1"),
    )
    assert out.result == "inconclusive"
    assert verification(app_engine, out.verification_id)["measurements"]["reasons"]


def test_t317_a_regression_after_the_hold_is_flagged_and_linked_and_nothing_is_undone(
    app_engine, applied, monitored
):
    incident_id, action_id, apply = applied
    release, monitoring = monitored
    attempt_id = apply()
    clock = Clock()
    v = verifier(app_engine, monitoring, clock, watch_seconds=600)
    out = run(v.verify_recovery, act.VerifyIn(str(action_id), attempt_id, 600, "k:verify"))
    assert out.result == "verified"
    start = datetime.fromisoformat(out.window_end.replace("Z", "+00:00"))
    end = start + timedelta(seconds=out.watch_seconds)
    watch = act.WatchIn(str(action_id), out.verification_id, out.window_end, end.isoformat(), "w1")
    # A regression planted after the hold: a value int() can't read, new to the error tracker.
    clock.offset += timedelta(minutes=2)
    release.out_of_band({"set": {"ORDER_TAX_RATE_BPS": "9.5"}, "revision": release.revision})
    flags, rounds, done = [], 0, False
    while not done:  # each stretch returns at its first new flag; the workflow notifies, goes on
        rounds += 1
        out_w = run(
            v.watch_after_change,
            dataclasses.replace(watch, watch_key=f"w{rounds}", flagged=[f.key for f in flags]),
        )
        assert out_w.flags or out_w.done
        flags += out_w.flags
        done = out_w.done
    assert sorted(f.kind for f in flags) == ["alert_breach", "new_error_group"]
    assert any("9.5" in f.detail for f in flags)
    flagged = events(app_engine, incident_id, "aisre.verification.watch_flagged.v1")
    assert len(flagged) == 2 and {f["action_id"] for f in flagged} == {str(action_id)}
    assert {f["verification_id"] for f in flagged} == {out.verification_id}
    (ended,) = events(app_engine, incident_id, "aisre.verification.watch_ended.v1")
    assert ended["flags"] == 2
    # Nothing rolled back: the action stays executed and the release process saw no undo.
    assert status(app_engine, action_id) == "executed"
    assert [h["kind"] for h in release.history].count("undo") == 0
    assert json.loads(json.dumps(release.state()["config"]))["ORDER_TAX_RATE_BPS"] == "9.5"
