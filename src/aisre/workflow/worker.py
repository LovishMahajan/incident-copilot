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

Evidence (T1.10, T1.17): with `AISRE_EVIDENCE_SOURCES` set, `collect_evidence` and
`retrieve_knowledge` are the real Evidence Collector and Knowledge Retriever
(record_activities.EvidenceActivities), ahead of any script. The variable is JSON for
aisre.evidence.sources.SourceSet.from_config, which names the adapter answering each READ tool,
for example `{"sources": {"*": {"adapter": "fake_monitoring", "url":
"http://fake-monitoring:8080/v1/scenarios/reference-incident"}}}`. Snapshots go to the object
store when `AISRE_OBJECT_STORE_URL`, `_BUCKET`, `_ACCESS_KEY` and `_SECRET_KEY` are all set.
All of them are typed settings (aisre.config).

Skills (T1.14, T1.18): with `AISRE_PROVIDER_API_KEY` set, `hypothesise`, `diagnose` and
`review_diagnosis` are the real skills (aisre.agent.skills), ahead of any script: the Model
Gateway over the Anthropic transport (to `AISRE_PROVIDER_URL` when set, which locally is
Compose's fake provider), the durable ledger, the prompts in `AISRE_PROMPTS_DIR`, and
PgSkillStore. `draft_patch` stays scripted until M2. `run_sandbox_test` is not registered here: it runs
in the Test Runner, on its own queue, the only process with Docker (ADR-71,
aisre.remediation.test_runner). Without a key, tests that ask for scripted
skills get them.

Budget jobs (T1.21): the worker registers BudgetJobWorkflow with the ledger's expiry and
reconciliation activities, over PostgreSQL, and starts both jobs if they aren't running
(aisre.workflow.budget_jobs).

Notifier (T1.19): the real `notify` activity, ahead of the scripts, and the dead-letter pass
running beside the relay (aisre.notify.notifier). Telemetry (T1.19): `/metrics` on
AISRE_METRICS_PORT, spans to OTEL_EXPORTER_OTLP_ENDPOINT, and every activity timed and traced
by the interceptors in aisre.reliability.temporal_telemetry.

Audit copy (T1.20): the copy and the weekly chain check run as timer jobs on the same
BudgetJobWorkflow, started if they aren't running (aisre.reliability.audit_jobs). The store
comes from the AISRE_AUDIT_STORE_* variables; without them only the chain is checked. The
worker's own audit lines (`aisre.model_output.unsafe`, T1.16) go to standard error and to the
same store, as the API's do (aisre.api.audit).

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
from temporalio.worker import Interceptor, Worker

from aisre.agent.skills.activities import ModelSkills
from aisre.agent.skills.reasoning import SCHEMAS, load_prompts
from aisre.agent.skills.store import PgSkillStore
from aisre.api import audit
from aisre.config import Settings
from aisre.evidence.snapshots import S3Snapshots
from aisre.evidence.sources import SourceSet
from aisre.gateway.adapters import ClaudeAdapter
from aisre.gateway.gateway import CLAUDE_PRICES, ModelGateway
from aisre.gateway.jobs import BudgetJobActivities
from aisre.gateway.ledger import PgBudgetLedger
from aisre.gateway.transport import AnthropicTransport
from aisre.notify.notifier import Notifier
from aisre.record import make_engine
from aisre.record.audit_copy import store_from_settings
from aisre.reliability import telemetry
from aisre.reliability.audit_jobs import AuditJobActivities, ensure_audit_jobs
from aisre.reliability.temporal_telemetry import interceptors
from aisre.workflow import activities as act
from aisre.workflow import scripted
from aisre.workflow.budget_jobs import BudgetJobWorkflow, ensure_budget_jobs
from aisre.workflow.record_activities import EvidenceActivities, RecordActivities
from aisre.workflow.relay import Relay
from aisre.workflow.workflow import IncidentWorkflow, ReconciliationWorkflow

ALIVE_FILE = Path(tempfile.gettempdir()) / "aisre-worker.alive"
ALIVE_WITHIN = timedelta(seconds=30)

log = logging.getLogger("aisre.worker")


def activity_name(fn: Callable[..., Any]) -> str:
    return fn.__name__  # every activity here is defined under its own name


def evidence_activities(settings: Settings, engine: Engine) -> list[Callable[..., Any]]:
    """T1.10, T1.17: the real Evidence Collector and Knowledge Retriever, when the evidence
    sources are configured; else none."""
    if settings.evidence_sources is None:
        return []
    production = settings.env == "production"
    sources = SourceSet.from_config(settings.evidence_sources, production=production)
    s = settings
    snapshots = (
        S3Snapshots(
            s.object_store_url,
            s.object_store_bucket,
            s.object_store_access_key,
            s.object_store_secret_key,
        )
        if s.object_store_url
        and s.object_store_bucket
        and s.object_store_access_key
        and s.object_store_secret_key
        else None
    )
    return EvidenceActivities(engine, sources, snapshots).all()


def skill_activities(
    settings: Settings, engine: Engine, transport: AnthropicTransport | None
) -> list[Callable[..., Any]]:
    """T1.18: the real model-backed skills, when the gateway is configured; else none."""
    if transport is None:
        return []
    gateway = ModelGateway(
        ClaudeAdapter(), transport, PgBudgetLedger(engine), CLAUDE_PRICES, SCHEMAS
    )
    prompts = load_prompts(Path(settings.prompts_dir))
    return ModelSkills(gateway, prompts, PgSkillStore(engine)).all()


def provider_transport(settings: Settings) -> AnthropicTransport | None:
    if settings.provider_api_key is None:
        return None
    return AnthropicTransport(settings.provider_api_key, settings.provider_url)


def investigation_activities(
    settings: Settings,
    engine: Engine,
    notifier: Notifier | None = None,
    transport: AnthropicTransport | None = None,
) -> list[Callable[..., Any]]:
    """Real record, evidence, skill and Notifier activities, then the scripts if enabled for
    what is still missing, then the interfaces for the rest."""
    chosen = (
        RecordActivities(engine).all()
        + evidence_activities(settings, engine)
        + skill_activities(settings, engine, transport)
        + (notifier or Notifier(engine)).activities()
    )
    if settings.scripted_skills:
        if settings.env == "production":
            raise RuntimeError("scripted skills are refused when AISRE_ENV is production")
        real = {activity_name(a) for a in chosen}
        scripts = scripted.scripted_activities(settings.scripted_step_seconds)
        chosen += [a for a in scripts if activity_name(a) not in real]
    names = {activity_name(a) for a in chosen}
    # activities.py isn't strict: its list is typed as plain functions.
    interfaces = cast(Sequence[Callable[..., Any]], act.ALL_INVESTIGATION_ACTIVITIES)
    return chosen + [a for a in interfaces if activity_name(a) not in names]


def budget_job_activities(engine: Engine) -> list[Callable[..., Any]]:
    """T1.21: reservation expiry and spend reconciliation, over the durable ledger."""
    return list(BudgetJobActivities(PgBudgetLedger(engine)).all())


def build_worker(
    client: Client,
    activities: Sequence[Callable[..., Any]],
    worker_interceptors: Sequence[Interceptor] = (),
) -> Worker:
    return Worker(
        client,
        task_queue=act.INVESTIGATION_QUEUE,
        workflows=[IncidentWorkflow, ReconciliationWorkflow, BudgetJobWorkflow],
        activities=activities,
        interceptors=worker_interceptors,
    )


async def run(settings: Settings, stop: asyncio.Event | None = None) -> None:
    stop = stop or asyncio.Event()
    telemetry.setup_tracing("aisre-worker", settings.otlp_endpoint)
    metrics = telemetry.serve_metrics(settings.metrics_port)
    client = await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        data_converter=act.DATA_CONVERTER,
    )
    engine = make_engine(settings.database_url)
    notifier = Notifier(engine)
    store = store_from_settings(settings)
    audit.install_handler()
    audit.install_copy(store)
    transport = provider_transport(settings)
    try:
        chosen = investigation_activities(settings, engine, notifier, transport)
        chosen += budget_job_activities(engine)
        chosen += AuditJobActivities(engine, store).all()
        async with build_worker(client, chosen, interceptors()):
            await ensure_budget_jobs(client, act.INVESTIGATION_QUEUE)
            await ensure_audit_jobs(client, act.INVESTIGATION_QUEUE)
            log.info(
                "worker on %s, relay running, model skills %s, scripted skills %s",
                act.INVESTIGATION_QUEUE,
                "on" if transport is not None else "off",
                "on" if settings.scripted_skills else "off",
            )
            await asyncio.gather(
                Relay(engine, client).run(stop, on_pass=ALIVE_FILE.touch),
                notifier.run(stop),
            )
    finally:
        if transport is not None:
            await transport.close()
        audit.install_copy(None)  # flushes what is pending
        engine.dispose()
        if metrics is not None:
            metrics.shutdown()


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
