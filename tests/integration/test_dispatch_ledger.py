"""Artifact 13.5 T3.4, T3.7, T3.16 (FR-27 to FR-29, FR-53, FR-55, FR-56, ST-03 to ST-05, ST-13,
ST-37, ADR-47, ADR-69): Layer 11's dispatcher over the PostgreSQL ledger, trusted store and
fence, connected as `aisre_dispatcher`, against the fake release process in-process.

The reference fix is drafted, tested, sealed and approved by two people through the API
(test_approvals' fixtures); the workflow's `executing` transition marks it `approved`. Then
the dispatcher alone writes: intent, the outcome, the action's status, blocks and its events.
"""

from __future__ import annotations

import asyncio
import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from test_approvals import (  # noqa: F401 (fixtures)
    PUBLIC,
    awaiting,
    client_for,
    headers,
    version_of,
)
from test_patch_review import drafted, events, review  # noqa: F401 (fixtures)

from aisre.record import make_engine, uuid7
from aisre.record.execution import PgFence, PgLedger, PgTrustedStore
from aisre.record.transitions import Transition, record_transition
from aisre.remediation.action import (
    ApprovalError,
    ApprovalPolicy,
    Directory,
    development_verify_keys,
)
from aisre.remediation.dispatcher import Dispatcher, LedgerState, Outcome, TargetState
from aisre.remediation.release import FakeReleaseClient

pytestmark = pytest.mark.integration
ROOT = Path(__file__).parents[2]


def _fake(name: str):
    spec = importlib.util.spec_from_file_location(f"fake_{name}_t34", ROOT / f"ops/fakes/{name}.py")
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


release_fake = _fake("release")


@pytest.fixture(scope="module")
def dispatcher_engine(postgres_url, settings):
    engine = make_engine(settings.dispatcher_database_url)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT current_user")).scalar() == "aisre_dispatcher"
    yield engine
    engine.dispose()


@pytest.fixture
def released(reference_repo):
    repo, _ = reference_repo
    release = release_fake.Release(release_fake.review.Repo(repo, "main"), "main")
    server = release_fake.serve(release, "t")
    yield release, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def approved(app_engine, settings, awaiting, drafted):  # noqa: F811
    org, service, incident_id, action_id, out = awaiting
    v = version_of(app_engine, action_id)
    with client_for(settings, app_engine) as c:
        for who in ("priya", "sam"):
            r = c.post(
                f"/v1/actions/{action_id}/approvals",
                json={"action_digest": out.action_digest},
                headers=headers(org, service, who, version=v),
            )
            assert r.status_code == 201, r.json()
    with app_engine.begin() as conn:
        version = conn.execute(
            text("SELECT version FROM incidents WHERE incident_id = :i"), {"i": incident_id}
        ).scalar_one()
        record_transition(
            conn,
            Transition(
                incident_id, str(uuid7()), "awaiting_approval", "executing", version, "approved",
                action_id=str(action_id), action_from="awaiting_approval", action_to="approved",
            ),
        )  # fmt: skip
    return service, incident_id, action_id, drafted[4]


def directory(service: str) -> Directory:
    d = Directory()
    for who in ("user:priya", "user:sam"):
        d.add_groups(who, [f"aisre:approver:{service}"], service)
    d.add_groups("user:lee", [f"aisre:incident_lead:{service}"], service)
    return d


def dispatcher(engine, service, release, blobs, *, timeout=5.0) -> Dispatcher:
    return Dispatcher(
        PgTrustedStore(engine, blobs),
        release,
        PgLedger(engine),
        development_verify_keys(PUBLIC),
        directory(service),
        ApprovalPolicy(),
        lambda: datetime.now(UTC),
        timeout,
        fence=PgFence(engine),
    )


def client(url, store) -> FakeReleaseClient:
    return FakeReleaseClient(url, SecretStr("t"), store.patch_content)


def attempts(app_engine, action_id) -> list[tuple]:
    with app_engine.connect() as conn:
        return [
            tuple(r)
            for r in conn.execute(
                text(
                    "SELECT attempt_no, outcome, reconciliation FROM execution_attempts "
                    "WHERE action_id = :a ORDER BY attempt_no"
                ),
                {"a": action_id},
            )
        ]


