"""Artifact 13.5 T1.6 (FR-33, FR-51): notes, cancellation, severity confirmation, closure and
re-investigation, against PostgreSQL as aisre_app. The workflow isn't running: each test
reads what the API wrote, the outbox rows the relay would deliver included."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from aisre.api.app import create_app
from aisre.api.auth import DEV_CALLER_HEADER, DEV_GROUPS_HEADER, DEV_ORG_HEADER
from aisre.record.tables import outbox
from aisre.record.transitions import Transition, record_transition

ALERT = json.loads(
    (Path(__file__).parents[1] / "fixtures/reference-incident/alert.json").read_text()
)
LEAD, RESPONDER, VIEWER = "aisre:incident_lead", "aisre:responder", "aisre:viewer"


@pytest.fixture
def api(app_engine, settings):
    with TestClient(create_app(settings, app_engine)) as c:
        yield c


class Incident:
    def __init__(self, api: TestClient, engine, org, service: str, incident_id: str) -> None:
        self.api, self.engine, self.org, self.service, self.id = (
            api,
            engine,
            org,
            service,
            incident_id,
        )
        self.n = 0

    def post(
        self,
        route: str,
        body: dict,
        *,
        groups: str = LEAD,
        caller: str = "lena",
        key: str | None = None,
        if_match: str | None = None,
    ):
        self.n += 1
        h = {DEV_CALLER_HEADER: caller, DEV_ORG_HEADER: str(self.org), DEV_GROUPS_HEADER: groups}
        h["Idempotency-Key"] = key or f"cmd-{self.n:04d}-{self.id[-12:]}"
        if if_match is not None:
            h["If-Match"] = if_match
        return self.api.post(f"/v1/incidents/{self.id}/{route}", json=body, headers=h)

    def row(self):
        with self.engine.connect() as conn:
            return conn.execute(
                text("SELECT * FROM incidents WHERE incident_id = :i"), {"i": self.id}
            ).one()

    def events(self) -> list[dict]:
        r = self.api.get(
            f"/v1/incidents/{self.id}/timeline",
            headers={DEV_CALLER_HEADER: "lena", DEV_ORG_HEADER: str(self.org)},
        )
        return r.json()["items"]

    def outbox(self):
        with self.engine.connect() as conn:
            return conn.execute(
                select(outbox).where(outbox.c.incident_id == self.id).order_by(outbox.c.outbox_id)
            ).all()

    def move(self, *path: str) -> None:
        """Walk the incident through `path` as the workflow would."""
        for to in path:
            row = self.row()
            with self.engine.begin() as conn:
                record_transition(
                    conn,
                    Transition(
                        self.id,
                        f"t-{self.id}-{to}-{row.version}",
                        row.status,
                        to,
                        row.version,
                        "test",
                    ),
                )


@pytest.fixture
def incident(api, app_engine, catalogue) -> Incident:
    org, service, _ = catalogue
    r = api.post(
        "/v1/incidents",
        json=ALERT["incident_create"] | {"service": service},
        headers={
            DEV_CALLER_HEADER: "monitoring",
            DEV_ORG_HEADER: str(org),
            DEV_GROUPS_HEADER: "aisre:monitoring",
            "Idempotency-Key": f"cmd-open-{org.hex[-12:]}",
        },
    )
    assert r.status_code == 201, r.text
    return Incident(api, app_engine, org, service, r.json()["incident_id"])


def test_a_note_is_an_event_with_its_author(incident):
    r = incident.post("notes", {"note": "Rolled back the canary by hand."}, groups=RESPONDER)
    assert r.status_code == 201, r.text
    last = incident.events()[-1]
    assert last["type"] == "aisre.incident.note_added.v1" and last["actor"] == "dev:lena"
    assert last["data"] == {"note": "Rolled back the canary by hand."}
    assert r.json()["sequence"] == last["sequence"] and incident.row().version == 1


def test_cancel_writes_a_stop_an_event_and_a_signal(incident, app_engine):
    r = incident.post("cancellation", {"reason": "false alarm"}, groups=RESPONDER)
    assert r.status_code == 202, r.text
    stop_id = r.json()["stop_request_id"]
    with app_engine.connect() as conn:
        stop = conn.execute(
            text("SELECT requested_by, reason FROM stop_requests WHERE stop_request_id = :s"),
            {"s": stop_id},
        ).one()
    assert tuple(stop) == ("dev:lena", "false alarm")
    event = incident.events()[-1]
    assert event["type"] == "aisre.incident.cancellation_requested.v1"
    assert event["causationid"] == stop_id
    signal = incident.outbox()[-1]
    assert (signal.kind, signal.workflow_id) == ("signal_workflow", f"incident-{incident.id}")
    assert signal.payload == {
        "signal": "cancellation_requested",
        "arg": {"request_id": stop_id, "requested_by": "dev:lena", "reason": "false alarm"},
    }
    # The incident itself waits for the workflow: still open, same version.
    assert incident.row().status == "open" and incident.row().version == 1


def test_the_same_key_replays_and_another_body_conflicts(incident):
    key = f"same-key-{incident.id[-12:]}"
    first = incident.post("cancellation", {"reason": "false alarm"}, key=key)
    again = incident.post("cancellation", {"reason": "false alarm"}, key=key)
    assert again.status_code == 202 and again.json() == first.json()
    assert len([e for e in incident.events() if "cancellation" in e["type"]]) == 1
    other = incident.post("cancellation", {"reason": "another reason"}, key=key)
    assert other.status_code == 409 and other.json()["code"] == "idempotency_key_reused"


def test_a_finished_incident_cannot_be_cancelled(incident):
    incident.move("investigating", "inconclusive", "handed_over")
    r = incident.post("cancellation", {"reason": "too late"})
    assert r.status_code == 409 and r.json()["code"] == "not_cancellable"


def test_severity_needs_if_match_and_the_current_version(incident):
    missing = incident.post("severity-confirmation", {"severity": "sev2"})
    assert missing.status_code == 428 and missing.json()["code"] == "precondition_required"
    stale = incident.post("severity-confirmation", {"severity": "sev2"}, if_match='"7"')
    assert stale.status_code == 412 and stale.headers["ETag"] == '"1"'
    ok = incident.post("severity-confirmation", {"severity": "sev2"}, if_match='"1"')
    assert ok.status_code == 200, ok.text
    assert ok.headers["ETag"] == '"2"' and ok.json()["version"] == 2
    row = incident.row()
    assert (row.confirmed_severity, row.severity_confirmed_by) == ("sev2", "dev:lena")
    assert incident.events()[-1]["data"] == {
        "confirmed_severity": "sev2",
        "severity_confirmed_by": "dev:lena",
    }
    assert incident.outbox()[-1].payload["arg"]["kind"] == "confirm_severity"
    again = incident.post("severity-confirmation", {"severity": "sev1"}, if_match='"1"')
    assert again.status_code == 412


def test_only_the_incident_lead_confirms_or_closes(incident, caplog):
    with caplog.at_level("INFO", logger="aisre.audit"):
        r = incident.post(
            "severity-confirmation", {"severity": "sev2"}, groups=RESPONDER, if_match="1"
        )
    assert r.status_code == 403 and r.json()["code"] == "forbidden"
    lead_elsewhere = f"{LEAD}:other-svc,{VIEWER}"
    r = incident.post("closure", {}, groups=lead_elsewhere, if_match="1")
    assert r.status_code == 403  # can read here, but leads another service
    r = incident.post("closure", {}, groups=f"{LEAD}:other-svc", if_match="1")
    assert r.status_code == 404  # can't even read it: the same answer as a missing incident


def test_close_from_handed_over_ends_the_run(incident):
    incident.move("investigating", "inconclusive", "handed_over")
    version = incident.row().version
    early = incident.post("closure", {}, if_match=str(version - 1))
    assert early.status_code == 412
    r = incident.post("closure", {"note": "Fixed by hand"}, if_match=str(version))
    assert r.status_code == 200, r.text
    row = incident.row()
    assert row.status == "closed" and row.closed_at is not None and row.version == version + 1
    changed, closed = incident.events()[-2:]
    assert changed["data"]["to_status"] == "closed" and changed["actor"] == "dev:lena"
    assert closed["type"] == "aisre.incident.closed.v1"
    assert (
        closed["data"]["from_status"] == "handed_over" and closed["data"]["note"] == "Fixed by hand"
    )
    assert incident.outbox()[-1].payload["arg"]["kind"] == "close"
    again = incident.post("closure", {}, if_match=str(version + 1))
    assert again.status_code == 409 and again.json()["code"] == "not_closable"


def test_an_open_incident_cannot_be_closed(incident):
    r = incident.post("closure", {}, if_match="1")
    assert r.status_code == 409 and r.json()["code"] == "not_closable"


def test_reinvestigation_starts_run_two_only_from_handed_over(incident):
    reason = {"reason": "New evidence from the payments team"}
    early = incident.post("reinvestigations", reason, groups=RESPONDER, if_match="1")
    assert early.status_code == 409 and early.json()["code"] == "not_handed_over"

    incident.move("investigating", "inconclusive", "handed_over")
    version = incident.row().version
    r = incident.post("reinvestigations", reason, groups=RESPONDER, if_match=str(version))
    assert r.status_code == 202, r.text
    assert r.json()["investigation_run"] == 2
    assert r.json()["workflow_id"] == f"incident-{incident.id}-r2"
    signal, start = incident.outbox()[-2:]
    assert signal.workflow_id == f"incident-{incident.id}"  # the run that handed over ends
    assert signal.payload["arg"]["kind"] == "reinvestigate"
    assert (start.kind, start.workflow_id) == ("start_workflow", f"incident-{incident.id}-r2")
    assert start.payload == {"incident_id": incident.id, "run": 2}
    assert incident.events()[-1]["data"]["investigation_run"] == 2

    twice = incident.post("reinvestigations", reason, groups=RESPONDER, if_match=str(version))
    assert twice.status_code == 409 and twice.json()["code"] == "reinvestigation_requested"

    # The new run moves the incident, and the database raises investigation_run (ADR-46).
    incident.move("investigating")
    assert incident.row().investigation_run == 2
    cancel = incident.post("cancellation", {"reason": "duplicate of INC-1"})
    assert cancel.json() and incident.outbox()[-1].workflow_id == f"incident-{incident.id}-r2"


def test_a_viewer_is_refused_every_write(incident):
    for route, body, if_match in [
        ("notes", {"note": "hi"}, None),
        ("cancellation", {"reason": "false alarm"}, None),
        ("reinvestigations", {"reason": "please look again"}, "1"),
    ]:
        r = incident.post(route, body, groups=VIEWER, if_match=if_match)
        assert r.status_code == 403, route
