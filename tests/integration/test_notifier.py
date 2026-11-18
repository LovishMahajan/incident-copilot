"""Artifact 13.5 T1.19 (NFR-09): the Notifier against PostgreSQL, as aisre_app.

Dead letters reach the incident lead in ID order, once each, with the cursor stored; an
incident's first message observes aisre_time_to_first_notification_seconds; a repeated key is
dropped; a failed send is retried."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from aisre.contracts import IncidentCreate
from aisre.notify.channels import Notification
from aisre.notify.notifier import DEAD_LETTERS, Notifier
from aisre.record import create_incident, uuid7
from aisre.record.dead_letters import add_dead_letter
from aisre.record.notifications import consumer_cursors, notifications
from aisre.reliability import telemetry
from aisre.workflow import activities as act


class Recorder:
    name = "test"

    def __init__(self) -> None:
        self.sent: list[Notification] = []
        self.fail = 0

    def send(self, n: Notification) -> None:
        if self.fail:
            self.fail -= 1
            raise ConnectionError("channel down")
        self.sent.append(n)


def first_notifications() -> float:
    return (
        telemetry.REGISTRY.get_sample_value("aisre_time_to_first_notification_seconds_count") or 0
    )


def drain(notifier: Notifier) -> None:
    while notifier.dead_letter_pass():
        pass


def test_dead_letters_reach_the_incident_lead_once_each_in_order(app_engine):
    """The Compose worker's own Notifier may take some rows first: either way, each dead
    letter gets one notification row, and this one sends the rest in ID order."""
    channel = Recorder()
    notifier = Notifier(app_engine, channel, settle=timedelta(0))
    drain(notifier)  # whatever earlier tests left
    channel.sent.clear()
    org = uuid7()
    with app_engine.begin() as conn:
        ids = [
            add_dead_letter(
                conn,
                org_id=org,
                caller_id="dev:mon",
                reason="unknown_service",
                raw_payload={"n": n},
            )
            for n in range(3)
        ]
    drain(notifier)
    mine = [n.dead_letter_id for n in channel.sent if n.dead_letter_id in {str(i) for i in ids}]
    assert mine == sorted(mine)
    assert {tuple(n.audience) for n in channel.sent} <= {("incident_lead",)}
    with app_engine.connect() as conn:
        rows = conn.execute(
            select(notifications.c.dead_letter_id, notifications.c.kind).where(
                notifications.c.dead_letter_id.in_(ids)
            )
        ).all()
        cursor = conn.execute(
            select(consumer_cursors.c.position).where(consumer_cursors.c.consumer == DEAD_LETTERS)
        ).scalar_one()
    assert sorted(r[0] for r in rows) == sorted(ids) and {r[1] for r in rows} == {"dead_letter"}
    assert cursor >= str(max(ids))
    assert notifier.dead_letter_pass() == 0


def test_a_failed_send_leaves_no_record_and_is_sent_on_retry(app_engine, catalogue):
    org, service, env = catalogue
    incident_id = opened(app_engine, org, service, env)
    channel = Recorder()
    notifier = Notifier(app_engine, channel)
    n = Notification(
        key=f"{incident_id}:retry",
        kind="opened",
        audience=["on_call"],
        incident_id=str(incident_id),
    )
    channel.fail = 1
    with pytest.raises(ConnectionError):
        notifier.send(n)
    assert notifier.send(n) is True and notifier.send(n) is False
    assert [x.key for x in channel.sent] == [n.key]


def opened(engine, org, service, env):
    body = IncidentCreate(
        service=service,
        environment=env,
        started_at=datetime.now(UTC),
        title="Notifier test",
        source="engineer",
    )
    with engine.begin() as conn:
        return create_incident(conn, org_id=org, caller_id="dev:t", body=body).incident_id


def test_notify_sends_once_per_key_and_times_the_first(app_engine, catalogue):
    org, service, env = catalogue
    incident_id = opened(app_engine, org, service, env)
    channel = Recorder()
    notifier = Notifier(app_engine, channel)
    before = first_notifications()
    inp = act.NotifyIn(str(incident_id), f"{incident_id}:n1", "opened", ["on_call"])
    asyncio.run(notifier.notify(inp))
    asyncio.run(notifier.notify(inp))  # a retried activity
    asyncio.run(
        notifier.notify(act.NotifyIn(str(incident_id), f"{incident_id}:n2", "summary", ["on_call"]))
    )
    assert [n.kind for n in channel.sent] == ["opened", "summary"]
    # Only the first message for the incident is timed. (With the Compose worker running, its
    # own `opened` for this incident may have been the first.)
    assert first_notifications() - before in (0, 1)
    with app_engine.connect() as conn:
        kinds = conn.execute(
            select(notifications.c.kind).where(notifications.c.incident_id == incident_id)
        ).scalars()
        assert sorted(kinds) == ["opened", "summary"]
