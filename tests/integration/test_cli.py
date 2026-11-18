"""Artifact 13.5 T1.5 (FR-38): the CLI against the API served over real HTTP, on PostgreSQL."""

from __future__ import annotations

import io
import json
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from sqlalchemy import text

from aisre.api.app import create_app
from aisre.api.stream import Timing
from aisre.cli import main
from aisre.record import uuid7
from aisre.record.transitions import Transition, record_transition

ALERT = Path(__file__).parents[1] / "fixtures/reference-incident/alert.json"


@pytest.fixture
def api_url(app_engine, settings):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = create_app(settings, app_engine, stream_timing=Timing(poll_s=0.05))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(5)


def cli(url: str, org, groups: str, *argv: str) -> tuple[int, list[str]]:
    env = {
        "AISRE_API_URL": url,
        "AISRE_DEV_CALLER": "carl",
        "AISRE_DEV_GROUPS": groups,
        "AISRE_DEV_ORG": str(org),
    }
    out = io.StringIO()
    code = main(list(argv), env, out)
    return code, out.getvalue().splitlines()


def test_submit_show_timeline_cancel(api_url, add_service, quiet):
    org = uuid7()
    add_service(org, "orders-api")
    incident_id = quiet(org, "orders-api")  # the relay never starts it (conftest)
    code, out = cli(api_url, org, "aisre:responder", "incident", "submit", str(ALERT))
    made = json.loads(out[0])  # the same alert again: it joins the open incident
    assert code == 0 and made["incident_id"] == incident_id and made["deduplicated"]

    code, out = cli(api_url, org, "aisre:viewer", "incident", "show", incident_id)
    assert code == 0 and json.loads(out[0])["status"] == "open"

    code, out = cli(
        api_url, org, "aisre:responder", "incident", "cancel", incident_id, "--reason", "test run"
    )
    assert code == 0 and "stop_request_id" in json.loads(out[0])

    code, out = cli(api_url, org, "aisre:viewer", "incident", "timeline", incident_id)
    assert [json.loads(line)["type"] for line in out] == [
        "aisre.incident.created.v1",
        "aisre.incident.alert_attached.v1",
        "aisre.incident.cancellation_requested.v1",
    ]

    code, out = cli(
        api_url, org, "aisre:viewer", "incident", "cancel", incident_id, "--reason", "no rights"
    )
    assert code == 1 and out == []


def test_close_sends_the_etag_and_follow_ends_with_the_incident(
    api_url, app_engine, add_service, quiet
):
    org = uuid7()
    add_service(org, "orders-api")
    incident_id = quiet(org, "orders-api")
    for to in ("investigating", "inconclusive", "handed_over"):
        with app_engine.begin() as conn:
            status, version = conn.execute(
                text("SELECT status, version FROM incidents WHERE incident_id = :i"),
                {"i": incident_id},
            ).one()
            record_transition(conn, Transition(incident_id, str(uuid7()), status, to, version, "t"))

    code, out = cli(
        api_url, org, "aisre:incident_lead", "incident", "close", incident_id, "--note", "done"
    )
    assert code == 0 and json.loads(out[0])["version"] == 5

    code, out = cli(api_url, org, "aisre:viewer", "incident", "timeline", incident_id, "--follow")
    assert code == 0 and json.loads(out[-1])["type"] == "aisre.incident.closed.v1"
