"""T4.1 (Artifact 13.7 "Worker crash", "Write succeeded, no reply"; NFR-04, ST-37, ST-38): a
dispatcher process killed with SIGKILL at each step of a write, against PostgreSQL and the fake
release process (ADR-73), then a restarted dispatcher.

The dispatcher runs as its own process (`python -c`, the same Dispatcher and PgLedger the
Compose service builds, as aisre_dispatcher) so the kill is a real one: no handler, no
`finally`. The fake release process runs in this process (test_dispatch_ledger's `released`).

1. Killed after intent, before the release process saw the key (its call hangs on a socket
   that never answers): the restarted dispatcher finds the key unknown to the release process
   and the target at base, and the approval still standing, so it sends again with the same key
   while the action is `applying` (ST-38). One apply.
2. Killed twice in a row that way: still one apply.
3. Killed after the release process applied, before the dispatcher heard (the fake answers
   late): the restarted dispatcher reconciles by key, `applied`, and sends nothing (ST-37).

Every case ends with one `apply` in the release history for the key and one applied attempt.
"""

# ruff: noqa: F811 (pytest fixtures imported, then named as arguments)
from __future__ import annotations

import asyncio
import base64
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from test_approvals import PUBLIC, awaiting  # noqa: F401 (fixtures)
from test_dispatch_ledger import (  # noqa: F401 (fixtures)
    approved,
    attempts,
    client,
    dispatcher,
    dispatcher_engine,
    released,
    status,
)
from test_patch_review import drafted, review  # noqa: F401 (fixtures)

from aisre.record.execution import PgTrustedStore
from aisre.remediation.dispatcher import Outcome

CALL_TIMEOUT_S = 2.0  # the restarted dispatcher's: its in-flight window is 2 x 2 + 5 = 9 s
WINDOW_S = 2 * CALL_TIMEOUT_S + 5
HELD: dict[str, list[str]] = {}  # blackhole URL -> the writes it is holding

CHILD = """
import asyncio, base64, json, sys
from datetime import UTC, datetime
from pydantic import SecretStr
from aisre.config import Settings
from aisre.record import make_engine
from aisre.record.execution import PgFence, PgLedger, PgTrustedStore
from aisre.remediation.action import ApprovalPolicy, Directory, development_verify_keys
from aisre.remediation.dispatcher import Dispatcher
from aisre.remediation.release import FakeReleaseClient

a = json.loads(sys.argv[1])
objects = {k: base64.b64decode(v) for k, v in json.load(open(a["blobs"])).items()}

class Blobs:
    def get(self, key):
        return objects[key]

d = Directory()
for who in ("user:priya", "user:sam"):
    d.add_groups(who, [f"aisre:approver:{a['service']}"], a["service"])
engine = make_engine(Settings.from_env().dispatcher_database_url)
store = PgTrustedStore(engine, Blobs())
release = FakeReleaseClient(a["release"], SecretStr("t"), store.patch_content, timeout_s=600)
dispatcher = Dispatcher(
    store, release, PgLedger(engine), development_verify_keys(a["public"]), d,
    ApprovalPolicy(), lambda: datetime.now(UTC), a["timeout"], fence=PgFence(engine),
)
print("dispatching", flush=True)
print(asyncio.run(dispatcher.execute(a["action_id"])), flush=True)
"""


@pytest.fixture
def blackhole(released) -> Iterator[str]:
    """The release process as the dispatcher sees it when its write never arrives: reads
    (state, lookup) pass through to the fake, and a write is held with no answer."""
    _, upstream = released
    stop = threading.Event()
    writes: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802
            req = urllib.request.Request(upstream + self.path, headers=dict(self.headers))
            try:
                with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 (local)
                    status, body = r.status, r.read()
            except urllib.error.HTTPError as e:
                status, body = e.code, e.read()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            writes.append(self.path)
            stop.wait(600)  # never answers while the test runs

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    HELD[url] = writes
    yield url
    stop.set()
    server.shutdown()


