"""ST-37, ST-38, ST-40: one execution per action, surviving crashes and a lying release process. Real dispatcher and
ledger against a fake release process. The crash is an exception no dispatcher path catches, so nothing after that
point runs; a new Dispatcher then picks up over the same ledger, as a restarted process does over the database."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from support import (
    AFTER,
    BASE,
    KEYS,
    POLICY,
    SOON,
    T0,
    FakeRelease,
    decide,
    make_action,
    people,
    rig,
)

from aisre.reliability.fence import ActionRecord, FenceState, reconcile_after_restore
from aisre.remediation.action import ApprovalError
from aisre.remediation.dispatcher import (
    Dispatcher,
    LedgerEntry,
    LedgerState,
    Outcome,
    ReleaseInProgress,
)
from aisre.workflow import workflow as wf
from aisre.workflow.workflow import ApprovalSignal, IncidentWorkflow


class Killed(BaseException):
    """The process died here."""


def approved(**kw):
    a = make_action(**kw)
    return a, [decide(a, "priya")]


def restarted(r, later=timedelta(minutes=2)):
    """A new process over the same ledger. By default past the in-flight window: the first call is dead."""
    return Dispatcher(
        r.store,
        r.release,
        r.ledger,
        KEYS,
        people(),
        POLICY,
        lambda: SOON + later,
        fence=FenceState(),
    )


def applied_rows(r, key):
    return [e for e in r.ledger.rows[key] if e.state == LedgerState.APPLIED]


# ---------------------------------------------------------------- ST-37
def test_st37_second_dispatch_after_success_gets_the_first_result():
    a, ds = approved()
    r = rig((a, ds))
    first, second = r.run(a.action_id), r.run(a.action_id)
    assert first.outcome == second.outcome == Outcome.APPLIED and "idempotent" in second.reason
    assert len(r.release.calls) == 1 and len(applied_rows(r, a.action_id)) == 1


def test_st37_two_dispatches_started_together_send_one_write():
    class Yields(FakeRelease):  # both calls are past their pre-checks before either records intent
        async def current_state(self, deployment):
            await asyncio.sleep(0)
            return await super().current_state(deployment)

    a, ds = approved()
    r = rig((a, ds), release=Yields())

    async def both():
        return await asyncio.gather(
            r.dispatcher.execute(a.action_id), r.dispatcher.execute(a.action_id)
        )

    results = asyncio.run(both())
    assert len(r.release.calls) == 1 and len(applied_rows(r, a.action_id)) == 1
    assert sorted(x.outcome.value for x in results) == ["applied", "refused"]


class SlowRelease(FakeRelease):
    """apply is in flight on the network: the release process hasn't seen the key yet."""

    def __init__(self):
        super().__init__()
        self.sent, self.go = asyncio.Event(), asyncio.Event()

    async def apply(self, *args):
        self.sent.set()
        await self.go.wait()
        return await super().apply(*args)


def test_st37_a_repeat_during_a_live_apply_cannot_record_not_applied():
    a, ds = approved()

    async def scenario():
        release = SlowRelease()
        r = rig((a, ds), release=release)
        first = asyncio.create_task(r.dispatcher.execute(a.action_id))
        await release.sent.wait()
        await r.dispatcher.execute(a.action_id)  # the repeat
        release.go.set()
        await asyncio.gather(first, return_exceptions=True)
        return r

    r = asyncio.run(scenario())
    assert r.release.revision == AFTER
    assert r.ledger.get(a.action_id).state != LedgerState.NOT_APPLIED


def repeat_during_apply(repeat):
    """Runs `repeat(r)` while the first call's apply is in flight, then lets it land."""
    a, ds = approved()

    async def scenario():
        release = SlowRelease()
        r = rig((a, ds), release=release)
        first = asyncio.create_task(r.dispatcher.execute(a.action_id))
        await release.sent.wait()
        seen = await repeat(r, a)
        state, blocked = r.ledger.get(a.action_id).state, r.ledger.unresolved(a.target.deployment)
        release.go.set()
        return r, a, seen, state, blocked, await first

    return asyncio.run(scenario())


