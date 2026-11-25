"""T4.5 (Artifact 12.5, WR-22, WR-23): readiness checks and dependency probes.

The checks run against stand-ins here; tests/integration/test_readiness_stack.py runs them on the
Compose stack and reads the gauges from each process's /metrics.
"""

from __future__ import annotations

import asyncio
import importlib.util
from datetime import timedelta
from pathlib import Path

import pytest

from aisre.reliability import health, telemetry

ROOT = Path(__file__).parents[2]


def ok() -> health.Check:
    async def check() -> None:
        return None

    return check


def fails(why: str) -> health.Check:
    async def check() -> None:
        raise health.NotReady(why)

    return check


def hangs() -> health.Check:
    async def check() -> None:
        await asyncio.sleep(60)

    return check


def gauge(name: str, **labels: str) -> float | None:
    return telemetry.REGISTRY.get_sample_value(name, labels)


def test_schema_head_is_the_last_migration():
    versions = ROOT / "db/migrations/versions"
    mods = {}
    for p in versions.glob("*.py"):
        spec = importlib.util.spec_from_file_location(p.stem, p)
        assert spec and spec.loader
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        mods[m.revision] = m
    downs = {m.down_revision for m in mods.values()}
    assert [r for r in mods if r not in downs] == [health.SCHEMA_HEAD]


def test_ready_sets_the_gauge_and_names_what_failed():
    h = health.Health("api", {"database": ok(), "schema_version": fails("schema at 0009")})
    report = asyncio.run(h.ready())
    assert not report.ready and list(report.failing) == ["schema_version"]
    assert "0009" in report.failing["schema_version"]
    assert gauge("aisre_process_ready", process="api") == 0
    assert asyncio.run(health.Health("api", {"database": ok()}).ready()).ready
    assert gauge("aisre_process_ready", process="api") == 1


def test_a_hung_check_fails_within_the_timeout(monkeypatch):
    monkeypatch.setattr(health, "PROBE_TIMEOUT", timedelta(milliseconds=50))
    h = health.Health("worker", {"database": hangs()}, {"postgresql": hangs(), "temporal": ok()})
    assert asyncio.run(h.probe()) == {"postgresql": False, "temporal": True}
    assert gauge("aisre_dependency_up", dependency="postgresql") == 0
    assert gauge("aisre_dependency_up", dependency="temporal") == 1
    assert "TimeoutError" in asyncio.run(h.ready()).failing["database"]


def test_only_the_four_dependencies_are_probed():
    with pytest.raises(ValueError, match="model_provider"):
        health.Health("worker", {}, {"model_provider": ok()})


def test_readiness_never_includes_temporal_or_the_provider():
    """12.5: a dependency outage doesn't take a process out of service."""
    from aisre.api.app import api_health
    from aisre.config import Settings

    h = api_health(Settings(env="test"), object(), None)  # type: ignore[arg-type]
    assert set(h.readiness) == {"database", "schema_version", "configuration"}
    assert set(h.dependencies) == {"postgresql"}
    on = Settings(env="test", release_adapter="fake_release")
    assert "approval_signing_key" in api_health(on, object(), None).readiness  # type: ignore[arg-type]


def test_the_dispatcher_is_not_ready_while_fenced_and_probes_the_release_process():
    from aisre.config import Settings
    from aisre.remediation.dispatcher_process import dispatcher_health

    s = Settings(
        env="test",
        release_adapter="fake_release",
        release_url="http://fake-release:8080",
        release_token="fake-release-token",  # type: ignore[arg-type]
    )
    h = dispatcher_health(s, object(), object())  # type: ignore[arg-type]
    assert "write_fence" in h.readiness
    assert set(h.dependencies) == {"postgresql", "temporal", "release_process"}
    assert set(h.gauges) == {"fence"}  # aisre_dispatch_fenced, which AisreWritesFenced reads


def test_gauges_publish_every_cycle_and_a_failure_is_only_logged(caplog):
    published = []

    async def queue() -> None:
        published.append("queue")

    h = health.Health("worker", {}, {}, {"queue": queue, "spend": fails("db gone")})
    assert asyncio.run(h.publish()) == {"spend": "NotReady: db gone"}
    assert published == ["queue"]
    assert "gauge publisher spend failed" in caplog.text


def test_run_probes_until_stopped():
    calls = []

    async def counted() -> None:
        calls.append(1)

    async def go() -> None:
        stop = asyncio.Event()
        h = health.Health("worker", {"database": counted}, {"postgresql": counted})
        task = asyncio.create_task(h.run(stop, every=timedelta(milliseconds=10)))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, 1)

    asyncio.run(go())
    assert len(calls) >= 4


def test_the_api_reads_the_approval_path_as_the_release_module_does():
    from aisre.api.app import approval_path_on
    from aisre.config import Settings
    from aisre.remediation.release import remediation_enabled

    for env in ("development", "test", "staging"):
        for adapter in (None, "fake_release"):
            try:
                s = Settings(env=env, release_adapter=adapter)
            except ValueError:
                continue
            assert approval_path_on(s) == remediation_enabled(s)


def test_probes_log_changes_only(caplog):
    state = {"up": False}

    async def flaky() -> None:
        if not state["up"]:
            raise ConnectionError("refused")

    h = health.Health("worker", {}, {"postgresql": flaky})
    with caplog.at_level("WARNING", logger="aisre.reliability.health"):
        asyncio.run(h.probe())  # the first result is the gauge's, not a change
        asyncio.run(h.probe())
        state["up"] = True
        asyncio.run(h.probe())
        state["up"] = False
        asyncio.run(h.probe())
    assert [r.getMessage().split(":")[0] for r in caplog.records] == [
        "dependency postgresql back up",
        "dependency postgresql down",
    ]