def start_child(
    tmp_path: Path, service: str, action_id: str, url: str, blobs, timeout: float = 600.0
) -> subprocess.Popen:
    path = tmp_path / "blobs.json"
    path.write_text(json.dumps({k: base64.b64encode(v).decode() for k, v in blobs.objects.items()}))
    args = {
        "service": service,
        "action_id": action_id,
        "release": url,
        "blobs": str(path),
        "public": PUBLIC,
        "timeout": timeout,
    }
    return subprocess.Popen(
        [sys.executable, "-c", CHILD, json.dumps(args)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=os.environ.copy(),
    )


def kill_when(child: subprocess.Popen, ready, within: float = 30) -> None:
    """SIGKILL the child once `ready()` holds."""
    deadline = time.monotonic() + within
    while not ready():
        assert child.poll() is None, child.stdout.read() if child.stdout else ""
        assert time.monotonic() < deadline, "the child never reached the step"
        time.sleep(0.1)
    os.kill(child.pid, signal.SIGKILL)
    child.wait(10)
    assert child.returncode == -signal.SIGKILL


def applies(release, key: str) -> int:
    return sum(1 for h in release.history if h.get("execution_key") == key and h["kind"] == "apply")


def restarted(dispatcher_engine, service, url, blobs):
    store = PgTrustedStore(dispatcher_engine, blobs)
    return dispatcher(dispatcher_engine, service, client(url, store), blobs, timeout=CALL_TIMEOUT_S)


def test_killed_before_the_release_saw_it_is_sent_again_with_the_same_key(
    tmp_path, app_engine, dispatcher_engine, approved, released, blackhole
):
    service, _, action_id, blobs = approved
    release, url = released
    child = start_child(tmp_path, service, str(action_id), blackhole, blobs)
    kill_when(child, lambda: attempts(app_engine, action_id) == [(1, "started", False)])
    assert status(app_engine, action_id) == "applying"
    time.sleep(WINDOW_S + 1)  # past the in-flight window: the first call is dead
    result = asyncio.run(restarted(dispatcher_engine, service, url, blobs).execute(str(action_id)))
    assert result.outcome == Outcome.APPLIED, result.reason
    assert attempts(app_engine, action_id) == [(1, "applied", False)]
    assert applies(release, str(action_id)) == 1 and status(app_engine, action_id) == "executed"


def test_killed_twice_in_a_row_is_applied_once(
    tmp_path, app_engine, dispatcher_engine, approved, released, blackhole
):
    service, _, action_id, blobs = approved
    release, url = released
    first = start_child(tmp_path, service, str(action_id), blackhole, blobs)
    kill_when(first, lambda: attempts(app_engine, action_id) == [(1, "started", False)])
    time.sleep(WINDOW_S + 1)
    # A second dispatcher, its write also held, finds the row dead (its window is 9 s), sees
    # the key unknown and the target at base, sends again, and is killed during that call.
    second = start_child(tmp_path, service, str(action_id), blackhole, blobs, CALL_TIMEOUT_S)
    kill_when(second, lambda: len(HELD[blackhole]) == 2)  # killed mid re-send
    assert attempts(app_engine, action_id) == [(1, "started", False)]
    result = asyncio.run(restarted(dispatcher_engine, service, url, blobs).execute(str(action_id)))
    assert result.outcome == Outcome.APPLIED, result.reason
    assert applies(release, str(action_id)) == 1
    assert attempts(app_engine, action_id) == [(1, "applied", False)]


def test_killed_after_the_release_applied_reconciles_with_no_resend(
    tmp_path, app_engine, dispatcher_engine, approved, released
):
    service, _, action_id, blobs = approved
    release, url = released
    release.faults["apply_delay_s"] = 3.0  # the release process applies, then answers late
    child = start_child(tmp_path, service, str(action_id), url, blobs)
    kill_when(child, lambda: release.in_progress == str(action_id))
    time.sleep(4)  # the release process finishes applying with nobody listening
    assert release.lookup(str(action_id)) == {"status": "applied"}
    release.faults["apply_delay_s"] = 0.0
    time.sleep(WINDOW_S)
    result = asyncio.run(restarted(dispatcher_engine, service, url, blobs).execute(str(action_id)))
    assert result.outcome == Outcome.APPLIED, result.reason
    assert applies(release, str(action_id)) == 1  # nothing re-sent
    assert attempts(app_engine, action_id) == [(1, "unknown", False), (2, "applied", True)]
    assert status(app_engine, action_id) == "executed"
