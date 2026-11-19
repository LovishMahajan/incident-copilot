"""T4.1 (Artifact 13.7 "Worker crash"; NFR-02, NFR-03, NFR-04, NFR-40): the Compose worker killed
with SIGKILL at each step of an incident, and twice in a row, on the running stack.

`fault`: it restarts the worker with scripted skills, each step taking 10 s (the scripts'
maximum, README's M0 demo uses 3), so a kill lands mid-step; the evidence still comes from the
fake monitoring source. After every timeline event (each step recorded) the worker is killed
with SIGKILL and started again, up to 8 times; the first kill is two in a row. After each
restart the next timeline event must come within 2 minutes (NFR-03; heartbeat loss is 60 s,
NFR-40). The incident must still reach `handed_over` (NFR-02), with no transition, hand-over or
notification twice (NFR-04). The worker goes back to its .env.example settings at the end.

The dispatcher's kills are test_dispatcher_kills.py, a real dispatcher process against
PostgreSQL; the approval path on the stack needs a person and the Test Runner (ADR-71).
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Iterator

import pytest
import stack
from sqlalchemy import text
from test_alert_faults import post_alert, seeded_service, status_of

pytestmark = pytest.mark.fault

SLOW = {
    "AISRE_SCRIPTED_STEP_SECONDS": "10",  # scripted.MAX_STEP_SECONDS
    "AISRE_PROVIDER_API_KEY": "",
    "AISRE_REVIEW_ADAPTER": "",  # the review settings go together (Settings)
    "AISRE_REVIEW_URL": "",
    "AISRE_REVIEW_TOKEN": "",
}
RESUME_S = 120  # NFR-03
MAX_KILLS = 8


@pytest.fixture
def slow_worker() -> Iterator[None]:
    why = stack.available()
    if why:
        pytest.skip(why)
    try:
        stack.start("worker", overrides=SLOW)
        yield
    finally:
        stack.restore()  # recreates the worker with .env.example's settings


def last_seq(app_engine, iid: str) -> int:
    with app_engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT last_event_seq FROM incidents WHERE incident_id = :i"), {"i": iid}
            ).scalar_one()
        )


def crash(times: int = 1) -> float:
    """SIGKILL the worker `times` times in a row, each followed by a start; returns the time of
    the last start (not waiting for health: recovery is what is measured)."""
    for _ in range(times):
        stack.kill("worker")
        stack.compose("start", "worker")
    return time.monotonic()


def test_kill_the_worker_at_each_step_and_twice_in_a_row(slow_worker, app_engine):
    service = seeded_service()
    code, body = post_alert(service)
    assert code == 201, body
    iid = str(body["incident_id"])  # type: ignore[index]
    resumed: list[tuple[str, int, float]] = []  # (status, seq killed at, seconds to progress)
    seen = 0
    deadline = time.monotonic() + 1200
    while (st := status_of(iid)) != "handed_over":
        assert time.monotonic() < deadline, f"stuck in {st}"
        seq = last_seq(app_engine, iid)
        if seq > seen and len(resumed) < MAX_KILLS:  # a step was recorded: kill in the next
            seen = seq
            up = crash(2 if not resumed else 1)  # the first kill is two in a row
            stack.wait_for(
                lambda b=seq: last_seq(app_engine, iid) > b, RESUME_S, f"progress after {st}", 1
            )
            resumed.append((str(st), seq, round(time.monotonic() - up, 1)))
        time.sleep(0.2)
    print(f"kills (status, at seq, seconds to resume): {resumed}")
    assert len({s for s, _, _ in resumed}) >= 2, resumed  # more than one status was hit
    assert all(t <= RESUME_S for _, _, t in resumed)

    with app_engine.connect() as conn:
        events = conn.execute(
            text("SELECT type, data FROM timeline_events WHERE incident_id = :i ORDER BY seq"),
            {"i": iid},
        ).all()
        notes = conn.execute(
            text("SELECT kind, count(*) FROM notifications WHERE incident_id = :i GROUP BY kind"),
            {"i": iid},
        ).all()
    moves = Counter(
        (e.data["from_status"], e.data["to_status"])
        for e in events
        if e.type == "aisre.incident.status_changed.v1"
    )
    assert moves and max(moves.values()) == 1, moves  # no step recorded twice
    assert sum(e.type == "aisre.incident.handed_over.v1" for e in events) == 1
    sent = dict(notes)
    assert sent.get("opened") == 1 and sent.get("handed_over", 1) == 1, sent  # sent once
