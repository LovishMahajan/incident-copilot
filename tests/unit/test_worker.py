"""Artifact 13.5 T0.10: the worker entry point, offline."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from aisre.workflow import activities as act
from aisre.workflow import worker
from aisre.workflow.record_activities import EvidenceActivities

ROOT = Path(__file__).parents[2]


def test_worker_registers_the_investigation_queue_only():
    names = {a.__name__ for a in act.ALL_INVESTIGATION_ACTIVITIES}
    assert not names & {a.__name__ for a in act.ALL_DISPATCHER_ACTIVITIES}
    assert worker.act.INVESTIGATION_QUEUE == "aisre-investigation"


def test_check_alive_follows_the_file(tmp_path, monkeypatch):
    alive = tmp_path / "alive"
    monkeypatch.setattr(worker, "ALIVE_FILE", alive)
    assert worker.main(["--check-alive"]) == 1  # never touched
    alive.touch()
    assert worker.main(["--check-alive"]) == 0
    old = time.time() - worker.ALIVE_WITHIN.total_seconds() - 5
    os.utime(alive, (old, old))
    assert worker.main(["--check-alive"]) == 1


def test_unknown_arguments_are_refused():
    assert worker.main(["--nope"]) == 2


def _activities(environ, scripted=True):
    from sqlalchemy import create_engine

    from aisre.config import Settings

    settings = Settings.from_env(
        {**environ, "AISRE_ENV": "test", "AISRE_SCRIPTED_SKILLS": str(scripted).lower()}
    )
    engine = create_engine("postgresql+psycopg://aisre_app@127.0.0.1:1/aisre")  # never connects
    return worker.investigation_activities(settings, engine)


def test_the_real_collector_replaces_the_script_once_sources_are_configured():
    config = {"sources": {"*": {"adapter": "fake_monitoring", "url": "http://fake:8080/x"}}}
    chosen = _activities({"AISRE_EVIDENCE_SOURCES": json.dumps(config)})
    names = [worker.activity_name(a) for a in chosen]
    assert len(names) == len(set(names))
    collect = next(a for a in chosen if worker.activity_name(a) == "collect_evidence")
    assert isinstance(collect.__self__, EvidenceActivities) and collect.__self__.snapshots is None
    knowledge = next(a for a in chosen if worker.activity_name(a) == "retrieve_knowledge")
    assert knowledge.__self__ is collect.__self__
    s3 = {
        "AISRE_EVIDENCE_SOURCES": json.dumps(config),
        "AISRE_OBJECT_STORE_URL": "http://minio:9000",
        "AISRE_OBJECT_STORE_BUCKET": "aisre-artifacts",
        "AISRE_OBJECT_STORE_ACCESS_KEY": "fake-minio-admin",
        "AISRE_OBJECT_STORE_SECRET_KEY": "fake-minio-password",
    }
    collect = next(a for a in _activities(s3) if worker.activity_name(a) == "collect_evidence")
    assert collect.__self__.snapshots is not None


def test_without_sources_the_script_still_answers():
    chosen = _activities({})
    collect = next(a for a in chosen if worker.activity_name(a) == "collect_evidence")
    assert collect.__module__ == "aisre.workflow.scripted"


def test_the_real_skills_replace_the_scripts_once_the_gateway_is_configured():
    from sqlalchemy import create_engine

    from aisre.agent.skills.activities import ModelSkills
    from aisre.config import Settings
    from aisre.gateway.transport import AnthropicTransport

    env = {
        "AISRE_ENV": "test",
        "AISRE_SCRIPTED_SKILLS": "true",
        "AISRE_PROVIDER_URL": "http://fake-provider:8080",
        "AISRE_PROVIDER_API_KEY": "fake-provider-key",
        "AISRE_PROMPTS_DIR": str(ROOT / "prompts"),
    }
    settings = Settings.from_env(env)
    transport = worker.provider_transport(settings)
    assert isinstance(transport, AnthropicTransport)
    assert "fake-provider-key" not in repr(transport) + repr(settings)
    engine = create_engine("postgresql+psycopg://aisre_app@127.0.0.1:1/aisre")  # never connects
    chosen = worker.investigation_activities(settings, engine, transport=transport)
    by_name = {worker.activity_name(a): a for a in chosen}
    assert len(by_name) == len(chosen)
    for name in ("hypothesise", "diagnose", "review_diagnosis"):
        assert isinstance(by_name[name].__self__, ModelSkills)
    assert by_name["draft_patch"].__module__ == "aisre.workflow.scripted"  # until M2
    assert worker.provider_transport(Settings.from_env({"AISRE_ENV": "test"})) is None
