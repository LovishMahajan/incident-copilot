"""T4.2 (Artifact 12.5; NFR-05, NFR-06, NFR-41): an investigation caught mid-step by a PostgreSQL
outage resumes when the database returns, on the Compose stack.

`fault`: it restarts the worker with scripted skills slowed to 10 s a step (as
test_worker_kills.py), posts an alert, and once the incident is `investigating` stops PostgreSQL
for OUTAGE_S. Every activity that touches the record fails while it is down and is retried by
Temporal (its state is in Temporal's own database on the same server, so Temporal waits too);
nothing is written from memory. When PostgreSQL is back the same workflow goes on from the step
it was in: the incident still reaches `handed_over`, the first progress comes within NFR-03's 2
minutes of the database answering, and no transition, hand-over or notification is recorded
twice. The stack goes back to .env.example's settings at the end.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Iterator

import pytest
import stack
from sqlalchemy import text
from test_alert_faults import post_alert, seeded_service, status_of
from test_worker_kills import SLOW, last_seq

pytestmark = pytest.mark.fault

OUTAGE_S = 60
RESUME_S = 120  # NFR-03


@pytest.fixture
def slow_worker() -> Iterator[None]:
    why = stack.available()
    if why:
        pytest.skip(why)
    try:
        stack.start("worker", overrides=SLOW)
        yield
    finally:
        stack.restore()


def test_an_investigation_stuck_by_a_database_outage_resumes_when_it_returns(
    slow_worker, app_engine
):
    service = seeded_service()
    code, body = post_alert(service)
    assert code == 201, body
    iid = str(body["incident_id"])  # type: ignore[index]
    stack.wait_for(lambda: status_of(iid) == "investigating", 120, "investigating", 1)
    before = last_seq(app_engine, iid)
    stack.stop("postgres")
    try:
        time.sleep(OUTAGE_S)  # activities fail and are retried meanwhile
    finally:
        stack.start("postgres")
    back = time.monotonic()
    stack.restore()  # Temporal keeps its state on the same server: it comes back with it

    def progressed() -> bool:
        try:
            return last_seq(app_engine, iid) > before
        except Exception:  # noqa: BLE001 (the pool's connections died with the server)
            app_engine.dispose()
            return False

    stack.wait_for(progressed, RESUME_S, "progress after the outage", 2)
    resumed = time.monotonic() - back
    stack.wait_for(lambda: status_of(iid) == "handed_over", 900, "the hand-over", 5)
    print(f"stopped at seq {before}; first progress {resumed:.0f} s after PostgreSQL returned")
    assert resumed <= RESUME_S

    with app_engine.connect() as conn:
        events = conn.execute(
            text("SELECT type, data FROM timeline_events WHERE incident_id = :i ORDER BY seq"),
            {"i": iid},
        ).all()
        notes = dict(
            conn.execute(
                text(
                    "SELECT kind, count(*) FROM notifications WHERE incident_id = :i GROUP BY kind"
                ),
                {"i": iid},
            ).all()
        )
    moves = Counter(
        (e.data["from_status"], e.data["to_status"])
        for e in events
        if e.type == "aisre.incident.status_changed.v1"
    )
    assert moves and max(moves.values()) == 1, moves
    assert sum(e.type == "aisre.incident.handed_over.v1" for e in events) == 1
    assert notes.get("opened") == 1 and notes.get("handed_over", 1) == 1, notes
