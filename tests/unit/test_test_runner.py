"""Artifact 13.5 T2.6, T2.7 (ADR-71): the Test Runner's activities, against a recorded `docker`.

The real launcher, harness and database are tests/integration/test_sandbox_test_activity.py. Here:
refusal before anything starts, kill on cancel, a run that got no slot, and the reaper's
5-minute schedule leaving nothing after 60 minutes (time-skipped).
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment

from aisre.reliability import telemetry
from aisre.remediation import sandbox as sb
from aisre.remediation import test_runner as tr
from aisre.workflow import activities as act

TEST_SERVER = os.environ.get("TEMPORAL_TEST_SERVER")
IMAGE = "sha256:" + "b" * 64
SPEC = sb.sandbox_spec("test", "runc", IMAGE)


def canonical(path: str) -> bytes:
    return json.dumps(
        {
            "repository": "orders-api",
            "base_revision": "c" * 40,
            "files": [{"path": path, "op": "modify", "hunks": []}],
        },
        sort_keys=True,
    ).encode()


class Patches:
    def __init__(self, body: bytes) -> None:
        self.stored = tr.StoredPatch(uuid4(), uuid4(), hashlib.sha256(body).hexdigest(), body)

    def load(self, action_id: str, patch_sha256: str) -> tr.StoredPatch:
        return self.stored


class Runner(tr.TestRunner):
    """The real activity, with the record in memory."""

    def __init__(self, body: bytes, **kw) -> None:
        super().__init__(None, Patches(body), SPEC, {"orders-api": "/mirror"}, **kw)  # type: ignore[arg-type]
        self.stored: list[tuple[sb.TestOutcome, sb.ResultBundle | None]] = []

    def stored_result(self, test_key, conn=None):
        return None

    def store(self, inp, patch, prepared, bundle, outcome, started, finished):
        self.stored.append((outcome, bundle))
        return act.TestOut("t1", bool(bundle and bundle.reproduction_before), outcome.value)


class Docker:
    """A recorded `docker` and `git`. `docker run` blocks until `docker kill` names it."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.killed = threading.Event()

    def run(self, args, **kw):
        self.calls.append(list(args))
        out: str | bytes = ""
        if args[:1] == ["git"] and "rev-parse" in args:
            out = "c" * 40 + "\n"
        elif args[:1] == ["git"]:
            out = b"checkout-tar"
        elif args[:2] == ["docker", "kill"]:
            self.killed.set()
        return subprocess.CompletedProcess(args, 0, out, "")

    def popen(self, args, **kw):
        docker = self

        class Proc:
            returncode = None

            def communicate(self, timeout=None, input=None):
                docker.calls.append(list(args[:2]))
                docker.killed.wait(30)
                self.returncode = 137
                return b"", b""

        return Proc()


IN = act.TestIn("a1", "0" * 64, test_key="a1:test:1")


@pytest.mark.asyncio
async def test_a_patch_touching_tests_is_refused_before_anything_starts():
    docker = Docker()
    runner = Runner(canonical("tests/unit/test_pricing.py"), run=docker.run, popen=docker.popen)
    with pytest.raises(ApplicationError) as e:
        await ActivityEnvironment().run(runner.run_sandbox_test, IN)
    assert e.value.type == "PatchRejected" and e.value.non_retryable
    assert docker.calls == [] and runner.stored == []


@pytest.mark.asyncio
async def test_cancel_kills_the_sandbox_and_cleans_up_before_the_activity_ends():
    docker = Docker()
    runner = Runner(
        canonical("deploy/production.env"),
        run=docker.run,
        popen=docker.popen,
        limiter=sb.SandboxLimiter(),
    )
    env = ActivityEnvironment()
    heartbeats: list[object] = []
    env.on_heartbeat = lambda *d: heartbeats.append(d)

    async def cancel_once_running():
        while ["docker", "run"] not in docker.calls:
            await asyncio.sleep(0.01)
        env.cancel()

    canceller = asyncio.create_task(cancel_once_running())
    with pytest.raises(asyncio.CancelledError):
        await env.run(runner.run_sandbox_test, IN)
    await canceller
    names = [c for c in docker.calls if c[:2] in (["docker", "kill"], ["docker", "rm"])]
    assert names[0][:2] == ["docker", "kill"] and ["docker", "rm"] in [c[:2] for c in names]
    assert any(c[:3] == ["docker", "volume", "rm"] for c in docker.calls)
    assert runner.stored == []  # nothing recorded for a cancelled run
    assert runner.limiter.acquire(0)  # its slot came back