def test_st37_the_repeat_leaves_the_row_started_and_the_block_on_and_the_first_call_finishes():
    async def again(r, a):
        return await r.dispatcher.execute(a.action_id)

    r, a, seen, state, blocked, first = repeat_during_apply(again)
    assert seen.outcome == Outcome.UNKNOWN and state == LedgerState.STARTED and blocked
    assert (
        first.outcome == Outcome.APPLIED and r.ledger.get(a.action_id).state == LedgerState.APPLIED
    )
    assert len(r.release.calls) == 1 and not r.ledger.unresolved(a.target.deployment)


def test_st37_a_repeat_from_another_process_inside_the_window_is_held_the_same_way():
    async def elsewhere(r, a):
        return await restarted(r, later=timedelta(seconds=10)).execute(a.action_id)

    _, _, seen, state, blocked, first = repeat_during_apply(elsewhere)
    assert seen.outcome == Outcome.UNKNOWN and state == LedgerState.STARTED and blocked
    assert first.outcome == Outcome.APPLIED


def test_st37_reconcile_inside_the_window_waits():
    async def look(r, a):
        with pytest.raises(ReleaseInProgress):
            await r.dispatcher.reconcile(a.action_id)

    assert repeat_during_apply(look)[3] == LedgerState.STARTED


def no_resend(r, a):
    """A stop request on the incident: ST-38's same-key re-send (T4.1) needs every pre-check to
    pass, so the late look settles instead, which is the case these tests are about."""
    r.store.stops[a.incident_id] = "user:lee"


def test_st37_a_first_call_that_outlives_the_window_never_raises_and_a_conflict_needs_a_person():
    async def late_look(r, a):
        no_resend(r, a)
        return await restarted(r).execute(a.action_id)  # past the window, the row is taken for dead

    r, a, seen, state, _, first = repeat_during_apply(late_look)
    assert (
        seen.outcome == Outcome.NOT_APPLIED and state == LedgerState.NOT_APPLIED
    )  # what the look could see
    assert r.release.revision == AFTER
    assert (
        first.outcome == Outcome.UNKNOWN and "a person must check" in first.reason
    )  # the change landed: said aloud


def test_st37_a_late_apply_puts_the_block_back_and_only_a_person_clears_it():
    """ADR-69: reconciliation settled the key as not_applied, then the first call landed. No attempt row can
    follow not_applied, so the block is its own record."""

    async def late_look(r, a):
        no_resend(r, a)
        return await restarted(r).execute(a.action_id)

    r, a, _, _, _, _ = repeat_during_apply(late_look)
    r.store.stops.clear()
    assert (
        r.ledger.get(a.action_id).state == LedgerState.NOT_APPLIED
    )  # the attempt rows can't say it
    assert [(b.execution_key, b.reason) for b in r.ledger.unresolved(a.target.deployment)] == [
        (a.action_id, "late_apply")
    ]
    other = make_action(base_revision=AFTER, expected_after_revision="0badf00d")
    r.store.add(other, [decide(other, "priya")])
    d = restarted(r)
    refused = asyncio.run(d.execute(other.action_id))
    assert (
        refused.outcome == Outcome.REFUSED
        and "unresolved change" in refused.reason
        and len(r.release.calls) == 1
    )
    asyncio.run(d.reconcile(a.action_id))  # no later look lifts it
    assert r.ledger.unresolved(a.target.deployment)
    with pytest.raises(ApprovalError) as e:
        d.resolve_outcome(
            a.action_id,
            subject="priya",
            outcome="applied",
            observed_revision=AFTER,
            reason="Checked the target by hand",
            auth_time=SOON,
            mfa=True,
        )
    assert e.value.code == "forbidden" and r.ledger.unresolved(a.target.deployment)
    resolved = d.resolve_outcome(
        a.action_id,
        subject="sam",
        outcome="applied",
        observed_revision=AFTER,
        reason="Checked the target by hand",
        auth_time=SOON,
        mfa=True,
    )
    assert resolved.outcome == Outcome.APPLIED and r.ledger.unresolved(a.target.deployment) == []
    assert (
        r.ledger.blocks[-1].resolution.subject == "sam" and len(r.ledger.rows[a.action_id]) == 2
    )  # rows stay final
    assert asyncio.run(d.execute(other.action_id)).outcome == Outcome.APPLIED


def test_st37_control_a_call_that_lands_in_time_leaves_no_block():
    a, ds = approved()
    r = rig((a, ds))
    assert (
        r.run(a.action_id).outcome == Outcome.APPLIED
        and r.ledger.unresolved(a.target.deployment) == []
    )
    assert [b.reason for b in r.ledger.blocks] == ["unresolved"] and r.ledger.blocks[0].cleared


