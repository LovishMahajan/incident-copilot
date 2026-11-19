"""T4.6, T4.7 pass 2 (Artifact 12.3; WR-25; ADR-74): more alerts fired by real faults on the
Compose stack, each delivered Prometheus -> Alertmanager -> the FAKE pager, to its role.

`fault`: these restart or stop this project's services and take minutes; each puts the stack
back (stack.restore) and waits first for its alert to be quiet, since Alertmanager doesn't send
a still-firing alert again within its 4-hour repeat interval.

- AisreQueueDepthHigh (Incident lead) and AisreQueueBacklog (Platform owner): 56 alerts on 56
  services through intake, each confirmed sev2 by the incident lead while queued (intake keeps
  no severity, and the queue takes a severity set while waiting), with the worker's scripted steps slowed to 10 s so the 5 slots
  stay held; the worker's own queue gauges cross 50 waiting, then the oldest waits 5 minutes.
  The 56 are cancelled at the end, and the queue drains.
- AisreModelAuthErrors (Platform owner): the worker's provider key rejected (the fake provider's
  `fake-script-auth-error`).
- AisreModelOutputSecret (Security lead): a model answer holding a fake credential
  (`fake-script-leak-secret`), stopped by the gateway.
- AisreCircuitOpen (Platform owner): the monitoring source stopped and alerts kept coming, so
  the evidence collector's breaker keeps reopening (10 failures in 60 s, open 30 s) for the
  alert's 5-minute hold.
- AisreReleaseChangeUnmatched (Security lead): an out-of-band change on the fake release
  process, found by the release reconciliation triggered by hand (ST-40).
- AisreAuditChainBroken (Security lead): one timeline event altered with the superuser in
  replica mode (only a privileged edit can), found by the chain check run on a worker start;
  the edit is reverted and the check run again.

AisreSandboxLeak can't be fired here: the Test Runner that publishes it runs on the host and
Prometheus doesn't scrape it (ADR-74).
"""

from __future__ import annotations

import asyncio
import time
import urllib.request
import uuid
from collections.abc import Iterator

import pytest
import stack
from sqlalchemy import create_engine, text
from temporalio.client import Client
from test_alert_faults import post_alert, seeded_service, status_of
from test_worker_kills import SLOW

pytestmark = pytest.mark.fault

DEV_ORG = "00000000-0000-7000-8000-000000000001"
SUPERUSER = "postgresql+psycopg://postgres:fake-superuser-password@127.0.0.1:55432/aisre"
BURST = 56  # 5 run, 51 wait: over AisreQueueDepthHigh's 50


@pytest.fixture
def stack_up() -> Iterator[None]:
    why = stack.available()
    if why:
        pytest.skip(why)
    try:
        yield
    finally:
        stack.restore()


def page(alertname: str, role: str, since: str, within: float) -> dict:
    got = stack.wait_for(
        lambda: stack.pages_since(since, alertname=alertname, status="firing"),
        within,
        f"{alertname} to reach the pager",
        every=5,
    )[0]
    assert got["role"] == role, got
    return got


def services(app_engine, n: int) -> list[str]:
    names = [f"m4-burst-{uuid.uuid4().hex[:8]}" for _ in range(n)]
    with app_engine.begin() as conn:
        for name in names:
            conn.execute(
                text(
                    "INSERT INTO services (service_id, org_id, slug, display_name, owner_group) "
                    "VALUES (gen_random_uuid(), :o, :s, :s, 'test-owners')"
                ),
                {"o": DEV_ORG, "s": name},
            )
    return names


def cancel(iid: str) -> None:
    lead = stack.token_for("m4-fault-lead", ["aisre:incident_lead"])
    stack.http(
        "POST",
        f"{stack.API}/v1/incidents/{iid}/cancellation",
        {"reason": "M4 fault test: burst cleared"},
        {"Authorization": f"Bearer {lead}", "Idempotency-Key": f"m4-cancel-{uuid.uuid4().hex}"},
    )


def confirm(iid: str, severity: str) -> None:
    lead = stack.token_for("m4-fault-lead", ["aisre:incident_lead"])
    auth = {"Authorization": f"Bearer {lead}"}
    req = urllib.request.Request(f"{stack.API}/v1/incidents/{iid}", headers=auth)
    with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 (local stack)
        etag = r.headers["ETag"]
    code, out = stack.http(
        "POST",
        f"{stack.API}/v1/incidents/{iid}/severity-confirmation",
        {"severity": severity},
        auth | {"If-Match": etag, "Idempotency-Key": f"m4-sev-{uuid.uuid4().hex}"},
    )
    assert code == 200, out


