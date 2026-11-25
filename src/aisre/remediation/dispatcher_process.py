"""Artifact 6, 11.9–11.11, 13.5 T3.5, T3.16 (ADR-47, ADR-51, ADR-73): the trusted dispatcher
process. `python -m aisre.remediation.dispatcher_process`.

Its own process and its own Compose service, on the `aisre-dispatcher` task queue, serving
four activities and nothing else: `dispatch_change`, `reconcile_execution`, `resolve_outcome`
and, read-only, the daily release reconciliation `reconcile_releases` (T3.11, ST-40), which
compares the release process's change history with `execution_attempts`. It serves /metrics on
AISRE_METRICS_PORT. It connects to PostgreSQL as `aisre_dispatcher` and is the only process that
holds the release credential (`AISRE_RELEASE_URL`, `_TOKEN`); it never holds the Docker socket,
and the worker and the API never hold the release token. It reads the patch bytes from the
object store, holds only the approval verify key, and asks the identity directory for an
approver's current groups at each check (Q11.59). Without the fake release adapter, which
`Settings` refuses in production, it refuses to start: no production write exists (ADR-44).

Each activity builds Layer 11's `dispatcher.Dispatcher` over the PostgreSQL ledger, trusted
store and fence (aisre.record.execution) and runs it in a thread with its own event loop, so the
synchronous database calls never hold the worker's loop. `dispatch_change` is never retried by
the workflow (NO_RETRY); `reconcile_execution` raises dispatcher.ReleaseInProgress while the
release is running, which READ_RETRY waits out. A refused outcome resolution is non-retryable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from sqlalchemy import Engine, text
from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from aisre.config import Settings
from aisre.evidence.snapshots import S3Snapshots
from aisre.record import make_engine
from aisre.record.execution import PgFence, PgLedger, PgTrustedStore
from aisre.reliability import telemetry
from aisre.remediation.action import (
    MATRIX,
    ApprovalError,
    ApprovalPolicy,
    Directory,
    Perm,
    development_verify_keys,
    roles_from_groups,
)
from aisre.remediation.dispatcher import (
    Dispatcher,
    DispatchResult,
    ReleaseProcess,
    unmatched_changes,
)
from aisre.remediation.release import ReleaseRefused, release_from_settings
from aisre.workflow import activities as act

log = logging.getLogger("aisre.dispatcher")
audit = logging.getLogger("aisre.audit")
UNMATCHED_EVENT = "aisre.release.unmatched_change"  # the audit line per unmatched change (ST-40)
DIRECTORY_TIMEOUT_S = 3.0


def fetch_groups(url: str) -> Callable[[str], list[str]]:
    """The identity directory: a subject's current groups. Unreachable or unknown: none, so a
    check fails closed."""

    def groups_of(subject: str) -> list[str]:
        sub = subject.removeprefix("user:")
        try:
            with urllib.request.urlopen(
                f"{url.rstrip('/')}/v1/directory/{quote(sub, safe='')}", timeout=DIRECTORY_TIMEOUT_S
            ) as r:
                found = json.loads(r.read())
        except (urllib.error.URLError, OSError, ValueError):
            return []
        groups = found.get("groups") if isinstance(found, dict) else None
        return [str(g) for g in groups] if isinstance(groups, list) else []

    return groups_of


class GroupDirectory(Directory):
    """action.Directory answered now, from the identity groups (ADR-73)."""

    def __init__(self, groups_of: Callable[[str], list[str]]) -> None:
        super().__init__()
        self.groups_of = groups_of

    def can(self, subject: str, service: str, perm: Perm) -> bool:
        if subject.startswith("system:"):
            return False
        found = roles_from_groups(self.groups_of(subject))
        roles = found.get("*", frozenset()) | found.get(service, frozenset())
        return any(perm in MATRIX[r] for r in roles)


class DispatcherActivities:
    def __init__(
        self,
        engine: Engine,
        release: Callable[[PgTrustedStore], ReleaseProcess],
        store: PgTrustedStore,
        keys: dict[str, Any],
        directory: Directory,
        policy: ApprovalPolicy | None = None,
    ) -> None:
        self.engine, self.store, self.keys, self.directory = engine, store, keys, directory
        self.release = release(store)
        self.policy = policy or ApprovalPolicy()

    def all(self) -> list[Callable[..., Any]]:
        return [
            self.dispatch_change,
            self.reconcile_execution,
            self.resolve_outcome,
            self.reconcile_releases,
        ]

    def dispatcher(self) -> Dispatcher:
        return Dispatcher(
            self.store,
            self.release,
            PgLedger(self.engine),
            self.keys,
            self.directory,
            self.policy,
            lambda: datetime.now(UTC),
            fence=PgFence(self.engine),
        )

    def _out(self, d: Dispatcher, res: DispatchResult) -> act.DispatchOut:
        entry = d.ledger.get(res.execution_key)
        attempt = (entry.attempt_id or "") if entry is not None and res.intent_recorded else ""
        telemetry.dispatch_outcomes.labels(outcome=res.outcome.value).inc()
        return act.dispatch_out(res, attempt)

    @activity.defn(name="dispatch_change")
    async def dispatch_change(self, inp: act.DispatchIn) -> act.DispatchOut:
        d = self.dispatcher()
        res = await asyncio.to_thread(asyncio.run, d.execute(inp.action_id))
        log.info("dispatch %s: %s (%s)", inp.action_id, res.outcome.value, res.reason)
        return self._out(d, res)

    @activity.defn(name="reconcile_execution")
    async def reconcile_execution(self, inp: act.DispatchIn) -> act.DispatchOut:
        d = self.dispatcher()
        res = await asyncio.to_thread(asyncio.run, d.reconcile(inp.action_id))
        return self._out(d, res)

    @activity.defn(name="resolve_outcome")
    async def resolve_outcome(self, inp: act.ResolutionIn) -> act.DispatchOut:
        d = self.dispatcher()

        def run() -> DispatchResult:
            return d.resolve_outcome(
                inp.action_id,
                subject=inp.subject,
                outcome=inp.outcome,
                observed_revision=inp.observed_revision,
                reason=inp.reason,
                auth_time=datetime.fromisoformat(inp.auth_time),
                mfa=inp.mfa,
            )

        try:
            res = await asyncio.to_thread(run)
        except ApprovalError as e:
            raise ApplicationError(str(e), type="Refused", non_retryable=True) from None
        return self._out(d, res)

    @activity.defn(name="reconcile_releases")
    async def reconcile_releases(
        self, inp: act.ReleaseReconciliationIn
    ) -> act.ReleaseReconciliationOut:
        """T3.11, ST-40: each deployment's change history against execution_attempts. An
        unmatched change sets aisre_release_changes_unmatched, which pages the Security lead
        (AisreReleaseChangeUnmatched), and writes one `aisre.release.unmatched_change` audit line.
        Read-only."""
        since = datetime.fromisoformat(inp.since)

        def known() -> tuple[list[str], set[str]]:
            with self.engine.connect() as conn:
                deps = [
                    str(d)
                    for (d,) in conn.execute(
                        text("SELECT DISTINCT target_deployment FROM actions ORDER BY 1")
                    )
                ]
                keys = {
                    str(k)
                    for (k,) in conn.execute(text("SELECT execution_key FROM execution_attempts"))
                }
            return deps, keys

        deployments, keys = await asyncio.to_thread(known)
        history = getattr(self.release, "history", None)
        if history is None:
            raise ApplicationError("this release adapter has no change history", non_retryable=True)
        changes = [c for d in deployments for c in await history(d)]
        found = unmatched_changes(changes, keys, since)
        telemetry.release_changes_unmatched.set(len(found))
        for c in found:
            record = {
                "event": UNMATCHED_EVENT,
                "deployment": c.deployment,
                "at": c.at.isoformat(),
                "kind": c.kind,
                "revision": c.revision,
                "execution_key": c.execution_key,
                "reconciliation": inp.idempotency_key,
            }
            audit.warning(json.dumps(record, sort_keys=True), extra={"aisre": record})
        return act.ReleaseReconciliationOut(
            len(changes),
            [f"{c.deployment} {c.at.isoformat()} {c.kind} {c.revision}" for c in found],
        )


def from_settings(settings: Settings, engine: Engine) -> DispatcherActivities:
    """Refuses to build without each thing the dispatcher needs; in production, always."""
    s = settings
    if s.approval_verify_key is None or s.identity_directory_url is None:
        raise ReleaseRefused(
            "AISRE_APPROVAL_VERIFY_KEY and AISRE_IDENTITY_DIRECTORY_URL are required"
        )
    if not (
        s.object_store_url
        and s.object_store_bucket
        and s.object_store_access_key
        and s.object_store_secret_key
    ):
        raise ReleaseRefused("the dispatcher reads patch bytes from the object store: set it")
    blobs = S3Snapshots(
        s.object_store_url,
        s.object_store_bucket,
        s.object_store_access_key,
        s.object_store_secret_key,
    )
    release_from_settings(settings, lambda sha: b"")  # refuses before anything connects
    return DispatcherActivities(
        engine,
        lambda store: release_from_settings(settings, store.patch_content),
        PgTrustedStore(engine, blobs),
        development_verify_keys(s.approval_verify_key),
        GroupDirectory(fetch_groups(s.identity_directory_url)),
    )


async def run(settings: Settings, stop: asyncio.Event) -> None:
    engine = make_engine(settings.dispatcher_database_url)
    acts = from_settings(settings, engine)
    metrics = telemetry.serve_metrics(settings.metrics_port)
    client = await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        data_converter=act.DATA_CONVERTER,
    )
    try:
        async with Worker(client, task_queue=act.DISPATCHER_QUEUE, activities=acts.all()):
            log.info("dispatcher on %s (fake release process, ADR-73)", act.DISPATCHER_QUEUE)
            await stop.wait()
    finally:
        engine.dispose()
        if metrics is not None:
            metrics.shutdown()


def main(argv: Sequence[str] | None = None) -> int:
    settings = Settings.from_env()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    async def go() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await run(settings, stop)

    try:
        asyncio.run(go())
    except ReleaseRefused as e:
        print(f"dispatcher refused to start: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
