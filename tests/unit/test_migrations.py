"""Tests for db/migrations (Artifact 13.1, ADR-64): 0001_baseline is schema.sql, unchanged.

The real proof is CI's schema job: `alembic upgrade head` from empty on PostgreSQL 16 and 17,
37 tables, then schema-tests.sql. These checks are the offline part.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
VERSIONS = ROOT / "db/migrations/versions"


def load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


baseline = load(VERSIONS / "0001_baseline.py")


def test_baseline_is_schema_sql_without_its_transaction_wrapper():
    sql = (ROOT / "db/schema.sql").read_text(encoding="utf-8")
    body = baseline.schema_body()
    assert not re.search(r"^(BEGIN|COMMIT);$", body, flags=re.M)
    # The wrapper lines become blank; every other line is the same.
    expected = ["" if ln in ("BEGIN;", "COMMIT;") else ln for ln in sql.splitlines()]
    assert body.splitlines() == expected
    assert len(re.findall(r"^CREATE TABLE ", body, flags=re.M)) == 37


def test_baseline_refuses_a_schema_it_does_not_recognise(tmp_path, monkeypatch):
    changed = tmp_path / "schema.sql"
    changed.write_text("BEGIN;\nCREATE TABLE t ();\nCOMMIT;\nBEGIN;\nCOMMIT;\n")
    monkeypatch.setattr(baseline, "SCHEMA_SQL", changed)
    with pytest.raises(RuntimeError, match="exactly one"):
        baseline.schema_body()


def test_one_head_starting_at_the_baseline():
    revisions = {p.stem: load(p) for p in VERSIONS.glob("*.py")}
    assert baseline.down_revision is None and baseline.revision == "0001_baseline"
    downs = {m.down_revision for m in revisions.values()}
    heads = [r for r, m in revisions.items() if m.revision not in downs]
    assert len(heads) == 1


def test_downgrade_keeps_the_roles():
    assert "ROLE" not in baseline.DROP_PUBLIC.upper().replace("DROP ROUTINE", "")
