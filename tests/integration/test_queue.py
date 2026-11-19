"""T4.3 (Artifact 8.1, 12.5; NFR-16, NFR-17, NFR-38, NFR-39, NFR-44; WR-13): the investigation
queue in PostgreSQL, through the real `acquire_investigation_slot`.

A burst of 60 incidents asks for a slot: at most 5 run, every one of the 60 is recorded and
queued (none dropped), each queued one tells on-call once, and beyond 50 waiting the incident
lead is told and `aisre_incident_queue_depth` crosses AisreQueueDepthHigh's threshold. As slots
free, the next grant goes by severity, then age, except that one service never holds more than
4 of the 5 (NFR-44). Leaving `investigating` deletes the row (WR-13).

The activity is called directly, so the burst takes seconds: the workflow's 15 s SLOT_RETRY is
the only wait it skips. Its incidents are withheld from the Compose relay (`delivered` with no
attempt), and the test deletes its queue rows when it ends, so the stack's own queue is left as
it was.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from conftest import add_catalogue
from sqlalchemy import func, text, update
from temporalio.testing import ActivityEnvironment

from aisre.contracts import IncidentCreate
from aisre.notify.notifier import Notifier
from aisre.record import uuid7
from aisre.record.incidents import create_incident
from aisre.record.tables import outbox
from aisre.record.transitions import Transition, record_transition
from aisre.reliability import telemetry
from aisre.workflow import activities as act
from aisre.workflow.queue_activities import QUEUE_ALERT_AT, QueueActivities

BURST = 60  # NFR-39's test size; NFR-16 asks for 20


@pytest.fixture
def org(app_engine):
    """A fresh organisation with two services, `noisy` and `calm`."""
    org_id = uuid7()
    add_catalogue(app_engine, org_id, "noisy")
    with app_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO services (service_id, org_id, slug, display_name, owner_group) "
                "VALUES (:i, :o, 'calm', 'calm', 'test-owners')"
            ),
            {"i": uuid7(), "o": org_id},
        )
    return org_id


@pytest.fixture
def opened(app_engine, org):
    made: list[str] = []

    def open_one(service: str, severity: str | None) -> str:
        n = len(made)
        body = IncidentCreate(
            service=service,
            environment="production",
            started_at=datetime.now(UTC),
            title=f"queue burst {n}",
            source="monitoring",
            fingerprint=f"{service}:burst-{n}-{uuid7()}",
        )
        with app_engine.begin() as conn:
            iid = create_incident(  # no grouping: each alert of the burst is its own incident
                conn,
                org_id=org,
                caller_id="dev:monitoring",
                body=body,
                grouping_window=timedelta(0),
            )
            assert not iid.deduplicated
            conn.execute(  # withheld from the relay, as the `quiet` fixture does
                update(outbox)
                .where(outbox.c.incident_id == iid.incident_id)
                .values(delivered_at=func.now())
            )
            if severity:
                conn.execute(
                    text(
                        "UPDATE incidents SET proposed_severity = CAST(:s AS severity), "
                        "version = version + 1 WHERE incident_id = :i"
                    ),
                    {"s": severity, "i": iid.incident_id},
                )
        made.append(str(iid.incident_id))
        return str(iid.incident_id)

    yield open_one
    with app_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM investigation_queue WHERE incident_id = ANY(CAST(:i AS uuid[]))"),
            {"i": made},
        )


def ask(acts: QueueActivities, iid: str) -> bool:
    """One attempt; True if granted."""
    try:
        asyncio.run(
            ActivityEnvironment().run(
                acts.acquire_investigation_slot,
                act.SlotIn(iid, f"incident-{iid}:slot", f"incident-{iid}"),
            )
        )
    except act.NoSlot:
        return False
    return True


def rows(app_engine, ids: list[str]) -> dict[str, bool]:
    with app_engine.connect() as conn:
        return {
            str(i): g is not None
            for i, g in conn.execute(
                text(
                    "SELECT incident_id, granted_at FROM investigation_queue "
                    "WHERE incident_id = ANY(CAST(:i AS uuid[]))"
                ),
                {"i": ids},
            )
        }


def notified(app_engine, ids: list[str], kind: str) -> list[str]:
    with app_engine.connect() as conn:
        return [
            str(i)
            for (i,) in conn.execute(
                text(
                    "SELECT incident_id FROM notifications WHERE kind = :k "
                    "AND incident_id = ANY(CAST(:i AS uuid[]))"
                ),
                {"k": kind, "i": ids},
            )
        ]


def free(app_engine, iid: str) -> None:
    """The incident leaves `investigating`: record_transition deletes its row (WR-13)."""
    with app_engine.begin() as conn:
        for to in ("investigating", "inconclusive"):
            status, version = conn.execute(
                text("SELECT status, version FROM incidents WHERE incident_id = :i"),
                {"i": UUID(iid)},
            ).one()
            record_transition(conn, Transition(iid, str(uuid7()), status, to, version, "test"))


def test_a_burst_of_60_is_queued_by_severity_and_none_dropped(app_engine, opened):
    acts = QueueActivities(app_engine, Notifier(app_engine))
    with app_engine.connect() as conn:
        others = conn.execute(text("SELECT count(*) FROM investigation_queue")).scalar_one()
    if others:
        pytest.skip(f"the stack's queue holds {others} rows; this test needs it empty")
    # Five noisy sev1s first, so NFR-44 shows: the fifth waits while a slot is free.
    noisy = [opened("noisy", "sev1") for _ in range(5)]
    assert [ask(acts, i) for i in noisy] == [True] * 4 + [False]
    calm = [opened("calm", sev) for sev in ("sev4", "sev2", "sev1", None)]
    assert ask(acts, calm[0])  # the free fifth slot goes to the other service
    later = [opened("calm", ("sev3", "sev2", "sev4")[n % 3]) for n in range(BURST - len(noisy) - 1)]
    ids = noisy + [calm[0]] + later
    waiting = [i for i in ids if not ask(acts, i)]
    for i in calm[1:]:  # extra incidents beyond the 60, for the order check below
        assert not ask(acts, i)
    held = rows(app_engine, ids)
    assert len(held) == BURST  # every incident recorded and queued: none dropped (NFR-16)
    assert sum(held.values()) == 5  # never more than 5 running (NFR-38)
    assert len(waiting) == BURST - 5  # the waiting noisy sev1 included
    # NFR-17: each queued incident told on-call, once, however often it asked.
    assert sorted(notified(app_engine, ids, "queued")) == sorted(waiting)
    for i in waiting[:3]:
        ask(acts, i)
    assert len(notified(app_engine, ids, "queued")) == len(waiting)
    # NFR-39: beyond 50 waiting, the incident lead is told, and the gauge crosses the alert.
    overflow = notified(app_engine, ids + calm[1:], "queue_overflow")
    assert overflow and len(overflow) == len(waiting) + len(calm[1:]) - QUEUE_ALERT_AT
    depth = sum(
        telemetry.REGISTRY.get_sample_value("aisre_incident_queue_depth", {"severity": s}) or 0
        for s in ("sev1", "sev2", "sev3", "sev4", "none")
    )
    assert depth > QUEUE_ALERT_AT

    # A noisy slot frees: the noisy sev1 that waited is the most severe, oldest: it goes next.
    free(app_engine, noisy[0])
    assert noisy[0] not in rows(app_engine, ids)  # WR-13: the row went with the transition
    granted = [i for i in reversed(waiting + calm[1:]) if ask(acts, i)]
    assert granted == [noisy[4]]
    # The calm sev4 frees: the calm sev1 (most severe left) goes, ahead of older sev2s and sev3s.
    free(app_engine, calm[0])
    granted = [i for i in waiting + calm[1:] if i != noisy[4] and ask(acts, i)]
    assert granted == [calm[2]]
