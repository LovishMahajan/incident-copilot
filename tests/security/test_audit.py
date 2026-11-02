"""ST-31, ST-32: audit tampering is enforced by PostgreSQL, proved by schema-tests.sql on a real server. pytest has no
database connection, so this file confirms the named checks exist with the expected label, and leaves the proof there."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from support import ROOT, SCHEMA_CHECKS, SCHEMA_TESTS

LABELS = dict(l.split(" ", 1) for l in re.findall(r"pg_temp\.must_\w+\(\s*'([^']+)'", SCHEMA_TESTS.read_text()))
ALERTS = ROOT / "ops/alerts.yml"
README = Path(__file__).with_name("README.md")


@pytest.mark.parametrize("st,check,label", [(st, c, l) for st, checks in SCHEMA_CHECKS.items() for c, l in checks.items()])
def test_named_schema_check_exists_with_this_label_and_is_in_the_readme(st, check, label):
    assert LABELS.get(check) == label
    row = next(l for l in README.read_text().splitlines() if l.startswith(f"| {st} "))
    assert re.search(rf"\b{check}\b", row)


def test_st31_st32_every_st_id_is_on_a_readme_row_with_a_known_status():
    rows = re.findall(r"^\| (ST-\d\d) \| ([^|]+) \|", README.read_text(), re.M)
    assert [r[0] for r in rows] == [f"ST-{i:02d}" for i in range(1, 41)]
    assert {r[1].strip() for r in rows} <= {"Automated", "Partly automated", "Needs running system"}


def test_st31_update_or_delete_needs_postgresql():
    pytest.skip("Needs PostgreSQL: triggers and role rights exist only on a server. Run schema-tests.sql "
                "(checks D1-D5, D19; see README)")


def test_st32_privileged_edit_needs_postgresql_and_the_write_once_store():
    pytest.skip("Needs PostgreSQL for the chain (checks I18-I20) and a write-once store, which doesn't exist")


def test_st32_a_broken_chain_has_an_alert():
    assert re.search(r"(?i)chain|tamper", ALERTS.read_text())
