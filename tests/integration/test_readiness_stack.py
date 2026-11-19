"""T4.5 (Artifact 12.5, WR-22, WR-23): readiness and the dependency probes on the Compose stack.

The schema-version check reads the Alembic version as aisre_app and aisre_dispatcher (migration
0011 grants a read of that one column); the API's /readyz answers;
and Prometheus holds aisre_process_ready and aisre_dependency_up from the API, the worker and
the dispatcher, each probing what it uses.
"""

# ruff: noqa: F811 (pytest fixtures imported, then named as arguments)
from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.parse
import urllib.request

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from test_dispatch_ledger import dispatcher_engine  # noqa: F401 (fixture)

from aisre.reliability import health

API = os.environ.get("AISRE_TEST_API_URL", "http://127.0.0.1:8000")
PROMETHEUS = os.environ.get("AISRE_TEST_PROMETHEUS_URL", "http://127.0.0.1:59090")


def get(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310 (local stack)
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except OSError as e:
        pytest.skip(f"{url} unreachable: {type(e).__name__}")


def series(query: str) -> dict[tuple[str, str], float]:
    status, body = get(f"{PROMETHEUS}/api/v1/query?" + urllib.parse.urlencode({"query": query}))
    assert status == 200
    out = {}
    for r in json.loads(body)["data"]["result"]:
        m = r["metric"]
        out[(m["job"], m.get("process") or m.get("dependency", ""))] = float(r["value"][1])
    return out


def test_schema_version_and_fence_checks_pass_under_each_role(app_engine, dispatcher_engine):
    async def go() -> None:
        for engine in (app_engine, dispatcher_engine):
            await health.schema_version(engine)()
        await health.write_fence(dispatcher_engine)()

    asyncio.run(go())
    with dispatcher_engine.connect() as conn:  # read, never write
        with pytest.raises(ProgrammingError, match="permission denied"):
            conn.execute(text("UPDATE aisre_migrations.alembic_version SET version_num = 'x'"))


def test_a_schema_behind_the_code_is_not_ready(app_engine):
    with pytest.raises(health.NotReady, match="code expects 9999_future"):
        asyncio.run(health.schema_version(app_engine, "9999_future")())


def test_the_api_is_live_and_ready():
    assert get(f"{API}/livez")[0] == 200
    status, body = get(f"{API}/readyz")
    assert status == 200, body


def test_every_process_publishes_readiness_and_its_probes():
    want = {
        ("api", "api"),
        ("worker", "worker"),
        ("dispatcher", "dispatcher"),
    }
    probes = {
        ("api", "postgresql"),
        ("api", "object_store"),
        ("worker", "postgresql"),
        ("worker", "temporal"),
        ("worker", "object_store"),
        ("dispatcher", "postgresql"),
        ("dispatcher", "temporal"),
        ("dispatcher", "release_process"),
    }
    deadline = time.monotonic() + 45  # one scrape interval and a probe, with room
    while True:
        ready = series("aisre_process_ready")
        up = series("aisre_dependency_up")
        if want <= set(ready) and probes <= set(up) or time.monotonic() > deadline:
            break
        time.sleep(3)
    assert {k: ready.get(k) for k in want} == dict.fromkeys(want, 1.0)
    assert {k: up.get(k) for k in probes} == dict.fromkeys(probes, 1.0)