def queued(app_engine) -> int:
    with app_engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT count(*) FROM investigation_queue WHERE granted_at IS NULL")
            ).scalar_one()
        )


def test_a_burst_pages_queue_depth_then_backlog(stack_up, app_engine):
    stack.start("worker", overrides=SLOW)
    stack.wait_for(lambda: queued(app_engine) == 0, 300, "the stack's queue to drain", 5)
    stack.quiet_alert("AisreQueueDepthHigh")
    stack.quiet_alert("AisreQueueBacklog")
    since = stack.now_iso()
    mon = stack.token_for("monitoring", ["aisre:monitoring"])
    made = []
    try:
        for svc in services(app_engine, BURST):
            body = {
                "service": svc,
                "environment": "production",
                "title": f"M4 burst on {svc}",
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "source": "monitoring",
                "severity_hint": "sev2",
            }
            code, out = stack.http(
                "POST",
                f"{stack.API}/v1/incidents",
                body,
                {"Authorization": f"Bearer {mon}", "Idempotency-Key": f"m4-{uuid.uuid4().hex}"},
            )
            assert code == 201, out
            made.append(str(out["incident_id"]))
        for iid in made:  # intake keeps no severity: the incident lead confirms it, queued
            confirm(iid, "sev2")
        t0 = time.monotonic()
        page("AisreQueueDepthHigh", "incident_lead", since, 240)
        print(f"AisreQueueDepthHigh paged the incident lead {time.monotonic() - t0:.0f} s in")
        page("AisreQueueBacklog", "platform_owner", since, 600)
        print(f"AisreQueueBacklog paged the platform owner {time.monotonic() - t0:.0f} s in")
    finally:
        for iid in made:
            cancel(iid)
    stack.wait_for(lambda: queued(app_engine) == 0, 600, "the burst to leave the queue", 5)


@pytest.mark.parametrize(
    ("key", "alertname", "role"),
    [
        ("fake-script-auth-error", "AisreModelAuthErrors", "platform_owner"),
        ("fake-script-leak-secret", "AisreModelOutputSecret", "security_lead"),
    ],
)
def test_a_provider_fault_pages_its_role(stack_up, key, alertname, role):
    stack.quiet_alert(alertname)
    since = stack.now_iso()
    stack.start("worker", overrides={"AISRE_PROVIDER_API_KEY": key})
    code, body = post_alert(seeded_service())
    assert code == 201, body
    iid = str(body["incident_id"])  # type: ignore[index]
    page(alertname, role, since, 300)
    stack.wait_for(lambda: status_of(iid) == "handed_over", 300, "the hand-over", 5)


def replayed_alert(service: str) -> tuple[int, object]:
    """An alert at the reference incident's time: the evidence replay stops at 2026-10-20
    14:20 (README), so only an alert inside it makes the collector call the source at all."""
    mon = stack.token_for("monitoring", ["aisre:monitoring"])
    body = {
        "service": service,
        "environment": "production",
        "title": f"M4 circuit test on {service}",
        "started_at": "2026-10-20T14:07:00Z",
        "source": "monitoring",
    }
    return stack.http(
        "POST",
        f"{stack.API}/v1/incidents",
        body,
        {"Authorization": f"Bearer {mon}", "Idempotency-Key": f"m4-{uuid.uuid4().hex}"},
    )


def test_the_monitoring_source_down_opens_the_circuit_and_pages(stack_up):
    """The breaker opens for 30 s after 10 failing calls in 60 s, and its gauge falls back to 0
    when it next lets a call through; AisreCircuitOpen holds 5 minutes. So the alert fires only
    while failing calls keep coming: an alert every 15 s keeps the collector calling."""
    stack.quiet_alert("AisreCircuitOpen")
    since = stack.now_iso()
    stack.stop("fake-monitoring")
    deadline = time.monotonic() + 15 * 60
    try:
        while not stack.pages_since(since, alertname="AisreCircuitOpen", status="firing"):
            assert time.monotonic() < deadline, "AisreCircuitOpen never paged"
            code, body = replayed_alert(seeded_service())
            assert code == 201, body
            time.sleep(15)
        page("AisreCircuitOpen", "platform_owner", since, 5)
    finally:
        stack.start("fake-monitoring")