def test_st37_a_late_look_while_the_release_runs_the_key_leaves_the_row_to_the_live_call():
    """T4.1: the look asks the release process first. It says the key's call is running, so the
    row isn't taken for dead: it stays `started`, and the live call settles it."""

    async def late_look(r, a):
        no_resend(r, a)
        r.release.in_progress_key = a.action_id
        with pytest.raises(ReleaseInProgress):
            await restarted(r).reconcile(a.action_id)
        r.release.in_progress_key = None

    r, a, _, state, _, first = repeat_during_apply(late_look)
    assert state == LedgerState.STARTED and first.outcome == Outcome.APPLIED
    assert r.ledger.get(a.action_id).state == LedgerState.APPLIED and len(r.release.calls) == 1


def test_st37_ledger_refuses_a_second_applied_row():
    a, ds = approved()
    r = rig((a, ds))
    r.run(a.action_id)
    entry = r.ledger.get(a.action_id)
    with pytest.raises(RuntimeError):
        r.ledger.put(
            LedgerEntry(
                entry.execution_key,
                entry.action_digest,
                entry.deployment,
                entry.approval_id,
                2,
                LedgerState.APPLIED,
            )
        )


def test_st37_two_approvers_on_one_action_are_one_execution():
    a = make_action(high_impact=True)
    r = rig((a, [decide(a, "priya"), decide(a, "sam")]))
    r.run(a.action_id), r.run(a.action_id)
    assert len(r.release.calls) == 1


def test_st37_repeated_approval_signals_are_ignored(monkeypatch):
    monkeypatch.setattr(wf.workflow, "now", lambda: T0.astimezone())
    w = IncidentWorkflow()
    w.awaited = ("a1", "d" * 64)
    w.approval_decided(ApprovalSignal("ap-1", "a1", "approved", "d" * 64, "priya"))
    w.approval_decided(
        ApprovalSignal("ap-1", "a1", "revoked", "d" * 64, "priya")
    )  # same ID: duplicate delivery
    assert w._valid_approvals() == 1


def test_st37_a_signal_for_another_action_or_digest_is_ignored(monkeypatch):
    monkeypatch.setattr(wf.workflow, "now", lambda: T0.astimezone())
    w = IncidentWorkflow()
    w.awaited = ("a1", "d" * 64)
    w.approval_decided(ApprovalSignal("ap-1", "a2", "approved", "d" * 64, "priya"))
    w.approval_decided(ApprovalSignal("ap-2", "a1", "approved", "e" * 64, "priya"))
    assert w._valid_approvals() == 0


# ---------------------------------------------------------------- ST-38
class DiesBeforeSending(FakeRelease):
    async def apply(self, *args):
        raise Killed()


class DiesAfterApplying(FakeRelease):
    async def apply(self, *args):
        await super().apply(*args)
        raise Killed()


def crash(r, action_id):
    with pytest.raises(Killed):
        r.run(action_id)


def test_st38_intent_is_recorded_before_the_write():
    a, ds = approved()
    r = rig((a, ds), release=DiesBeforeSending())
    crash(r, a.action_id)
    assert r.ledger.get(a.action_id).state == LedgerState.STARTED
    assert r.ledger.action_status[a.action_id] == "applying"


def test_st38_killed_before_sending_is_resent_with_the_same_key_while_applying():
    """T4.1: the release process never saw the key, the target is at base and the approval
    stands, so the restarted dispatcher sends again, same key, same attempt: applied once."""
    a, ds = approved()
    r = rig((a, ds), release=DiesBeforeSending())
    crash(r, a.action_id)
    r.release.__class__ = FakeRelease  # the release process is healthy now
    d = restarted(r)
    assert asyncio.run(d.execute(a.action_id)).outcome == Outcome.APPLIED
    assert [c["key"] for c in r.release.calls] == [a.action_id]
    rows = r.ledger.rows[a.action_id]
    assert len(rows) == 1 and rows[0].state == LedgerState.APPLIED
    assert "re-sent with the same key (ST-38)" in rows[0].notes
    assert asyncio.run(d.execute(a.action_id)).reason == "already applied (idempotent)"


