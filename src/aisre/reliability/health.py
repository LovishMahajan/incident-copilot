"""Artifact 12.5, 13.5 T4.5 (WR-22, WR-23): `/readyz` checks and the dependency probes.

Two things, kept apart (12.5's health checks):

- Readiness: can this process do its job right now? The database, the schema version and the
  configuration, plus the approval signing key in the API and the write fence in the
  dispatcher (fenced: not ready to write, but alive). Never Temporal, the model provider or the
  telemetry collector, so a dependency outage doesn't take a process out of service.
  `aisre_process_ready{process}` is 1 when every check passes.
- Dependency probes: every PROBE_EVERY (15 s, provisional) each process probes what it uses
  and sets `aisre_dependency_up{dependency}`: postgresql, temporal, object_store,
  release_process. Each process publishes its own, so a probe still reports while the
  dependency is down. `AisrePostgresDown` (ops/alerts.yml) reads the postgresql one.

A check is an async callable that raises on failure; each runs under PROBE_TIMEOUT. A blocking
check runs in a thread (`blocking`), so the event loop never waits on a dead dependency.
Liveness (`/livez`, the worker's `--check-alive`) is not here: it is only that the process
responds.
"""

from __future__ import annotations

import asyncio
import logging
import urllib.request
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import Engine, text

from aisre.reliability import telemetry

PROBE_EVERY = timedelta(seconds=15)  # provisional (12.1)
PROBE_TIMEOUT = timedelta(seconds=3)
# The Alembic head this code expects. tests/unit/test_health.py holds it to db/migrations.
SCHEMA_HEAD = "0011_m4_schema_version_read"
DEPENDENCIES = ("postgresql", "temporal", "object_store", "release_process")

Check = Callable[[], Awaitable[None]]
log = logging.getLogger(__name__)


class NotReady(Exception):
    """A readiness check's failure, with what failed."""


def blocking(fn: Callable[[], Any]) -> Check:
    """A synchronous check, run in a thread."""

    async def run() -> None:
        await asyncio.to_thread(fn)

    return run


def database(engine: Engine) -> Check:
    def ping() -> None:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))

    return blocking(ping)


def schema_version(engine: Engine, expected: str = SCHEMA_HEAD) -> Check:
    """The database is at the migration this code was built for (aisre_migrations schema)."""

    def read() -> None:
        with engine.connect() as conn:
            got = conn.execute(
                text("SELECT version_num FROM aisre_migrations.alembic_version")
            ).scalar()
        if got != expected:
            raise NotReady(f"schema at {got}, code expects {expected}")

    return blocking(read)


def write_fence(engine: Engine) -> Check:
    """The dispatcher's readiness to write: no global fence reason set (12.5). A fence on one
    deployment stops writes there only, so it doesn't count. Reads as aisre_dispatcher."""

    def read() -> None:
        with engine.connect() as conn:
            active = conn.execute(
                text(
                    "SELECT reason FROM fence_reasons "
                    "WHERE cleared_at IS NULL AND scope = 'global' ORDER BY reason"
                )
            ).scalars()
            reasons = [str(r) for r in active]
        if reasons:
            raise NotReady(f"writes fenced: {', '.join(reasons)}")

    return blocking(read)


def present(name: str, value: object) -> Check:
    """A configuration value this process can't work without."""

    async def check() -> None:
        if not value:
            raise NotReady(f"{name} is not configured")

    return check


def http_ok(url: str) -> Check:
    """GET `url` answers 2xx (the fake release process's /healthz)."""

    def get() -> None:
        with urllib.request.urlopen(url, timeout=PROBE_TIMEOUT.total_seconds()) as r:  # noqa: S310
            if not 200 <= r.status < 300:
                raise NotReady(f"{url}: {r.status}")

    return blocking(get)


def temporal(client: Any) -> Check:
    """The frontend answers: GetSystemInfo, no namespace work."""
    from temporalio.api.workflowservice.v1 import GetSystemInfoRequest

    async def ask() -> None:
        await client.workflow_service.get_system_info(GetSystemInfoRequest())

    return ask


def object_store(store: Any) -> Check:
    """A one-key LIST on the audit bucket (aisre.record.audit_copy.S3Store)."""
    return blocking(lambda: store.keys("health-probe/"))


@dataclass
class Report:
    failing: dict[str, str] = field(default_factory=dict)  # readiness check -> why

    @property
    def ready(self) -> bool:
        return not self.failing


async def _run(checks: Mapping[str, Check]) -> dict[str, str]:
    async def one(check: Check) -> str | None:
        try:
            await asyncio.wait_for(check(), PROBE_TIMEOUT.total_seconds())
        except Exception as e:  # noqa: BLE001 (any failure: the check fails)
            return f"{type(e).__name__}: {e}"[:200] or type(e).__name__
        return None

    names = list(checks)
    results = await asyncio.gather(*(one(checks[n]) for n in names))
    return {n: r for n, r in zip(names, results, strict=True) if r is not None}


class Health:
    """One process's readiness checks and dependency probes, and the two gauges they set."""

    def __init__(
        self,
        process: str,
        readiness: Mapping[str, Check],
        dependencies: Mapping[str, Check] | None = None,
    ) -> None:
        unknown = set(dependencies or {}) - set(DEPENDENCIES)
        if unknown:
            raise ValueError(f"unknown dependencies: {sorted(unknown)}")
        self.process = process
        self.readiness = dict(readiness)
        self.dependencies = dict(dependencies or {})
        self._last: dict[str, bool] = {}

    async def ready(self) -> Report:
        """Runs the readiness checks once and sets aisre_process_ready{process}."""
        report = Report(await _run(self.readiness))
        telemetry.process_ready.labels(process=self.process).set(1 if report.ready else 0)
        return report

    async def probe(self) -> dict[str, bool]:
        """Probes every dependency once and sets aisre_dependency_up{dependency}."""
        failed = await _run(self.dependencies)
        now = {n: n not in failed for n in self.dependencies}
        for name, up in now.items():
            telemetry.dependency_up.labels(dependency=name).set(1 if up else 0)
            before = self._last.get(name)
            if before is not None and before != up:  # changes only; the gauge has the rest
                if up:
                    log.warning("dependency %s back up", name)
                else:
                    log.warning("dependency %s down: %s", name, failed[name])
        self._last = now
        return now

    async def run(self, stop: asyncio.Event, every: timedelta = PROBE_EVERY) -> None:
        """Readiness and probes every `every` until `stop` is set."""
        while not stop.is_set():
            await asyncio.gather(self.ready(), self.probe())
            try:
                await asyncio.wait_for(stop.wait(), every.total_seconds())
            except TimeoutError:
                pass
