"""M4 fault tests' handle on the Compose stack: stop, kill and start this project's services only.

Every call goes through `docker compose --env-file .env.example` in the repository, so it can
only touch project `aisre`'s containers; nothing here prunes or names another Docker resource.
`restore()` brings every long-running service back and waits for its health check, and fault
tests call it in a `finally`, so the stack is left up and healthy.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

ROOT = Path(__file__).parents[2]
COMPOSE = ["docker", "compose", "--env-file", ".env.example"]
ONE_SHOT = {"temporal-schema", "temporal-namespace", "migrate", "corpus-seed", "minio-bucket"}
API = "http://127.0.0.1:8000"
PAGER = "http://127.0.0.1:59600"
ALERTMANAGER = "http://127.0.0.1:59093"
PROMETHEUS = "http://127.0.0.1:59090"
ISSUER = "http://127.0.0.1:59100"
RELEASE = "http://127.0.0.1:59500"
T = TypeVar("T")


def compose(*args: str, timeout: float = 300, overrides: dict[str, str] | None = None) -> str:
    """The test's own AISRE_* settings point at the host's ports; Compose would take them over
    .env.example when it recreates a container, so they are left out of its environment.
    `overrides` are set on purpose, for one call (a worker restarted with other settings)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AISRE_", "OTEL_"))}
    env.update(overrides or {})
    out = subprocess.run(
        [*COMPOSE, *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
    )
    if out.returncode != 0:
        raise RuntimeError(f"docker compose {' '.join(args)}: {out.stderr[-2000:]}")
    return out.stdout


def available() -> str | None:
    """None if the stack answers, else why not (the caller skips)."""
    try:
        running = compose("ps", "--services", "--status", "running", timeout=30).split()
    except (OSError, subprocess.SubprocessError, RuntimeError) as e:
        return f"docker compose unavailable: {type(e).__name__}"
    missing = {"api", "worker", "dispatcher", "postgres", "temporal", "prometheus"} - set(running)
    return f"stack services not running: {sorted(missing)}" if missing else None


def services() -> list[str]:
    return [s for s in compose("config", "--services", timeout=60).split() if s not in ONE_SHOT]


def stop(*names: str) -> None:
    compose("stop", "-t", "10", *names)


def kill(*names: str) -> None:
    """SIGKILL: no shutdown handler runs (Q13.32)."""
    compose("kill", "-s", "SIGKILL", *names)


def start(*names: str, overrides: dict[str, str] | None = None) -> None:
    compose("up", "-d", "--no-deps", "--wait", *names, timeout=600, overrides=overrides)


def restore() -> None:
    """Every long-running service up and healthy (or running, where it has no check)."""
    compose("up", "-d", "--no-deps", "--wait", *services(), timeout=900)


def http(
    method: str, url: str, body: Any = None, headers: dict[str, str] | None = None
) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)  # noqa: S310 (local stack)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw.decode(errors="replace")
    except OSError as e:
        return 0, f"{type(e).__name__}: {e}"


def wait_for(check: Callable[[], T | None], within: float, what: str, every: float = 2) -> T:
    """Polls `check` until it returns something truthy; fails after `within` seconds."""
    deadline = time.monotonic() + within
    while True:
        got = check()
        if got:
            return got
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {within:.0f} s waiting for {what}")
        time.sleep(every)


def token(persona: str) -> str:
    status, body = http("POST", f"{ISSUER}/token", {"persona": persona})
    assert status == 200, body
    return str(body["access_token"])


def token_for(sub: str, groups: list[str], mfa: bool = True) -> str:
    """A FAKE issuer token for any subject and groups (local use only)."""
    status, body = http("POST", f"{ISSUER}/token", {"sub": sub, "groups": groups, "mfa": mfa})
    assert status == 200, body
    return str(body["access_token"])


def pages(**match: str) -> list[dict[str, Any]]:
    status, body = http("GET", f"{PAGER}/v1/pages?" + urllib.parse.urlencode(match))
    return list(body["pages"]) if status == 200 else []


def now_iso() -> str:
    """The pager stamps each page in UTC ISO form; pages at or after this came later."""
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def pages_since(since: str, **match: str) -> list[dict[str, Any]]:
    """Alertmanager sends a still-firing group again only after repeat_interval, so a test
    looks for a page newer than its fault, or (pages()) for any page of that alert."""
    return [p for p in pages(**match) if p["received_at"] >= since]


def firing(alertname: str) -> bool:
    status, body = http("GET", f"{PROMETHEUS}/api/v1/alerts")
    if status != 200:
        return False
    return any(
        a["labels"]["alertname"] == alertname and a["state"] == "firing"
        for a in body["data"]["alerts"]
    )


GROUP_INTERVAL_S = 60  # ops/compose/alertmanager.yml


def quiet_alert(alertname: str, within: float = 600) -> None:
    """Waits until `alertname` is neither firing in Prometheus nor active in Alertmanager, then
    one group interval more, so Alertmanager has sent (or dropped) its resolution. Otherwise a
    new firing of the same alert looks like the one already notified, and isn't sent again
    before the repeat interval (Alertmanager's own de-duplication)."""

    def clear() -> bool:
        if firing(alertname):
            return False
        status, body = http("GET", f"{ALERTMANAGER}/api/v2/alerts?active=true")
        return status == 200 and not any(a["labels"]["alertname"] == alertname for a in body)

    wait_for(clear, within, f"{alertname} to clear", every=5)
    time.sleep(GROUP_INTERVAL_S + 10)
