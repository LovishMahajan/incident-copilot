"""Tests for the session guard in tests/conftest.py (Artifact 13.7, Q13.23).

Each case runs a real pytest session in a subprocess with one variable planted, and checks the
run stopped before its first test. A clean environment is the control.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
PROBE = "tests/unit/test_config.py::test_dot_env_is_ignored"
GUARD_EXIT_CODE = 4
CHECKED = ("AISRE_", "ANTHROPIC_API_KEY", "DATABASE_URL", "PGHOST")


def run_session(**planted: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(CHECKED)}
    env.update(planted)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", PROBE],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_control_a_clean_environment_runs():
    out = run_session()
    assert out.returncode == 0, out.stdout + out.stderr
    assert "1 passed" in out.stdout


@pytest.mark.parametrize(
    "planted",
    [
        {"AISRE_ENV": "production"},
        {"AISRE_ENV": "development"},
        {
            "AISRE_DATABASE_URL": "postgresql+psycopg://aisre_app:fake@db.prod.example.com:5432/aisre"
        },
        {"AISRE_DISPATCHER_DATABASE_URL": "postgresql+psycopg://d@127.0.0.1/aisre?host=10.0.0.5"},
        {"DATABASE_URL": "postgresql://u@rds.amazonaws.com/aisre"},
        {"PGHOST": "aisre-prod.internal"},
        {"AISRE_TEMPORAL_ADDRESS": "temporal.prod.example.com:7233"},
        {"ANTHROPIC_API_KEY": "fake"},
        {"AISRE_PROD_SIGNING_KEY": ""},
    ],
    ids=lambda p: next(iter(p)),
)
def test_a_production_variable_stops_the_run_before_the_first_test(planted):
    out = run_session(**planted)
    assert out.returncode == GUARD_EXIT_CODE, out.stdout + out.stderr
    assert "Session guard" in out.stdout + out.stderr
    assert "passed" not in out.stdout
    for name, value in planted.items():  # names a credential or URL variable, never its value
        if value and name != "AISRE_ENV":
            assert value not in out.stdout + out.stderr


@pytest.mark.parametrize(
    "planted",
    [
        {"AISRE_ENV": "test"},
        {"AISRE_DATABASE_URL": "postgresql+psycopg://aisre_app:fake@postgres:5432/aisre"},
        {"AISRE_MIGRATION_DATABASE_URL": "postgresql+psycopg://aisre_owner@localhost/aisre"},
        {"AISRE_DATABASE_URL": "postgresql+psycopg://aisre_app@[::1]:5432/aisre"},
        {"PGHOST": "/tmp"},
        {"AISRE_TEMPORAL_ADDRESS": "temporal:7233"},
        {"AISRE_TEST_PROVIDER_KEY": "fake"},
    ],
    ids=lambda p: next(iter(p)) + "=" + next(iter(p.values())).split("@")[-1],
)
def test_local_and_test_values_are_allowed(planted):
    out = run_session(**planted)
    assert out.returncode == 0, out.stdout + out.stderr
