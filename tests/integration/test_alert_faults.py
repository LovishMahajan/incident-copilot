"""T4.6, T4.2 (Artifact 12.3, 12.5; NFR-05 to NFR-07, NFR-25; WR-25): alerts fired by real
faults on the Compose stack, delivered Prometheus -> Alertmanager -> the FAKE pager (ADR-74).

`fault`: these stop this project's services and take minutes, so only `-m fault` runs them.
Each puts every service back up and healthy before it ends (stack.restore).

- PostgreSQL stopped: the API stays live but not ready and refuses intake with 503 (it never
  acknowledges what it can't store), the dispatcher can't record intent, so nothing executes
  (NFR-05), and AisrePostgresDown reaches the Platform owner's receiver while PostgreSQL is
  still down: the paging path has no PostgreSQL in it (NFR-25), timed against NFR-07's 2
  minutes. When it returns, every process is ready again and a new incident is investigated
  (NFR-06).
- An approval attempt refused for its sign-in (`reauth_required`, a security code) raises
  AisreApprovalRefused, which Alertmanager routes to the Security lead's receiver.
- Temporal stopped: intake still answers 201, the start waits in the outbox, AisreOutboxStuck
  pages the Platform owner after 120 s, and once Temporal is back the outbox drains and the
  incident's workflow starts: nothing lost (12.5).

Which alerts can't be fired by a fault on this stack, and why, is in ADR-74.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator

import pytest
import stack
from sqlalchemy import text

pytestmark = pytest.mark.fault

NFR07_S = 120  # NFR-07: on-call told within 2 minutes of the outage starting


@pytest.fixture
def stack_up() -> Iterator[None]:
    why = stack.available()
    if why:
        pytest.skip(why)
    try:
        yield
    finally:
        stack.restore()


def seeded_service() -> str:
    """A fresh service in the development organisation (the dev seed), so the alert opens its
    own incident instead of joining an open one."""
    name = f"m4-fault-{uuid.uuid4().hex[:8]}"
    stack.compose("exec", "-T", "api", "python", "-m", "aisre.api.dev_seed", name, "production")
    return name


def post_alert(service: str) -> tuple[int, object]:
    mon = stack.token_for("monitoring", ["aisre:monitoring"])
    body = {
        "service": service,
        "environment": "production",
        "title": f"M4 fault test on {service}",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "monitoring",
    }
    return stack.http(
        "POST",
        f"{stack.API}/v1/incidents",
        body,
        {"Authorization": f"Bearer {mon}", "Idempotency-Key": f"m4-fault-{uuid.uuid4().hex}"},
    )


def status_of(incident_id: str) -> str | None:
    read = stack.token_for("m4-reader", ["aisre:viewer"])
    code, body = stack.http(
        "GET",
        f"{stack.API}/v1/incidents/{incident_id}",
        headers={"Authorization": f"Bearer {read}"},
    )
    return str(body["status"]) if code == 200 and isinstance(body, dict) else None


def test_postgres_down_pages_the_platform_owner_without_postgres(stack_up):
    stack.quiet_alert("AisrePostgresDown")
    since = stack.now_iso()
    stack.stop("postgres")
    t0 = time.monotonic()
    try:
        assert stack.http("GET", f"{stack.API}/livez")[0] == 200  # alive: don't restart it
        assert stack.wait_for(
            lambda: stack.http("GET", f"{stack.API}/readyz")[0] == 503, 20, "readyz to fail"
        )
        code, _ = post_alert("orders-api")
        assert code == 503  # never acknowledged without being stored
        page = stack.wait_for(
            lambda: stack.pages_since(since, alertname="AisrePostgresDown", status="firing"),
            240,
            "AisrePostgresDown to reach the pager",
            every=1,
        )[0]
        elapsed = time.monotonic() - t0
        print(f"AisrePostgresDown paged {page['role']} {elapsed:.0f} s after PostgreSQL stopped")
        assert page["role"] == "platform_owner" and page["severity"] == "page"
        assert elapsed <= NFR07_S, f"NFR-07: paged after {elapsed:.0f} s"
    finally:
        stack.start("postgres")
    stack.restore()
    assert stack.wait_for(
        lambda: stack.http("GET", f"{stack.API}/readyz")[0] == 200, 120, "readyz to pass"
    )
    service = seeded_service()
    code, body = post_alert(service)
    assert code == 201, body
    iid = str(body["incident_id"])  # type: ignore[index]
    t1 = time.monotonic()
    stack.wait_for(lambda: status_of(iid) not in (None, "open"), 120, "the incident to start")
    print(f"after recovery, a new incident left `open` in {time.monotonic() - t1:.0f} s")


def test_a_refused_approval_pages_the_security_lead(stack_up, app_engine):
    with app_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT a.action_id, a.action_digest, a.version FROM actions a "
                "JOIN services s ON s.service_id = a.target_service_id "
                "WHERE s.slug = 'orders-api' AND a.action_digest IS NOT NULL "
                "AND s.org_id = '00000000-0000-7000-8000-000000000001' "  # the issuer's default
                "ORDER BY a.created_at DESC LIMIT 1"
            )
        ).first()
    if row is None:
        pytest.skip("no orders-api action with a digest on this stack: run the M3 demo first")
    # A page goes out when the alert starts firing; one still firing from an earlier run isn't
    # sent again until Alertmanager's repeat interval, so wait for it to clear first.
    stack.quiet_alert("AisreApprovalRefused")
    since = stack.now_iso()
    approver = stack.token_for("m4-fault-approver", ["aisre:approver:orders-api"], mfa=False)
    code, body = stack.http(
        "POST",
        f"{stack.API}/v1/actions/{row.action_id}/approvals",
        {"action_digest": row.action_digest},
        {
            "Authorization": f"Bearer {approver}",
            "Idempotency-Key": f"m4-fault-{uuid.uuid4().hex}",
            "If-Match": f'"{row.version}"',
        },
    )
    assert code == 401 and body["code"] == "reauth_required", body  # type: ignore[index]
    page = stack.wait_for(
        lambda: stack.pages_since(since, alertname="AisreApprovalRefused", status="firing"),
        120,
        "AisreApprovalRefused to reach the pager",
    )[0]
    assert page["role"] == "security_lead"
    _, am = stack.http("GET", f"{stack.ALERTMANAGER}/api/v2/alerts")
    receivers = {
        r["name"]
        for a in am
        for r in a["receivers"]
        if a["labels"]["alertname"] == "AisreApprovalRefused"
    }
    assert receivers == {"security_lead"}


def test_temporal_down_holds_starts_in_the_outbox_and_pages(stack_up, app_engine):
    service = seeded_service()
    stack.quiet_alert("AisreOutboxStuck")
    since = stack.now_iso()
    stack.stop("temporal")
    try:
        code, body = post_alert(service)
        assert code == 201, body  # intake continues (12.5)
        iid = str(body["incident_id"])  # type: ignore[index]
        page = stack.wait_for(
            lambda: stack.pages_since(since, alertname="AisreOutboxStuck", status="firing"),
            300,
            "AisreOutboxStuck to reach the pager",
            every=3,
        )[0]
        assert page["role"] == "platform_owner"
        assert status_of(iid) == "open"
    finally:
        stack.start("temporal")
    stack.restore()
    stack.wait_for(lambda: status_of(iid) not in (None, "open"), 180, "the held start to run")
    with app_engine.connect() as conn:
        undelivered = conn.execute(
            text(
                "SELECT count(*) FROM outbox WHERE incident_id = :i AND delivered_at IS NULL "
                "AND kind = 'start_workflow'"
            ),
            {"i": iid},
        ).scalar_one()
    assert undelivered == 0
