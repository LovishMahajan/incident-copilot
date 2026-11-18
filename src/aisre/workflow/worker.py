"""Artifact 13.1 (ADR-51): the worker process. `python -m aisre.workflow.worker`.

Runs the Temporal worker on the investigation task queue, with the incident and
reconciliation workflows and every investigation-queue activity, and the outbox relay beside
it (Artifact 7.7: the relay lives in the worker). Client and worker use the Pydantic data
converter, as the workflow tests do (activities.DATA_CONVERTER). Dispatcher-queue activities
(`dispatch_change`, `reconcile_execution`) belong to the dispatcher process, from M3; none is
registered here.

Activities (T0.11): `load_incident`, `record_transition` and `hand_over` are real
(record_activities.py). With `AISRE_SCRIPTED_SKILLS` true, the activities M0 doesn't build answer
with aisre.workflow.scripted's fixed results, so an incident runs to `handed_over` with no model;
the worker refuses that in production, as Settings does. Every other activity is the interface
in activities.py, which raises if called. Without scripts, a workflow stops at the slot, retrying.

Liveness: after every relay pass the worker touches ALIVE_FILE; `--check-alive` exits 0 if
that happened within ALIVE_WITHIN, which is Compose's health check.

Connects to PostgreSQL as aisre_app, from `AISRE_DATABASE_URL` (ADR-51). Stops cleanly on
SIGTERM or SIGINT: the relay finishes its pass, then the worker drains.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

from sqlalchemy import Engine
from temporalio.client import Client
from temporalio.worker import Worker

from aisre.config import Settings
from aisre.record import make_engine
from aisre.workflow import activities as act
from aisre.workflow import scripted
from aisre.workflow.record_activities import RecordActivities
from aisre.workflow.relay import Relay
from aisre.workflow.workflow import IncidentWorkflow, ReconciliationWorkflow

ALIVE_FILE = Path(tempfile.gettempdir()) / "aisre-worker.alive"
ALIVE_WITHIN = timedelta(seconds=30)

log = logging.getLogger("aisre.worker")


def activity_name(fn: Callable[..., Any]) -> str:
    return fn.__name__  # every activity here is defined under its own name


def investigation_activities(settings: Settings, engine: Engine) -> list[Callable[..., Any]]:
    """Real record activities, then the scripts if enabled, then the interfaces for the rest."""
    chosen = RecordActivities(engine).all()
    if settings.scripted_skills:
        if settings.env == "production":
            raise RuntimeError("scripted skills are refused when AISRE_ENV is production")
        chosen += scripted.SCRIPTED_ACTIVITIES
    names = {activity_name(a) for a in chosen}
    # activities.py isn't strict: its list is typed as plain functions.
    interfaces = cast(Sequence[Callable[..., Any]], act.ALL_INVESTIGATION_ACTIVITIES)
    return chosen + [a for a in interfaces if activity_name(a) not in names]


def build_worker(client: Client, activities: Sequence[Callable[..., Any]]) -> Worker:
    return Worker(
        client,
        task_queue=act.INVESTIGATION_QUEUE,
        workflows=[IncidentWorkflow, ReconciliationWorkflow],
        activities=activities,
    )


async def run(settings: Settings, stop: asyncio.Event | None = None) -> None:
    stop = stop or asyncio.Event()
    client = await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        data_converter=act.DATA_CONVERTER,
    )
    engine = make_engine(settings.database_url)
    try:
        async with build_worker(client, investigation_activities(settings, engine)):
            log.info(
                "worker on %s, relay running, scripted skills %s",
                act.INVESTIGATION_QUEUE,
                "on" if settings.scripted_skills else "off",
            )
            await Relay(engine, client).run(stop, on_pass=ALIVE_FILE.touch)
    finally:
        engine.dispose()


def is_alive(now: float | None = None) -> bool:
    try:
        age = (now or time.time()) - ALIVE_FILE.stat().st_mtime
    except FileNotFoundError:
        return False
    return age <= ALIVE_WITHIN.total_seconds()


async def _main(settings: Settings) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await run(settings, stop)


def main(argv: Sequence[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if args == ["--check-alive"]:
        return 0 if is_alive() else 1
    if args:
        print("usage: python -m aisre.workflow.worker [--check-alive]", file=sys.stderr)
        return 2
    settings = Settings.from_env()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    asyncio.run(_main(settings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
