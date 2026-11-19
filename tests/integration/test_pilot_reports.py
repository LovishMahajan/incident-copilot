"""Artifact 13.5 T5.2 to T5.4 (ADR-75, migration 0013), against PostgreSQL: the incident review
is recorded once by the Incident lead, and the reports read it as aisre_reporting, which can
write nothing. Needs AISRE_REPORTING_DATABASE_URL; a volume made before 0013 needs the role's
password set (README, Pilot readiness)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy.exc
import test_api_commands as commands
from sqlalchemy import text
from test_api_commands import RESPONDER, Incident

from aisre.pilot import approval_review, report
from aisre.record import make_engine, uuid7

api, incident = commands.api, commands.incident  # the fixtures, shared


@pytest.fixture(scope="module")
def reporting(settings):
    if settings.reporting_database_url is None:
        pytest.skip("AISRE_REPORTING_DATABASE_URL is not set")
    engine = make_engine(settings.reporting_database_url)
    try:
        with engine.connect():
            pass
    except sqlalchemy.exc.OperationalError:
        pytest.skip("aisre_reporting can't sign in: set its password (README, Pilot readiness)")
    yield engine
    engine.dispose()


def diagnose(incident: Incident, outcome: str) -> None:
    h, d = uuid7(), uuid7()
    with incident.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO hypotheses (hypothesis_id, incident_id, ordinal, statement, category,"
                " confirm_test, refute_test, confidence) VALUES (:h, :i, 1, 'Tax rate config',"
                " 'deployment_config', 'revert clears', 'still fails', 'high')"
            ),
            {"h": h, "i": incident.id},
        )
        conn.execute(
            text(
                "INSERT INTO diagnoses (diagnosis_id, incident_id, revision, outcome,"
                " leading_hypothesis_id, category, confidence, summary, ruled_out, produced_by)"
                " VALUES (:d, :i, 1, CAST(:o AS diagnosis_outcome), :h, 'deployment_config',"
                " CAST(:c AS confidence), 'Tax rate', '{}', 'diagnosis-engine')"
            ),
            {
                "d": d,
                "i": incident.id,
                "o": outcome,
                "h": h if outcome == "established" else None,
                "c": "high" if outcome == "established" else "low",
            },
        )
    incident.move("investigating", "diagnosed" if outcome == "established" else "inconclusive")
    incident.move("handed_over")


def review(**kw):
    return {
        "cause": "ORDER_TAX_RATE_BPS set to 8.25 in release 2.14.0",
        "cause_category": "deployment_config",
        "cause_identified_at": (datetime.now(UTC) + timedelta(minutes=20)).isoformat(),
        "should_have_handed_over": False,
        "engineer_hours": 1.5,
        **kw,
    }


def test_the_incident_lead_records_the_review_once(incident, app_engine):
    early = incident.post("incident-review", review(proposed_cause_confirmed=True))
    assert early.status_code == 409 and early.json()["code"] == "not_reviewable"
    diagnose(incident, "established")
    r = incident.post("incident-review", review(proposed_cause_confirmed=True), groups=RESPONDER)
    assert r.status_code == 403
    missing = incident.post("incident-review", review())
    assert missing.status_code == 409 and missing.json()["code"] == "proposed_cause"
    ok = incident.post("incident-review", review(proposed_cause_confirmed=True))
    assert ok.status_code == 201, ok.text
    note = incident.events()[-1]
    assert note["type"] == "aisre.incident.note_added.v1" and note["actor"] == "dev:lena"
    assert note["data"]["note"] == "Incident review recorded: the proposed cause is confirmed."
    again = incident.post("incident-review", review(proposed_cause_confirmed=False))
    assert again.status_code == 409 and again.json()["code"] == "already_reviewed"
    with app_engine.connect() as conn:
        row = conn.execute(
            text("SELECT * FROM incident_reviews WHERE incident_id = :i"), {"i": incident.id}
        ).one()
    assert row.reviewed_by == "dev:lena" and row.proposed_cause_confirmed is True
    assert row.diagnosis_id is not None and float(row.engineer_hours) == 1.5
    with pytest.raises(sqlalchemy.exc.ProgrammingError, match="permission denied"):
        with app_engine.begin() as conn:
            conn.execute(
                text("DELETE FROM incident_reviews WHERE incident_id = :i"), {"i": incident.id}
            )


def test_no_proposed_cause_means_nothing_to_confirm(incident):
    diagnose(incident, "inconclusive")
    r = incident.post("incident-review", review(proposed_cause_confirmed=True))
    assert r.status_code == 409 and r.json()["code"] == "proposed_cause"
    ok = incident.post("incident-review", review(should_have_handed_over=True))
    assert ok.status_code == 201, ok.text
    assert incident.events()[-1]["data"]["note"].endswith("no cause was proposed.")


def test_the_reports_read_the_review_as_the_reporting_role(incident, reporting, app_engine):
    diagnose(incident, "established")
    assert (
        incident.post("incident-review", review(proposed_cause_confirmed=False)).status_code == 201
    )
    start, end = datetime.now(UTC) - timedelta(hours=1), datetime.now(UTC) + timedelta(hours=1)
    with reporting.connect() as conn:
        records = report.read(conn, start, end)
        reviewed = approval_review.read(conn, start, end)
        with pytest.raises(sqlalchemy.exc.ProgrammingError, match="permission denied"):
            conn.execute(text("UPDATE incident_reviews SET fix_reversed = true"))
    (mine,) = [i for i in records.incidents if i.incident_id == incident.id]
    assert mine.named and mine.review is not None
    assert mine.review.proposed_cause_confirmed is False and mine.review.change_caused
    assert mine.hand_overs == () and mine.service == incident.service
    assert isinstance(reviewed, list)
    label = "pilot" if report.is_pilot({"AISRE_ENV": "test"}, records) else "synthetic"
    md = report.to_markdown(report.pilot_report(records, label=label, start=start, end=end))
    assert label == "synthetic" and "SYNTHETIC" in md
    with app_engine.connect() as conn, pytest.raises(report.NotReadOnly):
        report.read(conn, start, end)
