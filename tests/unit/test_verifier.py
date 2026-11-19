"""Artifact 13.5 T3.8, T3.17 (FR-30, FR-50, Artifact 11.12): the verifier's plan, window and
watch rules, pure. The activities run against PostgreSQL in tests/integration/test_verifier.py."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from aisre.agent.skills.patching import RECOVERY
from aisre.verification.verification import (
    Verdict,
    criteria_digest,
    plan_from_criteria,
    samples_from_rows,
    verify,
    watch_flags,
    window_after,
)

T = datetime(2026, 11, 19, 12, 0, 30, tzinfo=UTC)


def rows(rates, start=datetime(2026, 11, 19, 12, 1, tzinfo=UTC), requests=1200):
    return [
        {
            "minute_utc": (start + timedelta(minutes=i)).isoformat().replace("+00:00", "Z"),
            "requests_total": requests,
            "rate_500_total": r,
        }
        for i, r in enumerate(rates)
    ]


def test_fr30_the_digest_is_the_one_the_record_signs():
    text = json.dumps(RECOVERY, sort_keys=True, separators=(",", ":"))
    assert criteria_digest(RECOVERY) == hashlib.sha256(text.encode()).hexdigest()
    other = [dict(RECOVERY[0], threshold=0.5)]
    assert criteria_digest(other) != criteria_digest(RECOVERY)


def test_fr50_the_window_starts_at_the_next_whole_minute_and_lasts_the_hold():
    start, end = window_after(T, 600)
    assert start == datetime(2026, 11, 19, 12, 1, tzinfo=UTC) and end - start == timedelta(
        seconds=600
    )
    exact = T.replace(second=0)
    assert window_after(exact, 60)[0] == exact


def test_fr50_no_verdict_of_verified_before_every_minute_of_the_hold_has_ended():
    plan = plan_from_criteria("orders-api", RECOVERY, 600)
    start, end = window_after(T, 600)
    healthy = rows([0.0008] * 10)
    for minutes in range(10):
        now = start + timedelta(minutes=minutes, seconds=30)
        assert verify(plan, samples_from_rows(healthy, start, end, now)).verdict != Verdict.VERIFIED
    assert verify(plan, samples_from_rows(healthy, start, end, end)).verdict == Verdict.VERIFIED


def test_one_bad_minute_fails_at_once_and_a_quiet_minute_is_not_held():
    plan = plan_from_criteria("orders-api", RECOVERY, 600)
    start, end = window_after(T, 600)
    early = start + timedelta(minutes=2)
    bad = verify(plan, samples_from_rows(rows([0.0008, 0.61]), start, end, early))
    assert bad.verdict == Verdict.FAILED
    quiet = rows([0.0008] * 10, requests=10)  # under 50 requests a minute: can't judge
    assert verify(plan, samples_from_rows(quiet, start, end, end)).verdict == Verdict.INCONCLUSIVE


def test_a_metric_monitoring_does_not_serve_is_never_held():
    plan = plan_from_criteria(
        "orders-api", [{"metric": "http_latency_p95_ms", "comparison": "<=", "threshold": 300}], 60
    )
    start, end = window_after(T, 60)
    res = verify(plan, samples_from_rows(rows([0.0]), start, end, end))
    assert res.verdict == Verdict.INCONCLUSIVE


def test_t317_the_watch_flags_a_breach_and_a_group_unseen_before_the_change():
    plan = plan_from_criteria("orders-api", RECOVERY, 600)
    start = datetime(2026, 11, 19, 12, 11, tzinfo=UTC)
    samples = samples_from_rows(rows([0.0008, 0.61], start), start, start + timedelta(hours=1),
                                start + timedelta(minutes=3))  # fmt: skip
    groups = [
        {"type": "ValueError", "message": "x '8.25'", "count": 3},
        {"type": "ValueError", "message": "x '9.5'", "count": 5},
    ]
    flags = watch_flags(plan, samples, groups, {"ValueError: x '8.25'"})
    assert [f.kind for f in flags] == ["alert_breach", "new_error_group"]
    assert "9.5" in flags[1].detail and len({f.key for f in flags}) == 2
    assert watch_flags(plan, samples[:1], groups[:1], {"ValueError: x '8.25'"}) == []