def status(app_engine, action_id) -> str:
    with app_engine.connect() as conn:
        return conn.execute(
            text("SELECT status FROM actions WHERE action_id = :a"), {"a": action_id}
        ).scalar_one()


def test_fr27_to_fr29_an_approved_fix_runs_once_as_the_dispatcher(
    app_engine, dispatcher_engine, approved, released
):
    service, incident_id, action_id, blobs = approved
    release, url = released
    store = PgTrustedStore(dispatcher_engine, blobs)
    d = dispatcher(dispatcher_engine, service, client(url, store), blobs)
    first = asyncio.run(d.execute(str(action_id)))
    again = asyncio.run(d.execute(str(action_id)))
    assert (first.outcome, again.outcome) == (Outcome.APPLIED, Outcome.APPLIED), first.reason
    assert "idempotent" in again.reason and len(release.history) == 2  # one write (ST-37)
    assert attempts(app_engine, action_id) == [(1, "applied", False)]
    assert status(app_engine, action_id) == "executed"
    assert release.state()["config"]["ORDER_TAX_RATE_BPS"] == "825"
    assert len(events(app_engine, incident_id, "aisre.execution.started.v1")) == 1
    (done,) = events(app_engine, incident_id, "aisre.execution.completed.v1")
    assert done["outcome"] == "applied"


def test_st13_bytes_that_hash_to_something_else_void_the_approval_and_send_nothing(
    app_engine, dispatcher_engine, approved, released
):
    service, incident_id, action_id, blobs = approved
    release, url = released

    class Tampered:
        def get(self, key: str) -> bytes:
            return blobs.get(key) + b" "

    store = PgTrustedStore(dispatcher_engine, Tampered())
    res = asyncio.run(
        dispatcher(dispatcher_engine, service, client(url, store), Tampered()).execute(
            str(action_id)
        )
    )
    assert res.outcome == Outcome.REFUSED and res.void_approval and not res.intent_recorded
    assert attempts(app_engine, action_id) == [] and len(release.history) == 1
    (refused,) = events(app_engine, incident_id, "aisre.execution.refused.v1")
    assert "approved as" in refused["reason"]


def test_fr27_a_target_that_moved_since_approval_voids_it(
    app_engine, dispatcher_engine, approved, released
):
    service, _, action_id, blobs = approved
    release, url = released
    release.out_of_band({"set": {"ORDER_TAX_RATE_BPS": "900"}})
    store = PgTrustedStore(dispatcher_engine, blobs)
    res = asyncio.run(
        dispatcher(dispatcher_engine, service, client(url, store), blobs).execute(str(action_id))
    )
    assert res.outcome == Outcome.REFUSED and res.void_approval and "approved for" in res.reason
    assert attempts(app_engine, action_id) == []


def test_fr56_a_stop_before_intent_is_honoured(app_engine, dispatcher_engine, approved, released):
    service, incident_id, action_id, blobs = approved
    _, url = released
    with app_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO stop_requests (stop_request_id, incident_id, requested_by, reason)"
                " VALUES (:s, :i, 'user:rin', 'customer comms first')"
            ),
            {"s": uuid7(), "i": incident_id},
        )
    store = PgTrustedStore(dispatcher_engine, blobs)
    res = asyncio.run(
        dispatcher(dispatcher_engine, service, client(url, store), blobs).execute(str(action_id))
    )
    assert res.outcome == Outcome.REFUSED and "stop requested by user:rin" in res.reason
    assert attempts(app_engine, action_id) == [] and status(app_engine, action_id) == "approved"


def test_st05_a_fence_refuses_the_write(app_engine, dispatcher_engine, approved, released):
    service, _, action_id, blobs = approved
    _, url = released
    fence_id = uuid7()
    with app_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO fence_reasons (fence_id, scope, reason, why, set_by) VALUES"
                " (:f, 'orders-api-production', 'manual', 'key rotation drill', 'user:lee')"
            ),
            {"f": fence_id},
        )
    try:
        store = PgTrustedStore(dispatcher_engine, blobs)
        res = asyncio.run(
            dispatcher(dispatcher_engine, service, client(url, store), blobs).execute(
                str(action_id)
            )
        )
    finally:
        with app_engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE fence_reasons SET cleared_by = 'user:lee', cleared_at = now()"
                    " WHERE fence_id = :f"
                ),
                {"f": fence_id},
            )
    assert res.outcome == Outcome.REFUSED and "fenced: manual" in res.reason
    assert attempts(app_engine, action_id) == []