def test_st38_killed_twice_in_a_row_is_still_applied_once():
    a, ds = approved()
    r = rig((a, ds), release=DiesBeforeSending())
    crash(r, a.action_id)
    with pytest.raises(Killed):  # the restarted dispatcher dies on its re-send too
        asyncio.run(restarted(r).execute(a.action_id))
    r.release.__class__ = FakeRelease
    d = restarted(r, later=timedelta(minutes=4))
    assert asyncio.run(d.execute(a.action_id)).outcome == Outcome.APPLIED
    assert len(r.release.calls) == 1 and len(applied_rows(r, a.action_id)) == 1


def test_st38_killed_before_sending_with_the_approval_gone_settles_not_applied_and_stays_final():
    """No re-send unless the approval still stands: here it was revoked by expiry of the
    window (the restart is a day later). Then not_applied, and final."""
    a, ds = approved()
    r = rig((a, ds), release=DiesBeforeSending())
    crash(r, a.action_id)
    r.release.__class__ = FakeRelease
    d = restarted(r, later=timedelta(days=1))
    assert asyncio.run(d.execute(a.action_id)).outcome == Outcome.NOT_APPLIED
    again = asyncio.run(d.execute(a.action_id))
    assert (
        again.outcome == Outcome.NOT_APPLIED
        and "new action" in again.reason
        and r.release.calls == []
    )


def test_st38_killed_before_sending_with_the_target_moved_is_not_resent():
    a, ds = approved()
    r = rig((a, ds), release=DiesBeforeSending())
    crash(r, a.action_id)
    r.release.__class__ = FakeRelease
    r.release.revision = "c0ffee00"  # someone changed the target meanwhile
    assert asyncio.run(restarted(r).execute(a.action_id)).outcome == Outcome.UNKNOWN
    assert r.release.calls == []


def test_st38_killed_after_apply_before_confirmation_goes_on_as_applied_with_no_resend():
    a, ds = approved()
    r = rig((a, ds), release=DiesAfterApplying())
    crash(r, a.action_id)
    assert r.ledger.get(a.action_id).state == LedgerState.STARTED and r.release.revision == AFTER
    d = restarted(r)
    assert asyncio.run(d.execute(a.action_id)).outcome == Outcome.APPLIED
    assert len(r.release.calls) == 1 and len(applied_rows(r, a.action_id)) == 1
    assert r.ledger.get(a.action_id).reconciliation is True


def unknown_case():
    """After the crash, neither the key nor the live revision settles it."""
    a, ds = approved()
    r = rig((a, ds), release=DiesAfterApplying())
    crash(r, a.action_id)
    r.release.applied_keys.clear()
    r.release.revision = "deadbeef"
    return a, r


def test_st38_unknown_stops_and_blocks_the_deployment_until_a_person_clears_it():
    a, r = unknown_case()
    d = restarted(r)
    assert asyncio.run(d.execute(a.action_id)).outcome == Outcome.UNKNOWN
    other = make_action(base_revision="deadbeef")
    r.store.add(other, [decide(other, "priya")])
    r.release.__class__ = FakeRelease
    assert asyncio.run(d.execute(other.action_id)).outcome == Outcome.REFUSED
    assert len(r.release.calls) == 1  # only the crashed one
    resolved = d.resolve_outcome(
        a.action_id,
        subject="sam",
        outcome="not_applied",
        observed_revision="deadbeef",
        reason="Checked the target by hand",
        auth_time=SOON,
        mfa=True,
    )
    assert resolved.outcome == Outcome.NOT_APPLIED
    assert asyncio.run(d.execute(other.action_id)).outcome == Outcome.APPLIED


@pytest.mark.parametrize("subject", ["priya", "vee", "admin"])
def test_st38_only_an_incident_lead_can_clear_an_unknown_outcome(subject):
    a, r = unknown_case()
    d = restarted(r)
    asyncio.run(d.execute(a.action_id))
    with pytest.raises(ApprovalError) as e:
        d.resolve_outcome(
            a.action_id,
            subject=subject,
            outcome="applied",
            observed_revision="deadbeef",
            reason="Checked the target by hand",
            auth_time=SOON,
            mfa=True,
        )
    assert e.value.code == "forbidden"


def test_st38_a_restart_inside_the_window_cannot_call_the_first_attempt_dead():
    a, ds = approved()
    r = rig((a, ds), release=DiesBeforeSending())
    crash(r, a.action_id)
    assert (
        asyncio.run(restarted(r, later=timedelta(seconds=10)).execute(a.action_id)).outcome
        == Outcome.UNKNOWN
    )
    assert r.ledger.get(a.action_id).state == LedgerState.STARTED and r.ledger.unresolved(
        a.target.deployment
    )


