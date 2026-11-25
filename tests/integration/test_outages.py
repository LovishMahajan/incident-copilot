"""T4.2 (Artifact 12.5; NFR-28, NFR-47): the model provider down, on the Compose stack.

`fault`: stops fake-provider, so only `-m fault` runs it, and it puts it back (stack.restore).

With the provider unreachable, evidence collection still runs, the model step fails after the
gateway's 3 attempts, and the incident is handed over with its evidence and timeline: it is
not retried without bound and doesn't wait for the provider (12.5, ADR-52). The database and
Temporal outages are in test_alert_faults.py, with the pages they raise; the 24-hour limit,
made short, is test_limits.py; the per-source retry and breaker bounds are unit tests
(test_evidence_collector.py, test_layer10.py).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import stack
from sqlalchemy import text
from test_alert_faults import post_alert, seeded_service, status_of

pytestmark = pytest.mark.fault

ATTEMPTS = 3  # gateway.GatewayConfig.max_attempts, per step


@pytest.fixture
def stack_up() -> Iterator[None]:
    why = stack.available()
    if why:
        pytest.skip(why)
    try:
        yield
    finally:
        stack.restore()


def test_provider_down_hands_over_with_evidence_and_bounded_calls(stack_up, app_engine):
    service = seeded_service()
    stack.stop("fake-provider")
    try:
        code, body = post_alert(service)
        assert code == 201, body
        iid = str(body["incident_id"])  # type: ignore[index]
        stack.wait_for(
            lambda: status_of(iid) == "handed_over", 420, "the incident to be handed over", 5
        )
    finally:
        stack.start("fake-provider")
    with app_engine.connect() as conn:
        kinds = [
            str(t)
            for (t,) in conn.execute(
                text("SELECT type FROM timeline_events WHERE incident_id = :i ORDER BY seq"),
                {"i": iid},
            )
        ]
        per_step = conn.execute(
            text("SELECT task, count(*) FROM model_attempts WHERE incident_id = :i GROUP BY task"),
            {"i": iid},
        ).all()
    assert "aisre.evidence.collected.v1" in kinds  # evidence still collected
    assert kinds[-1] == "aisre.incident.handed_over.v1"
    assert all(n <= ATTEMPTS for _, n in per_step), per_step  # bounded, never unbounded