class Late:
    """A release whose apply answers after the dispatcher stopped waiting, and only when told;
    meanwhile its lookup finds nothing and the target still runs the base."""

    def __init__(self, base: str) -> None:
        self.base = base

    async def current_state(self, deployment: str) -> TargetState:
        return TargetState(self.base, None)

    async def apply(self, *args) -> str:
        await asyncio.sleep(3600)
        return "applied"

    async def lookup(self, deployment: str, key: str) -> None:
        return None


def test_fr55_a_late_apply_puts_the_block_back_and_only_the_incident_lead_clears_it(
    app_engine, dispatcher_engine, approved
):
    service, incident_id, action_id, blobs = approved
    store = PgTrustedStore(dispatcher_engine, blobs)
    base = store.action(str(action_id)).base_revision
    d = dispatcher(dispatcher_engine, service, Late(base), blobs, timeout=0.2)
    res = asyncio.run(d.execute(str(action_id)))
    assert res.outcome == Outcome.NOT_APPLIED  # reconciled: nothing found, base still running
    assert attempts(app_engine, action_id) == [(1, "unknown", False), (2, "not_applied", True)]
    first = d.ledger.get(str(action_id))
    assert first is not None
    late = d._record(  # the first call's answer lands now
        type(first)(**{**first.__dict__, "attempt_no": 1, "state": LedgerState.STARTED}),
        LedgerState.APPLIED,
        "release process: applied",
    )
    assert late.outcome == Outcome.UNKNOWN and "blocked" in late.reason
    with app_engine.connect() as conn:
        blocks = conn.execute(
            text(
                "SELECT reason, cleared_at IS NULL FROM deployment_blocks WHERE action_id = :a"
                " ORDER BY opened_at"
            ),
            {"a": action_id},
        ).all()
    assert [tuple(b) for b in blocks] == [("unresolved", False), ("late_apply", True)]
    now = datetime.now(UTC)
    with pytest.raises(ApprovalError, match="forbidden"):
        d.resolve_outcome(
            str(action_id), subject="user:priya", outcome="applied", observed_revision="r",
            reason="saw it", auth_time=now, mfa=True,
        )  # fmt: skip
    done = d.resolve_outcome(
        str(action_id), subject="user:lee", outcome="applied",
        observed_revision="cfg-42+patch", reason="the change did land", auth_time=now, mfa=True,
    )  # fmt: skip
    assert done.outcome == Outcome.APPLIED and d.ledger.unresolved("orders-api-production") == []


def test_fr53_an_unanswered_write_blocks_until_settled_and_a_person_needs_a_fresh_sign_in(
    app_engine, dispatcher_engine, approved
):
    service, _, action_id, blobs = approved
    store = PgTrustedStore(dispatcher_engine, blobs)
    action = store.action(str(action_id))
    d = dispatcher(dispatcher_engine, service, Late(action.base_revision), blobs, timeout=0.2)
    res = asyncio.run(d.execute(str(action_id)))
    assert res.outcome == Outcome.NOT_APPLIED  # the honest reconciliation settles it
    assert status(app_engine, action_id) == "not_applied"
    with app_engine.connect() as conn:  # the block opened with intent and cleared with it
        assert (
            conn.execute(
                text("SELECT cleared_by FROM deployment_blocks WHERE action_id = :a"),
                {"a": action_id},
            ).scalar_one()
            == "system:dispatcher"
        )
    old = datetime.now(UTC) - timedelta(minutes=30)
    with pytest.raises(ApprovalError, match="reauth_required"):
        d.resolve_outcome(
            str(action_id), subject="user:lee", outcome="applied", observed_revision="r",
            reason="x", auth_time=old, mfa=True,
        )  # fmt: skip


def test_the_dispatcher_role_writes_nothing_else(dispatcher_engine, approved):
    _, incident_id, action_id, _ = approved
    with dispatcher_engine.begin() as conn, pytest.raises(Exception, match="permission denied"):
        conn.execute(
            text("UPDATE incidents SET status = 'closed' WHERE incident_id = :i"),
            {"i": UUID(incident_id)},
        )