@pytest.mark.asyncio
async def test_no_slot_in_the_budget_is_inconclusive_and_starts_nothing():
    docker = Docker()
    full = sb.SandboxLimiter(1)
    assert full.acquire(0)
    runner = Runner(
        canonical("deploy/production.env"),
        run=docker.run,
        popen=docker.popen,
        limiter=full,
        budget_s=sb.MIN_RUN_S,  # nothing left to wait in
    )
    out = await ActivityEnvironment().run(runner.run_sandbox_test, IN)
    assert out.outcome == "inconclusive" and runner.stored == [(sb.TestOutcome.INCONCLUSIVE, None)]
    assert not [c for c in docker.calls if c[:1] == ["docker"]]


@pytest.mark.asyncio
async def test_the_bundle_becomes_the_outcome():
    body = canonical("deploy/production.env")
    sha = hashlib.sha256(body).hexdigest()
    suites = [dataclasses.asdict(sb.SuiteResult(n, True, True, 1, 0)) for n in sb.REQUIRED_SUITES]
    line = json.dumps(
        {
            "run_id": "r",
            "patch_sha256": sha,
            "base_revision": "c" * 40,
            "image_digest": IMAGE,
            "spec_version": SPEC.spec_version,
            "harness_version": "harness@1",
            "reproduction_before": True,
            "reproduction_after": False,
            "suites": suites,
            "new_error_signatures": [],
            "duration_s": 1.0,
            "timed_out": False,
            "log_artifact_sha256": None,
        }
    )
    docker = Docker()

    class Proc:
        returncode = 0

        def communicate(self, timeout=None, input=None):
            return ("log\n" + line).encode(), b""

    runner = Runner(body, run=docker.run, popen=lambda *a, **k: Proc(), limiter=sb.SandboxLimiter())
    out = await ActivityEnvironment().run(runner.run_sandbox_test, IN)
    assert out.outcome == "passed" and out.reproduced


# ---------------------------------------------------------------- the reaper (T2.7)
class World:
    """Labelled containers and volumes, with creation times, behind a recorded `docker`."""

    def __init__(self) -> None:
        now = datetime.now(UTC)
        self.items = {
            "c-old": ("container", now - timedelta(minutes=50)),
            "v-old": ("volume", now - timedelta(minutes=50)),
            "c-new": ("container", now),
        }
        self.reaps = 0

    def run(self, args, **kw):
        out = ""
        kind = "volume" if args[1] == "volume" else "container"
        verb = args[2] if kind == "volume" else args[1]
        mine = [n for n, (k, _) in self.items.items() if k == kind]
        if verb in ("ps", "ls"):
            out = "\n".join(mine)
            self.reaps += kind == "container"
        elif verb == "inspect":
            names = [a for a in args if a in self.items]
            out = "\n".join(f"{n} {self.items[n][1].isoformat()}" for n in names)
        elif verb == "rm":
            for a in args:
                self.items.pop(a, None)
        return subprocess.CompletedProcess(args, 0, out, "")


@pytest.mark.asyncio
async def test_the_reaper_runs_every_five_minutes_and_nothing_is_left_after_sixty():
    world = World()
    runner = Runner(b"{}", run=world.run, limiter=sb.SandboxLimiter())
    assert runner.reap_after_s == SPEC.wall_clock_s + sb.REAP_MARGIN_S <= sb.REAP_AGE_S
    from temporalio.worker import Worker

    from aisre.workflow.budget_jobs import BudgetJobWorkflow

    async with await WorkflowEnvironment.start_time_skipping(
        test_server_existing_path=TEST_SERVER, data_converter=act.DATA_CONVERTER
    ) as e:
        async with Worker(
            e.client,
            task_queue=act.TEST_RUNNER_QUEUE,
            workflows=[BudgetJobWorkflow],
            activities=[runner.reap_sandboxes],
        ):
            await tr.ensure_reaper(e.client)
            await e.sleep(timedelta(seconds=30))
            assert "c-old" not in world.items and "v-old" not in world.items
            assert "c-new" in world.items  # younger than its wall clock plus the margin
            assert telemetry.sandboxes_leftover._value.get() == 2
            # The young one ages in real time here, not in skipped time, so age it by hand.
            world.items["c-new"] = ("container", datetime.now(UTC) - timedelta(minutes=61))
            await e.sleep(timedelta(minutes=60))
            assert world.items == {} and world.reaps >= 12
            assert telemetry.sandboxes_leftover._value.get() == 0
            await tr.ensure_reaper(e.client)  # on every start: the running job is kept
