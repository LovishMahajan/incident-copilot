"""Artifact 11.3–11.5, 13.5 T2.6, T2.7 (ADR-71): the Test Runner. `python -m aisre.remediation.test_runner`.

The only process that holds the Docker socket, which is root on its host (ADR-71): never the API,
never the worker that runs model-facing code. It polls its own task queue,
`activities.TEST_RUNNER_QUEUE`, and serves two activities there: `run_sandbox_test` and the
reaper's `reap_sandboxes`, which runs every 5 minutes as a BudgetJobWorkflow timer job
(aisre.workflow.budget_jobs; the time-skipping test server has no Schedule API). Locally it runs
on the host; in production on the test host (ADR-51).

`run_sandbox_test` (TestIn -> TestOut):

1. Reads the action's patch: the `patches` row's `patch_diff` artifact holds
   `Patch.canonical_bytes()`, in the object store under the artifact's storage key; its SHA-256
   must equal `patches.patch_sha256` and TestIn.patch_sha256, and the action must be `testing`.
2. Refuses a patch touching a protected path before anything starts (sandbox.refuse_protected),
   by the same rule as the draft (patch.check_paths, ADR-72), with what the reverted commit
   changed read from its own mirror.
3. Checks out the exact base commit from its mirror (`AISRE_SANDBOX_REPOS`), with `git archive`.
4. Takes one of three slots across every Test Runner process (PgSandboxSlots), then runs the
   sandbox: the harness reproduces first and tests the patch only if the failure appears. The
   queued wait and the run share BUDGET_S, inside the activity's 900 s.
5. Classifies the bundle (sandbox.classify: passed, failed, not_reproduced, inconclusive,
   timeout, crashed) and stores the test run with its isolation label and
   `aisre.action.tested.v1`, in one transaction, keyed by TestIn.test_key.

It heartbeats every few seconds. On cancellation it kills the container, waits for the run's own
cleanup, and lets the cancellation through: nothing is stored for a cancelled run. A run that
never got a slot is `inconclusive` (Artifact 11.5: something required has no result).

Connects to PostgreSQL as aisre_app and reads the object store until a Test Runner role exists
(M4, ADR-71). Settings: AISRE_SANDBOX_RUNTIME, AISRE_SANDBOX_IMAGE, AISRE_SANDBOX_REPOS.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import Connection, Engine, text
from temporalio import activity
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from aisre.config import Settings
from aisre.contracts import ActionTestedData
from aisre.evidence.snapshots import S3Snapshots, SnapshotFailed
from aisre.record import make_engine, uuid7
from aisre.record.incidents import append_event
from aisre.reliability import telemetry
from aisre.remediation import sandbox as sb
from aisre.remediation.patch import PatchRejected
from aisre.workflow import activities as act
from aisre.workflow.budget_jobs import BudgetJobIn, BudgetJobWorkflow

ACTIVITY_TIMEOUT_S = 900  # the workflow's start_to_close for run_sandbox_test
CLEANUP_MARGIN_S = 60  # left for the stop, the kill and the removal
BUDGET_S = ACTIVITY_TIMEOUT_S - CLEANUP_MARGIN_S  # the queued wait and the run, together
HEARTBEAT_EVERY_S = 5.0  # the workflow's heartbeat timeout is 20 s
REAPER_JOB = "aisre-sandbox-reaper"
SLOT_LOCK_CLASS = 0x4153  # advisory-lock key space of the sandbox slots
SOURCE = "/aisre/test-runner"
ACTOR = "system:test-runner"

log = logging.getLogger("aisre.test_runner")


# ---------------------------------------------------------------------------
# Three at once, across processes (ADR-56, ADR-71)
# ---------------------------------------------------------------------------
class PgSandboxSlots:
    """MAX_SANDBOXES slots as PostgreSQL session advisory locks, each held on its own connection
    for the whole run, so the cap holds across Test Runner processes and a process that dies
    frees its slot with its connection. Same methods as sandbox.SandboxLimiter; the lock is held
    by the thread that took it, which is the one that releases it (run_sandbox)."""

    def __init__(self, engine: Engine, limit: int = sb.MAX_SANDBOXES) -> None:
        self.engine, self.limit = engine, limit
        self._held = threading.local()

    def acquire(self, wait_s: float, cancel: threading.Event | None = None) -> bool:
        conn = self.engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        deadline = time.monotonic() + wait_s
        try:
            while True:
                for slot in range(self.limit):
                    got = conn.execute(
                        text("SELECT pg_try_advisory_lock(:c, :s)"),
                        {"c": SLOT_LOCK_CLASS, "s": slot},
                    ).scalar_one()
                    if got:
                        self._held.conn, self._held.slot = conn, slot
                        return True
                if time.monotonic() >= deadline or (cancel is not None and cancel.is_set()):
                    conn.close()
                    return False
                time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
        except BaseException:
            conn.close()
            raise

    def release(self) -> None:
        conn, slot = self._held.conn, self._held.slot
        self._held.conn = None
        try:
            conn.execute(
                text("SELECT pg_advisory_unlock(:c, :s)"), {"c": SLOT_LOCK_CLASS, "s": slot}
            )
        except BaseException:
            conn.invalidate()  # never back to the pool still holding the lock
            raise
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# The patch, as stored
# ---------------------------------------------------------------------------
class BlobReader(Protocol):
    def get(self, key: str) -> bytes: ...


@dataclass(frozen=True)
class StoredPatch:
    incident_id: UUID
    action_id: UUID
    patch_sha256: str
    canonical: bytes  # Patch.canonical_bytes(), what the harness applies and hashes

    @property
    def content(self) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.canonical)
        return loaded


def refused(kind: str, detail: str) -> ApplicationError:
    """Nothing to retry: the workflow hands over (NO_RETRY on run_sandbox_test)."""
    return ApplicationError(detail, type=kind, non_retryable=True)


class PgPatchSource:
    """Reads the patch an action names, and checks it is the one the workflow means."""

    def __init__(self, engine: Engine, blobs: BlobReader) -> None:
        self.engine, self.blobs = engine, blobs

    def load(self, action_id: str, patch_sha256: str) -> StoredPatch:
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    text(
                        "SELECT a.incident_id, a.status, p.patch_sha256, ar.storage_key "
                        "FROM actions a JOIN patches p ON p.action_id = a.action_id "
                        "JOIN artifacts ar ON ar.artifact_id = p.diff_artifact_id "
                        "WHERE a.action_id = :a"
                    ),
                    {"a": action_id},
                )
                .mappings()
                .first()
            )
        if row is None:
            raise refused("PatchMissing", f"action {action_id} has no stored patch")
        if str(row["status"]) != "testing":
            raise refused("ActionNotTesting", f"action {action_id} is {row['status']}")
        stored = str(row["patch_sha256"]).strip()
        try:
            body = self.blobs.get(str(row["storage_key"]))
        except SnapshotFailed as e:
            raise ApplicationError(str(e), type="PatchUnreadable") from None
        if not stored == patch_sha256 == hashlib.sha256(body).hexdigest():
            raise refused("PatchChanged", "the stored patch is not the one the workflow names")
        return StoredPatch(UUID(str(row["incident_id"])), UUID(action_id), stored, body)


# ---------------------------------------------------------------------------
# The activities
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Prepared:
    run_id: str
    commit: str
    payload: bytes


class PatchSource(Protocol):
    def load(self, action_id: str, patch_sha256: str) -> StoredPatch: ...


class TestRunner:
    """`run_sandbox_test` and `reap_sandboxes`, bound to the spec, mirrors and database."""

    __test__ = False

    def __init__(
        self,
        engine: Engine,
        patches: PatchSource,
        spec: sb.SandboxSpec,
        repos: dict[str, Path],
        *,
        limiter: Any = None,
        run: Callable[..., Any] = subprocess.run,
        popen: Callable[..., Any] = subprocess.Popen,
        budget_s: float = BUDGET_S,
        reap_after_s: float | None = None,
    ) -> None:
        self.engine, self.patches, self.spec, self.repos = engine, patches, spec, repos
        self.limiter = limiter if limiter is not None else PgSandboxSlots(engine)
        self.run, self.popen, self.budget_s = run, popen, budget_s
        # T2.7: past its wall clock plus a margin, and never past 60 minutes (FR-23)
        self.reap_after_s = min(
            reap_after_s if reap_after_s is not None else spec.wall_clock_s + sb.REAP_MARGIN_S,
            sb.REAP_AGE_S,
        )

    def all(self) -> list[Callable[..., Any]]:
        return [self.run_sandbox_test, self.reap_sandboxes]

    def prepare(self, patch: StoredPatch) -> Prepared:
        """Everything before a slot: refusal, checkout, payload. Nothing is started yet."""
        content = patch.content
        repository = str(content.get("repository", ""))
        plan, repo = sb.HARNESS_PLANS.get(repository), self.repos.get(repository)
        reverted = content.get("reverted_commit")
        revert = None
        if repo is not None and reverted:
            try:  # what the reverted commit changed, from this process's own mirror (ADR-72)
                revert = sb.revert_facts(repo, str(reverted), run=self.run)
            except (sb.SpecRejected, subprocess.CalledProcessError):
                revert = None  # unreadable: a protected path is then refused below
        try:
            restored = sb.refuse_protected(content, revert)
        except PatchRejected as e:
            raise refused("PatchRejected", str(e)) from None
        if plan is None or repo is None:
            raise refused("NoRepository", f"no mirror or harness plan for {repository!r}")
        run_id = uuid7().hex
        try:
            commit, tar = sb.checkout(repo, str(content.get("base_revision", "")), run=self.run)
            payload = sb.build_payload(
                self.spec, run_id, commit, tar, patch.canonical, plan, restored
            )
        except (sb.SpecRejected, subprocess.CalledProcessError) as e:
            raise refused("CheckoutFailed", f"{type(e).__name__}: no checkout") from None
        return Prepared(run_id, commit, payload)

    def launch(
        self, patch: StoredPatch, prepared: Prepared, cancel: threading.Event
    ) -> sb.ResultBundle | None:
        return sb.run_sandbox(
            self.spec,
            prepared.run_id,
            patch.patch_sha256,
            prepared.commit,
            popen=self.popen,
            run=self.run,
            limiter=self.limiter,
            payload=prepared.payload,
            budget_s=self.budget_s,
            cancel=cancel,
        )

    @activity.defn(name="run_sandbox_test")
    async def run_sandbox_test(self, inp: act.TestIn) -> act.TestOut:
        earlier = await asyncio.to_thread(self.stored_result, inp.test_key)
        if earlier is not None:
            return earlier
        patch = await asyncio.to_thread(self.patches.load, inp.action_id, inp.patch_sha256)
        prepared = await asyncio.to_thread(self.prepare, patch)
        started, cancel = datetime.now(UTC), threading.Event()
        task = asyncio.ensure_future(asyncio.to_thread(self.launch, patch, prepared, cancel))
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=HEARTBEAT_EVERY_S)
                activity.heartbeat()
            bundle: sb.ResultBundle | None = task.result()
            outcome = sb.classify(bundle, patch.patch_sha256)
        except sb.SandboxBusy:
            bundle, outcome = None, sb.TestOutcome.INCONCLUSIVE  # no slot in time: no result
        except asyncio.CancelledError:
            cancel.set()
            while not task.done():  # WR-15: the container dies now, then the run cleans up
                await asyncio.to_thread(sb.kill, prepared.run_id, run=self.run)
                await asyncio.wait({task}, timeout=1)
            raise
        return await asyncio.to_thread(
            self.store, inp, patch, prepared, bundle, outcome, started, datetime.now(UTC)
        )

    def stored_result(self, test_key: str, conn: Connection | None = None) -> act.TestOut | None:
        if conn is None:
            with self.engine.connect() as own:
                return self.stored_result(test_key, own)
        row = (
            conn.execute(
                text(
                    "SELECT test_run_id, reproduced, outcome, isolation FROM test_runs "
                    "WHERE test_key = :k"
                ),
                {"k": test_key},
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        return act.TestOut(
            str(row["test_run_id"]),
            bool(row["reproduced"]),
            row["outcome"],
            isolation=str(row["isolation"]),
        )

    def store(
        self,
        inp: act.TestIn,
        patch: StoredPatch,
        prepared: Prepared,
        bundle: sb.ResultBundle | None,
        outcome: sb.TestOutcome,
        started: datetime,
        finished: datetime,
    ) -> act.TestOut:
        """The test run and its event, in one transaction (Artifact 7: test_runs is append-only)."""
        label = sb.isolation(self.spec)
        reproduced = bundle is not None and bundle.reproduction_before is True
        test_run_id = uuid7()
        with self.engine.begin() as conn:
            conn.execute(
                text("SELECT 1 FROM incidents WHERE incident_id = :i FOR UPDATE"),
                {"i": patch.incident_id},
            )
            found = self.stored_result(inp.test_key, conn)
            if found is not None:  # a second write of the same run finds the first
                return found
            conn.execute(
                text(
                    "INSERT INTO test_runs (test_run_id, action_id, patch_sha256, reproduced, "
                    "outcome, started_at, finished_at, isolation, test_key, bundle_digest) "
                    "VALUES (:t, :a, :p, :r, CAST(:o AS test_outcome), :s, :f, :iso, :k, :d)"
                ),
                {
                    "t": test_run_id,
                    "a": patch.action_id,
                    "p": patch.patch_sha256,
                    "r": reproduced,
                    "o": outcome.value,
                    "s": started,
                    "f": max(finished, started),
                    "iso": label,
                    "k": inp.test_key,
                    "d": bundle.digest if bundle is not None else None,
                },
            )
            append_event(
                conn,
                patch.incident_id,
                "aisre.action.tested.v1",
                ACTOR,
                ActionTestedData(
                    action_id=patch.action_id,
                    test_run_id=test_run_id,
                    patch_sha256=patch.patch_sha256,
                    reproduced=reproduced,
                    outcome=outcome.value,
                    isolation=label,
                ),
                occurred_at=finished,
                source=SOURCE,
            )
        telemetry.sandbox_runs.labels(outcome=outcome.value).inc()
        log.info(
            "test run %s for action %s: %s (%s, run %s)",
            test_run_id,
            patch.action_id,
            outcome.value,
            label,
            prepared.run_id,
        )
        return act.TestOut(str(test_run_id), reproduced, outcome.value, isolation=label)

    @activity.defn(name="reap_sandboxes")
    async def reap_sandboxes(self) -> int:
        """T2.7: remove whatever is past its wall clock plus REAP_MARGIN_S (at most 60 minutes),
        and set aisre_sandboxes_leftover to how many there were."""
        left = await asyncio.to_thread(
            sb.reap, datetime.now(UTC), run=self.run, max_age_s=self.reap_after_s
        )
        telemetry.sandboxes_leftover.set(left)
        if left:
            log.warning("reaper removed %d sandbox containers or volumes left over", left)
        return left


# ---------------------------------------------------------------------------
# The reaper job and the process
# ---------------------------------------------------------------------------
async def ensure_reaper(client: Client, every_s: float = sb.REAP_INTERVAL_S) -> None:
    """Starts the reaper's timer job on the Test Runner's queue, if it isn't running."""
    await client.start_workflow(
        BudgetJobWorkflow.run,
        BudgetJobIn("reap_sandboxes", every_s),
        id=REAPER_JOB,
        task_queue=act.TEST_RUNNER_QUEUE,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
    )


