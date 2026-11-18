"""ST-31, ST-32: audit tampering is enforced by PostgreSQL, proved by schema-tests.sql on a real server, and caught by
the write-once audit copy (T1.20). pytest has no database connection here, so this file confirms the named checks exist
with the expected label, and runs the copy's half of ST-32 on the in-memory store."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from support import ROOT, SCHEMA_CHECKS, SCHEMA_TESTS

from aisre.record.audit_copy import AlreadyWritten, MemoryStore, canonical, compare

LABELS = dict(
    l.split(" ", 1)
    for l in re.findall(r"pg_temp\.must_\w+\(\s*'([^']+)'", SCHEMA_TESTS.read_text())
)
ALERTS = ROOT / "ops/alerts.yml"
README = Path(__file__).with_name("README.md")


@pytest.mark.parametrize(
    "st,check,label",
    [(st, c, l) for st, checks in SCHEMA_CHECKS.items() for c, l in checks.items()],
)
def test_named_schema_check_exists_with_this_label_and_is_in_the_readme(st, check, label):
    assert LABELS.get(check) == label
    row = next(l for l in README.read_text().splitlines() if l.startswith(f"| {st} "))
    assert re.search(rf"\b{check}\b", row)


def test_st31_st32_every_st_id_is_on_a_readme_row_with_a_known_status():
    rows = re.findall(r"^\| (ST-\d\d) \| ([^|]+) \|", README.read_text(), re.M)
    assert [r[0] for r in rows] == [f"ST-{i:02d}" for i in range(1, 41)]
    assert {r[1].strip() for r in rows} <= {"Automated", "Partly automated", "Needs running system"}


def test_st31_update_or_delete_needs_postgresql():
    pytest.skip(
        "Needs PostgreSQL: triggers and role rights exist only on a server. "
        "tests/integration/test_audit_copy.py::test_st31_* runs the case as the app role and the "
        "owner (and schema-tests.sql checks D1-D5, D19)"
    )


# ST-32 (T1.20): a privileged edit that keeps the chain consistent is still caught, because the
# write-once copy can't be rewritten with it. Against PostgreSQL, with a real edit:
# tests/integration/test_audit_copy.py::test_st32_*.
EVENT = {
    "event_id": "e1",
    "incident_id": "i1",
    "seq": 1,
    "type": "aisre.incident.created.v1",
    "data": {"title": "Checkout errors"},
    "prev_hash": None,
    "hash": "a" * 64,
}


def test_st32_the_copy_refuses_to_be_rewritten():
    store = MemoryStore()
    store.put_once("timeline/i1/000000000001.json", canonical(EVENT))
    store.put_once("timeline/i1/000000000001.json", canonical(EVENT))  # a retried copy
    with pytest.raises(AlreadyWritten):
        store.put_once("timeline/i1/000000000001.json", canonical(EVENT | {"hash": "b" * 64}))
    assert not hasattr(store, "delete")
    assert json.loads(store.get("timeline/i1/000000000001.json"))["hash"] == "a" * 64


def test_st32_an_edit_with_a_rewritten_hash_differs_from_the_copy():
    copy = {1: canonical(EVENT)}
    edited = EVENT | {"data": {"title": "Nothing happened"}, "hash": "c" * 64}
    (fault,) = compare("i1", [edited], copy, upto=1)
    assert (fault.at_seq, fault.problem) == (1, "differs from the write-once copy")
    assert compare("i1", [EVENT], copy, upto=1) == []  # control: the untouched record


def test_st32_a_deleted_or_inserted_event_is_a_fault():
    assert compare("i1", [], {1: canonical(EVENT)}, upto=1)[0].problem.startswith("in the write")
    second = EVENT | {"seq": 2}
    assert compare("i1", [EVENT, second], {1: canonical(EVENT)}, upto=2)[0].at_seq == 2


def test_st32_a_broken_chain_has_an_alert():
    assert re.search(r"(?i)chain|tamper", ALERTS.read_text())