async def trigger_release_reconciliation() -> None:
    client = await Client.connect("127.0.0.1:7233")
    await client.get_schedule_handle("aisre-release-reconciliation").trigger()


def test_an_unmatched_release_change_pages_the_security_lead(stack_up):
    stack.quiet_alert("AisreReleaseChangeUnmatched")
    since = stack.now_iso()
    code, out = stack.http(
        "POST",
        f"{stack.RELEASE}/_fake/deployments/orders-api-production/overrides",
        {"set": {"M4_FAULT_TEST": "out-of-band"}},
    )
    assert code in (200, 201), out
    try:
        asyncio.run(trigger_release_reconciliation())
        page("AisreReleaseChangeUnmatched", "security_lead", since, 300)
    finally:
        stack.http("POST", f"{stack.RELEASE}/_fake/reset", {})
        asyncio.run(trigger_release_reconciliation())  # finds nothing now: the gauge goes to 0


async def rerun_chain_check() -> None:
    client = await Client.connect("127.0.0.1:7233")
    try:
        await client.get_workflow_handle("aisre-audit-chain-check").terminate("M4 fault test")
    except Exception:  # noqa: BLE001 (not running: the worker start begins it)
        pass


def test_a_broken_audit_chain_pages_the_security_lead(stack_up):
    code, body = post_alert(seeded_service())
    assert code == 201, body
    iid = str(body["incident_id"])  # type: ignore[index]
    stack.wait_for(lambda: status_of(iid) == "handed_over", 300, "the hand-over", 5)
    stack.quiet_alert("AisreAuditChainBroken")
    since = stack.now_iso()
    su = create_engine(SUPERUSER)
    edit = "UPDATE timeline_events SET actor = :a WHERE incident_id = :i AND seq = 1"  # noqa: S608
    try:
        with su.begin() as conn:
            actor = conn.execute(
                text("SELECT actor FROM timeline_events WHERE incident_id = :i AND seq = 1"),
                {"i": iid},
            ).scalar_one()
            conn.execute(text("SET LOCAL session_replication_role = replica"))
            conn.execute(text(edit), {"a": "user:m4-tamperer", "i": iid})
        asyncio.run(rerun_chain_check())
        stack.compose("restart", "worker")  # ensure_audit_jobs starts the check again: it runs
        page("AisreAuditChainBroken", "security_lead", since, 600)
    finally:
        with su.begin() as conn:
            conn.execute(text("SET LOCAL session_replication_role = replica"))
            conn.execute(text(edit), {"a": actor, "i": iid})
        su.dispose()
        asyncio.run(rerun_chain_check())
        stack.compose("restart", "worker")  # the check runs again and finds nothing


def test_writes_fenced_for_15_minutes_pages_the_platform_owner(stack_up):
    """RB-11: a manual fence set and lifted through the CLI (T4.4); the dispatcher publishes
    `aisre_dispatch_fenced`, and after AisreWritesFenced's 15-minute hold it pages. A deployment
    of the stack's own tests' would be fenced too, so nothing else should run meanwhile."""
    from test_fence_cli import cli

    env = {
        "AISRE_ENV": "test",
        "AISRE_DATABASE_URL": "postgresql+psycopg://aisre_app:fake-app-password@127.0.0.1:55432/aisre",
        "AISRE_OIDC_ISSUER": "http://token-issuer:8080",
        "AISRE_OIDC_AUDIENCE": "aisre-api",
        "AISRE_OIDC_JWKS_URL": f"{stack.ISSUER}/jwks",
    }
    stack.quiet_alert("AisreWritesFenced")
    lead = stack.token_for("ines", ["aisre:incident_lead"])
    since = stack.now_iso()
    code, out = cli(env | {"AISRE_TOKEN": lead}, "fence", "set", "--why", "M4 RB-11 fault test")
    assert code == 0, out
    try:
        page("AisreWritesFenced", "platform_owner", since, 20 * 60)
    finally:  # tokens last 5 minutes: fresh ones for the lift
        lead = stack.token_for("ines", ["aisre:incident_lead"])
        admin = stack.token_for("pat", ["aisre:platform_admin"])
        code, out = cli(env | {"AISRE_TOKEN": admin, "AISRE_SIGN_OFF_TOKEN": lead}, "fence", "lift")
        assert code == 0, out