def from_settings(settings: Settings, engine: Engine) -> TestRunner:
    if settings.sandbox_image is None:
        raise RuntimeError("AISRE_SANDBOX_IMAGE is required: run ops/sandbox/build.sh")
    spec = sb.sandbox_spec(settings.env, settings.sandbox_runtime, settings.sandbox_image)
    s = settings
    if not (
        s.object_store_url
        and s.object_store_bucket
        and s.object_store_access_key
        and s.object_store_secret_key
    ):
        raise RuntimeError("the Test Runner reads patches from the object store: set it")
    blobs = S3Snapshots(
        s.object_store_url,
        s.object_store_bucket,
        s.object_store_access_key,
        s.object_store_secret_key,
    )
    repos = {name: Path(path) for name, path in (settings.sandbox_repos or {}).items()}
    return TestRunner(engine, PgPatchSource(engine, blobs), spec, repos)


def build_worker(client: Client, runner: TestRunner) -> Worker:
    return Worker(
        client,
        task_queue=act.TEST_RUNNER_QUEUE,
        workflows=[BudgetJobWorkflow],
        activities=runner.all(),
    )


async def run(settings: Settings, stop: asyncio.Event | None = None) -> None:
    stop = stop or asyncio.Event()
    metrics = telemetry.serve_metrics(settings.metrics_port)
    client = await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        data_converter=act.DATA_CONVERTER,
    )
    engine = make_engine(settings.database_url)
    runner = from_settings(settings, engine)
    try:
        async with build_worker(client, runner):
            await ensure_reaper(client)
            log.info(
                "test runner on %s: runtime %s (%s), repositories %s",
                act.TEST_RUNNER_QUEUE,
                runner.spec.runtime,
                sb.isolation(runner.spec),
                sorted(runner.repos),
            )
            await stop.wait()
    finally:
        engine.dispose()
        if metrics is not None:
            metrics.shutdown()


async def _main(settings: Settings) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await run(settings, stop)


def main(argv: Sequence[str] | None = None) -> int:
    if list(sys.argv[1:] if argv is None else argv):
        print("usage: python -m aisre.remediation.test_runner", file=sys.stderr)
        return 2
    settings = Settings.from_env()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    asyncio.run(_main(settings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