def test_st38_release_still_running_at_restart_is_waited_for_not_resent():
    a, ds = approved()
    r = rig((a, ds), release=DiesBeforeSending())
    crash(r, a.action_id)
    r.release.in_progress_key = a.action_id
    with pytest.raises(ReleaseInProgress):
        asyncio.run(restarted(r).reconcile(a.action_id))
    assert r.release.calls == []


def test_st38_restored_database_asks_the_target_not_its_own_ledger():
    rec = ActionRecord("a1", "d", "pending", AFTER, BASE)
    rep = reconcile_after_restore([rec], lambda d, k: "applied", lambda d: AFTER)
    assert rep.resolved == {"a1": "applied"}


# ---------------------------------------------------------------- ST-40
class Liar(FakeRelease):
    """Says applied. The live revision says otherwise."""

    async def apply(self, deployment, execution_key, base_revision, patch_sha256, to_revision):
        self.calls.append(execution_key)
        self.applied_keys[execution_key] = "applied"
        self.revision = "evil0000"
        return "applied"


def test_st40_success_with_the_wrong_live_revision_is_unknown_and_blocks():
    a, ds = approved()
    r = rig((a, ds), release=Liar())
    res = r.run(a.action_id)
    assert res.outcome == Outcome.UNKNOWN and r.ledger.get(a.action_id).state == LedgerState.UNKNOWN
    assert r.ledger.action_status[a.action_id] == "outcome_unknown"
    other = make_action(base_revision="evil0000")
    r.store.add(other, [decide(other, "priya")])
    assert r.run(other.action_id).outcome == Outcome.REFUSED


def test_st40_retry_does_not_turn_a_lie_into_applied():
    a, ds = approved()
    r = rig((a, ds), release=Liar())
    r.run(a.action_id)
    assert r.run(a.action_id).outcome == Outcome.UNKNOWN and len(r.release.calls) == 1


def test_st40_unreadable_target_after_apply_is_unknown():
    class Blind(FakeRelease):
        async def current_state(self, deployment):
            if self.calls:
                raise ConnectionError("gone")
            return await super().current_state(deployment)

    a, ds = approved()
    r = rig((a, ds), release=Blind())
    assert r.run(a.action_id).outcome == Outcome.UNKNOWN


def test_st40_out_of_band_change_refuses_the_next_action():
    a, ds = approved()
    r = rig((a, ds), release=FakeRelease(revision="outofband"))
    assert r.run(a.action_id).outcome == Outcome.REFUSED and r.release.calls == []


def test_st40_control_an_honest_release_is_applied():
    a, ds = approved()
    assert rig((a, ds)).run(a.action_id).outcome == Outcome.APPLIED


def test_st40_the_release_reconciliation_flags_every_change_no_attempt_carries():
    """T3.11: the daily comparison of the change history with execution_attempts. A platform change
    whose key an attempt carries is matched; an out-of-band change (no key) and a keyed change no
    attempt carries are not, and page the Security lead (AisreReleaseChangeUnmatched). The state the
    history starts from isn't a change, and nothing older than the window is looked at again.
    Against PostgreSQL and the fake release process: tests/integration/test_reconciliation.py."""
    from datetime import UTC, datetime

    from aisre.remediation.dispatcher import ReleaseChange, unmatched_changes

    t = datetime(2026, 11, 19, 12, tzinfo=UTC)
    dep = "orders-api-production"
    history = [
        ReleaseChange(dep, t - timedelta(days=3), "manual", "old", None),
        ReleaseChange(dep, t, "start", "r0", None),
        ReleaseChange(dep, t + timedelta(minutes=1), "apply", "r1", "a1"),
        ReleaseChange(dep, t + timedelta(minutes=2), "manual", "r1+manual", None),
        ReleaseChange(dep, t + timedelta(minutes=3), "apply", "r2", "forged-key"),
        ReleaseChange(dep, t + timedelta(minutes=4), "undo", "r0", "rb1"),
    ]
    found = unmatched_changes(history, {"a1", "rb1"}, t - timedelta(hours=25))
    assert [(c.kind, c.revision) for c in found] == [("manual", "r1+manual"), ("apply", "r2")]
    assert unmatched_changes(history[2:3] + history[5:], {"a1", "rb1"}, t) == []
