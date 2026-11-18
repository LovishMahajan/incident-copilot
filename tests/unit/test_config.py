"""Tests for config.py (Artifact 13.1, Q13.11): settings come from the environment, typed."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from aisre.config import ENV_VARS, Settings
from aisre.workflow import activities as act

ROOT = Path(__file__).parents[2]


def test_defaults_need_no_environment():
    s = Settings.from_env({})
    assert s.env == "development" and s.log_level == "INFO"
    assert s.database_url.get_secret_value().startswith("postgresql+psycopg://aisre_app@")
    assert "aisre_dispatcher@" in s.dispatcher_database_url.get_secret_value()


def test_values_come_from_the_environment():
    s = Settings.from_env(
        {
            "AISRE_ENV": "test",
            "AISRE_DATABASE_URL": "postgresql+psycopg://aisre_app:fake@db:5432/aisre",
            "AISRE_TEMPORAL_ADDRESS": "temporal:7233",
            "AISRE_TEMPORAL_NAMESPACE": "aisre",
            "AISRE_LOG_LEVEL": "debug",
        }
    )
    assert (s.env, s.temporal_address, s.temporal_namespace, s.log_level) == (
        "test",
        "temporal:7233",
        "aisre",
        "DEBUG",
    )
    assert s.database_url.get_secret_value().endswith(":fake@db:5432/aisre")


def test_an_empty_variable_counts_as_unset():
    assert Settings.from_env({"AISRE_ENV": ""}).env == "development"


@pytest.mark.parametrize("var, value", [("AISRE_ENV", "staging"), ("AISRE_LOG_LEVEL", "LOUD")])
def test_a_bad_value_is_refused(var, value):
    with pytest.raises(ValidationError):
        Settings.from_env({var: value})


def test_passwords_stay_out_of_repr():
    s = Settings.from_env({"AISRE_DATABASE_URL": "postgresql+psycopg://u:hunter2@h/aisre"})
    assert "hunter2" not in repr(s) and "hunter2" not in str(s.model_dump())


def test_task_queues_match_the_workflow():
    assert Settings.investigation_task_queue == act.INVESTIGATION_QUEUE
    assert Settings.dispatcher_task_queue == act.DISPATCHER_QUEUE
    assert Settings.test_runner_task_queue == act.TEST_RUNNER_QUEUE


def test_config_does_not_import_temporalio():
    code = "import sys, aisre.config; print('temporalio' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


def test_env_example_names_every_setting_with_fake_values():
    example = (ROOT / ".env.example").read_text()
    names = {
        line.split("=", 1)[0]
        for line in example.splitlines()
        if "=" in line and not line.startswith("#")
    }
    assert set(ENV_VARS.values()) <= names
    Settings.from_env(
        {
            k: v
            for k, v in (
                ln.split("=", 1)
                for ln in example.splitlines()
                if "=" in ln and not ln.startswith("#")
            )
        }
    )


def test_dot_env_is_ignored():
    assert ".env" in (ROOT / ".gitignore").read_text().splitlines()


def test_process_wiring_is_typed_under_its_own_variable_names():
    s = Settings.from_env(
        {
            "AISRE_EVIDENCE_SOURCES": '{"sources": {"*": {"adapter": "fake_monitoring"}}}',
            "AISRE_OBJECT_STORE_URL": "http://minio:9000",
            "AISRE_OBJECT_STORE_BUCKET": "aisre-artifacts",
            "AISRE_OBJECT_STORE_ACCESS_KEY": "fake-key",
            "AISRE_OBJECT_STORE_SECRET_KEY": "fake-secret",
            "AISRE_AUDIT_STORE_LOCK_DAYS": "3",
            "AISRE_EXPORT_SIGNING_KEY": "ZmFrZQ==",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://otel-collector:4318",
            "AISRE_METRICS_PORT": "9464",
        }
    )
    assert s.evidence_sources == {"sources": {"*": {"adapter": "fake_monitoring"}}}
    assert (s.metrics_port, s.otlp_endpoint, s.audit_store_lock_days) == (
        9464,
        "http://otel-collector:4318",
        3,
    )
    assert "fake-secret" not in repr(s) and "ZmFrZQ" not in repr(s)


@pytest.mark.parametrize(
    "values",
    [
        {"AISRE_EVIDENCE_SOURCES": "{not json"},
        {"AISRE_METRICS_PORT": "nine"},
        {"AISRE_OBJECT_STORE_URL": "http://minio:9000"},  # without bucket and keys
        {"AISRE_AUDIT_STORE_URL": "http://minio:9000", "AISRE_AUDIT_STORE_BUCKET": "b"},
        {"AISRE_AUDIT_STORE_LOCK_DAYS": "0"},
        {"OTEL_EXPORTER_OTLP_ENDPOINT": "otel-collector:4318"},
    ],
)
def test_bad_wiring_is_refused_at_start(values):
    with pytest.raises(ValidationError):
        Settings.from_env(values)


def test_no_process_reads_the_environment_outside_config():
    """Every AISRE_* variable a process reads goes through Settings. The CLI is a person's tool,
    and the evaluation runner hands the worker's override to `docker compose` in its environment."""
    src = ROOT / "src" / "aisre"
    offenders = [
        str(p.relative_to(ROOT))
        for p in src.rglob("*.py")
        if p.name != "config.py"
        and p not in (src / "cli.py", src / "evaluation/runner.py")
        and ("os.environ" in p.read_text() or "getenv(" in p.read_text())
    ]
    assert offenders == []


def test_the_export_key_comes_from_settings_and_is_required_in_production():
    import base64

    from aisre.api.export import ExportSigner

    seed = base64.b64encode(bytes(32)).decode()  # all zeros: a test key, never a real one
    signed = ExportSigner.from_settings(Settings.from_env({"AISRE_EXPORT_SIGNING_KEY": seed}))
    assert signed.key_id.startswith("export-")
    assert ExportSigner.from_settings(Settings()).key_id.startswith("ephemeral-")
    with pytest.raises(RuntimeError, match="AISRE_EXPORT_SIGNING_KEY"):
        ExportSigner.from_settings(Settings(env="production"))
