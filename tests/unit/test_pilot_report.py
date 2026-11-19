"""Artifact 13.5 T5.2, T5.3, T5.4 (Artifact 1.6, ADR-65, ADR-75): the pilot measures, on
synthetic records. Each measure is checked against a hand count; the output says "synthetic"
unless the run is production's."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from aisre.pilot import approval_review, report
from aisre.pilot.report import ApprovalRecord, IncidentRecord, Records, Review

T0 = datetime(2026, 12, 1, 9, 0, tzinfo=UTC)
START, END = T0 - timedelta(days=1), T0 + timedelta(days=30)


def inc(n: int, outcome="established", review=None, **kw) -> IncidentRecord:
    at = T0 + timedelta(hours=n)
    return IncidentRecord(
        f"i{n}",
        kw.pop("service", "orders-api"),
        at,
        outcome,
        first_summary_at=at + timedelta(seconds=kw.pop("summary_s", 60)),
        named_cause_at=at + timedelta(seconds=kw.pop("cause_s", 300))
        if outcome == "established"
        else None,
        review=review,
        **kw,
    )


def rev(n: int, confirmed, *, handover=False, category="deployment_config", **kw) -> Review:
    return Review(
        category,
        T0 + timedelta(hours=n, seconds=kw.pop("found_s", 1800)),
        confirmed,
        handover,
        **kw,
    )


RECORDS = Records(
    [
        # 1: proposed, confirmed, change-caused, a fix proposed
        inc(
            1,
            review=rev(1, True, engineer_hours=2.0, fix_reversed=False),
            action_proposed=True,
            cost_micros=400_000,
        ),
        # 2: proposed, refuted, and the review says the platform should have handed over:
        #    a cause named and an action proposed, so under-escalated and false-established
        inc(
            2,
            review=rev(2, False, handover=True, engineer_hours=6.0, fix_reversed=True),
            action_proposed=True,
            cost_micros=600_000,
        ),
        # 3: inconclusive, should have handed over and did: escalated correctly
        inc(
            3,
            "inconclusive",
            review=rev(3, None, handover=True, category="capacity"),
            hand_overs=("no cause established: evidence missing",),
        ),
        # 4: proposed, not reviewed yet: counted as pending, never as confirmed
        inc(
            4,
            cost_micros=200_000,
            hand_overs=("tested fix ready: a person applies it",),
            service="payments-api",
        ),
        # 5: proposed, confirmed, not change-caused
        inc(5, review=rev(5, True, category="dependency"), cause_s=120),
    ],
    approvals=[
        ApprovalRecord(T0, "decided", (("approved", T0 + timedelta(seconds=240)),)),
        ApprovalRecord(T0, "expired"),
    ],
    refusals=["self_approval", "self_approval", "digest_mismatch"],
)


def test_each_pilot_measure_matches_a_hand_count():
    r = report.pilot_report(RECORDS, label="synthetic", start=START, end=END)
    assert (r["incidents"], r["reviewed"], r["proposed_not_reviewed"]) == (5, 4, 1)
    assert (r["accuracy"]["k"], r["accuracy"]["n"]) == (2, 3)  # 1 and 5 of 1, 2, 5
    assert (r["accuracy_change_caused"]["k"], r["accuracy_change_caused"]["n"]) == (1, 2)
    assert (r["false_established"]["k"], r["false_established"]["n"]) == (1, 3)
    assert (r["under_escalation"]["k"], r["under_escalation"]["n"]) == (1, 2)  # 2 of 2, 3
    # platform time to cause counts only causes the review confirmed, change-caused: incident 1
    assert r["time_to_cause"]["platform"] == {"n": 1, "median_s": 300.0, "p95_s": 300.0}
    # responders: every reviewed change-caused incident (1, 2)
    assert r["time_to_cause"]["responders"]["n"] == 2
    assert r["time_to_cause"]["responders"]["median_s"] == 1800.0
    assert r["first_summary"]["n"] == 5 and r["first_summary"]["p95_s"] == 60.0
    assert r["cost"] == {"incidents": 5, "mean_usd": 0.24, "median_usd": 0.2, "total_usd": 1.2}
    assert r["engineer_hours"] == {"n": 2, "median": 4.0}
    assert (r["fixes_reversed"]["k"], r["fixes_reversed"]["n"]) == (1, 2)
    assert r["hand_overs"] == {"no cause established": 1, "tested fix ready": 1}
    assert r["by_service"] == {"orders-api": 4, "payments-api": 1}
    a = r["approvals"]
    assert (a["requests"], a["expired"], a["undecided"]) == (2, 1, 1)
    assert a["decisions"] == {"approved": 1}
    assert a["refusals"] == {"digest_mismatch": 1, "self_approval": 2}
    assert a["time_to_first_decision"]["median_s"] == 240.0


def test_no_records_reports_nothing_measured_never_a_pass():
    r = report.pilot_report(Records([]), label="synthetic", start=START, end=END)
    assert r["accuracy"]["value"] is None and r["accuracy"]["text"] == "not defined (0 of 0)"
    assert r["time_to_cause"]["platform"]["median_s"] is None
    assert "none (0 incidents)" in report.to_markdown(r)


@pytest.mark.parametrize(
    ("environ", "dev_key", "label"),
    [
        ({}, False, "synthetic"),
        ({"AISRE_ENV": "test"}, False, "synthetic"),
        ({"AISRE_ENV": "production"}, True, "synthetic"),
        ({"AISRE_ENV": "production"}, False, "pilot"),
    ],
)
def test_the_output_says_synthetic_unless_it_is_production_pilot_data(environ, dev_key, label):
    records = Records([], [ApprovalRecord(T0, None, (), development_key=dev_key)])
    assert ("pilot" if report.is_pilot(environ, records) else "synthetic") == label
    md = report.to_markdown(report.pilot_report(records, label=label, start=START, end=END))
    assert ("SYNTHETIC" in md) == (label == "synthetic")


def test_the_baseline_from_the_first_thirty_and_from_earlier_records():
    from_pilot = report.from_pilot(RECORDS)
    assert [r.alert_at for r in from_pilot] == [
        RECORDS.incidents[0].alert_at,
        RECORDS.incidents[1].alert_at,
    ]
    b = report.baseline_report(from_pilot, label="synthetic", source="pilot", wanted=30)
    assert b["complete"] is False and b["change_caused_incidents"] == 2
    assert b["time_to_identified_cause"]["median_s"] == 1800.0
    assert b["engineer_hours"]["median"] == 4.0 and b["fixes_reversed"]["k"] == 1
    assert "Incomplete: 30 are needed" in report.to_markdown(b)
    earlier = report.earlier_from_json(
        [
            {
                "alert_at": "2026-05-01T10:00:00+00:00",
                "cause_identified_at": "2026-05-01T11:00:00+00:00",
                "change_caused": True,
                "engineer_hours": 3,
            },
            {
                "alert_at": "2026-05-02T10:00:00+00:00",
                "cause_identified_at": "2026-05-02T10:30:00+00:00",
                "change_caused": False,
            },
        ]
    )
    e = report.baseline_report(earlier, label="synthetic", source="file")
    assert e["change_caused_incidents"] == 1 and e["time_to_identified_cause"]["median_s"] == 3600.0
    assert e["complete"] is True
    assert report.from_pilot(RECORDS, first=1) == from_pilot[:1]


def test_reports_are_markdown_and_json():
    r = report.pilot_report(RECORDS, label="synthetic", start=START, end=END)
    assert json.loads(report.to_json(r))["accuracy"]["k"] == 2
    md = report.to_markdown(r)
    assert md.startswith("# Pilot report (synthetic)") and "| False-established | 1/3" in md


def test_the_approval_review_leaves_the_judgement_to_the_approver():
    item = approval_review.Reviewed(
        approval_request_id="r1",
        action_id="a1",
        incident="INC-2026-000001",
        service="orders-api",
        kind="config_revert",
        description="Revert ORDER_TAX_RATE_BPS",
        risk="low",
        undo_plan="re-apply",
        high_impact=False,
        approvers_needed=1,
        requested_at=T0.isoformat(),
        expires_at=(T0 + timedelta(hours=1)).isoformat(),
        close_reason="decided",
        diff={
            "files_changed": 1,
            "lines_added": 1,
            "lines_removed": 1,
            "patch_sha256": "a" * 64,
            "review_ref": "pr/7",
            "artifact_id": "x",
        },
        test="passed, reproduced",
        diagnosis={"summary": "the config change", "outcome": "established", "confidence": "high"},
        evidence={
            "cited": 3,
            "found": 3,
            "by_kind": {"change": 1, "log": 2},
            "flagged_injection": 1,
            "partial": 0,
            "gaps": 0,
            "contradicting_leading_cause": 1,
        },
        decisions=[
            approval_review.Decision("approved", "approver", T0.isoformat(), 95.0, None, True)
        ],
    )
    rep = approval_review.report([item], label="synthetic", start=START, end=END)
    assert rep["seconds_to_first_decision"] == [95.0]
    md = approval_review.to_markdown(rep)
    assert "SYNTHETIC" in md and "95 s after the request, development key" in md
    assert "flagged as injection 1" in md and "contradicting the leading cause 1" in md
    assert "_not yet recorded_" in md
    assert json.loads(approval_review.to_json(rep))["items"][0]["diff"]["review_ref"] == "pr/7"
