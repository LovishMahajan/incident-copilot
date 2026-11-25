"""LOCAL USE ONLY (Artifact 13.5 T1.1): a fake monitoring source, serving synthetic scenarios.

The read-only systems the Evidence Collector's adapters query (T1.10, aisre.evidence.sources):
metrics, logs, an error tracker, traces, changes, dependency health and the service catalogue. Each scenario is a folder of fixture files, in the
reference incident's layout (tests/fixtures/reference-incident/). The answer key is never
served: it is what an investigation must find, not something monitoring knows.

    GET /v1/scenarios                         names of the scenario folders
    GET /v1/scenarios/<name>/alert            alert.json
    GET /v1/scenarios/<name>/metrics          metrics.csv as a list of rows
    GET /v1/scenarios/<name>/logs             logs.ndjson as a list of records
    GET /v1/scenarios/<name>/traces           trace.json
    GET /v1/scenarios/<name>/changes          release.json, plus config-diff.txt as `config_diff`
    GET /v1/scenarios/<name>/catalog          service-catalog.json
    GET /v1/scenarios/<name>/errors           error groups: logs.ndjson's errors grouped by type
                                              and message, as an error tracker would (the
                                              reference incident has no tracker of its own)
    GET /v1/scenarios/<name>/dependencies     dependency-health.json
    GET /healthz

Each case of an evaluation case store under `eval/` (Artifact 12.8) is a scenario too, named by
its case ID and served from its `fixture/` folder.

A scenario name may withhold files, for runs like the reference incident's honesty variant:
`reference-incident:without=release.json,config-diff.txt` serves that folder as if those files
were missing, so `changes` is 404 and its adapter reports the source unavailable.

Optional `start` and `end` (ISO 8601, UTC) filter metrics by `minute_utc`, logs by `ts` and
error groups by the logs they are built from. A missing fixture file is 404, which an adapter
reports as an unavailable source, never as an empty one (ADR-54).

Live metrics (M3, T3.1, ADR-73), for the deployment the fake release process holds:

    GET /v1/live/<deployment>/metrics?start=&end=   one row a minute: requests, HTTP 500s and
                                                    their rate, and the revision running then
    GET /v1/live/<deployment>/errors?start=&end=    error groups over the same minutes, as an
                                                    error tracker would group them (T3.17)

Each minute takes the configuration the release process's history says was running then. A
minute whose `ORDER_TAX_RATE_BPS` isn't an integer fails as the reference application does
(`int()` on every order, ops/reference-app), at the incident's rate; any other minute is the
baseline. A failing minute's errors are one group, `ValueError` with the value `int()` choked
on, so a different bad value is a new group. So applying the config revert brings the rate down and a bad change brings it up,
and what the Verifier reads comes from here, never from the release process's own answer. The
window defaults to the last 30 minutes and is at most 24 hours. FAKE_MONITORING_RELEASE_URL is
the release process's base URL; unset, live metrics are 404.

FAKE_MONITORING_FIXTURES names the folder holding the scenarios (default: tests/fixtures in
this repository); FAKE_MONITORING_PORT the port (default 8080). Standard library only.
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
import threading
import urllib.request
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

FIXTURES = Path(__file__).parents[2] / "tests/fixtures"


def _comment_free(path: Path) -> list[str]:
    return [ln for ln in path.read_text().splitlines() if ln.strip() and not ln.startswith("#")]


def _in_window(stamp: str, start: str | None, end: str | None) -> bool:
    return (start is None or stamp >= start) and (end is None or stamp <= end)


NEEDS = {  # the file each kind can't be served without
    "alert": "alert.json",
    "metrics": "metrics.csv",
    "logs": "logs.ndjson",
    "errors": "logs.ndjson",
    "traces": "trace.json",
    "changes": "release.json",
    "catalog": "service-catalog.json",
    "dependencies": "dependency-health.json",
}


def scenarios(root: Path) -> dict[str, Path]:
    """Scenario name -> folder: each folder holding an alert.json, and each case of an evaluation
    case store (Artifact 12.8, `eval/<suite>/cases/<case_id>/fixture/`), by its case ID."""
    found = {p.parent.name: p.parent for p in root.glob("*/alert.json")}
    return found | {
        p.parents[1].name: p.parent for p in root.glob("eval/*/cases/*/fixture/alert.json")
    }


def read(root: Path, scenario: str, kind: str, query: dict[str, str]) -> Any:
    """The answer for one scenario and kind. Raises FileNotFoundError or KeyError."""
    scenario, _, option = scenario.partition(":")
    withheld = set(option.removeprefix("without=").split(",")) if option else set()
    if option and not option.startswith("without="):
        raise KeyError(option)
    d = scenarios(root).get(scenario)
    if d is None or NEEDS[kind] in withheld:
        raise FileNotFoundError(scenario)
    start, end = query.get("start"), query.get("end")
    if kind == "metrics":
        rows = csv.DictReader(_comment_free(d / "metrics.csv"))
        return [r for r in rows if _in_window(r["minute_utc"], start, end)]
    if kind == "logs":
        records = [json.loads(ln) for ln in _comment_free(d / "logs.ndjson")]
        return [r for r in records if _in_window(r["ts"], start, end)]
    if kind == "errors":
        records = [json.loads(ln) for ln in _comment_free(d / "logs.ndjson")]
        return _error_groups([r for r in records if _in_window(r["ts"], start, end)])
    if kind == "changes":
        diff = d / "config-diff.txt"
        changes = json.loads((d / "release.json").read_text())
        shown = diff.exists() and diff.name not in withheld
        return changes | {"config_diff": diff.read_text() if shown else None}
    files = {
        "alert": "alert.json",
        "traces": "trace.json",
        "catalog": "service-catalog.json",
        "dependencies": "dependency-health.json",
    }
    return json.loads((d / files[kind]).read_text())


REQUESTS_PER_MINUTE = 1200
FAILING_RATE = 0.61  # v2.14.1's rate in the reference incident's metrics.csv
BASELINE_RATE = 0.0008
LIVE_DEFAULT = timedelta(minutes=30)
LIVE_MAX = timedelta(hours=24)
LIVE_KINDS = ("metrics", "errors")


def failing(config: dict[str, str]) -> bool:
    """The reference application's failure: `int(settings.order_tax_rate_bps)` on every order."""
    try:
        int(config.get("ORDER_TAX_RATE_BPS", ""))
    except ValueError:
        return True
    return False


def _stamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def live_metrics(
    history: list[dict[str, Any]], start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """One row per minute from `start` to `end`, from what the release history says ran then."""
    changes = sorted(((_stamp(h["at"]), h) for h in history), key=lambda c: c[0])
    rows: list[dict[str, Any]] = []
    minute = start.replace(second=0, microsecond=0)
    while minute <= end:
        running = [h for at, h in changes if at <= minute + timedelta(seconds=59)]
        if running:
            state = running[-1]
            rate = FAILING_RATE if failing(state.get("config") or {}) else BASELINE_RATE
            errors = round(REQUESTS_PER_MINUTE * rate)
            rows.append(
                {
                    "minute_utc": minute.isoformat().replace("+00:00", "Z"),
                    "requests_total": REQUESTS_PER_MINUTE,
                    "http_500_total": errors,
                    "rate_500_total": round(errors / REQUESTS_PER_MINUTE, 5),
                    "revision": state["revision"],
                }
            )
        minute += timedelta(minutes=1)
    return rows


def live_errors(
    history: list[dict[str, Any]], start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Error groups over the minutes live_metrics serves: each failing minute's errors fall in
    the group of the value `int()` was given, with first and last sighting and a count."""
    changes = sorted(((_stamp(h["at"]), h) for h in history), key=lambda c: c[0])
    groups: dict[str, dict[str, Any]] = {}
    for row in live_metrics(history, start, end):
        if not row["http_500_total"] or row["rate_500_total"] < FAILING_RATE / 2:
            continue
        minute = _stamp(row["minute_utc"])
        config = [h for at, h in changes if at <= minute + timedelta(seconds=59)][-1]["config"]
        value = (config or {}).get("ORDER_TAX_RATE_BPS", "")
        message = f"invalid literal for int() with base 10: '{value}'"
        g = groups.setdefault(
            message,
            {
                "type": "ValueError",
                "message": message,
                "service": "orders-api",
                "count": 0,
                "first_seen": row["minute_utc"],
                "last_seen": row["minute_utc"],
            },
        )
        g["count"] += row["http_500_total"]
        g["last_seen"] = row["minute_utc"]
    return sorted(groups.values(), key=lambda g: g["first_seen"])


def read_live(
    release_url: str, deployment: str, query: dict[str, str], kind: str = "metrics"
) -> Any:
    """Live metrics or error groups for `deployment`. Raises FileNotFoundError or KeyError."""
    try:
        with urllib.request.urlopen(
            f"{release_url.rstrip('/')}/v1/deployments/{deployment}", timeout=5
        ) as r:
            state = json.loads(r.read())
    except OSError as e:  # HTTPError, URLError and timeouts
        raise FileNotFoundError(deployment) from e
    end = _stamp(query["end"]) if query.get("end") else datetime.now(UTC)
    start = _stamp(query["start"]) if query.get("start") else end - LIVE_DEFAULT
    if not start <= end or end - start > LIVE_MAX:
        raise KeyError("window")
    history = state.get("history") or []
    return (
        live_errors(history, start, end) if kind == "errors" else live_metrics(history, start, end)
    )


def _error_groups(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Errors grouped by type and message, with first and last sighting, count, versions and the
    stack's files, in order of first sighting."""
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for r in records:
        err = r.get("error")
        if not err:
            continue
        g = groups.setdefault(
            (err["type"], err["message"]),
            {
                "type": err["type"],
                "message": err["message"],
                "service": r["service"],
                "count": 0,
                "first_seen": r["ts"],
                "last_seen": r["ts"],
                "versions": [],
                "frames": [],
            },
        )
        g["count"] += 1
        g["first_seen"], g["last_seen"] = (
            min(g["first_seen"], r["ts"]),
            max(g["last_seen"], r["ts"]),
        )
        if r.get("version") and r["version"] not in g["versions"]:
            g["versions"].append(r["version"])
        for frame in re.findall(r'File "([^"]+)", line (\d+)', err.get("stack", "")):
            if list(frame) not in g["frames"]:
                g["frames"].append(list(frame))
    return sorted(groups.values(), key=lambda g: g["first_seen"])


def handler_for(root: Path, release_url: str | None = None) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            sys.stderr.write(f"fake-monitoring {self.command} {self.path.split('?')[0]}\n")

        def reply(self, status: int, body: Any) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            url = urlsplit(self.path)
            parts = url.path.strip("/").split("/")
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            if parts == ["healthz"]:
                return self.reply(200, {"status": "ok"})
            if parts == ["v1", "scenarios"]:
                return self.reply(200, sorted(scenarios(root)))
            if len(parts) == 4 and parts[:2] == ["v1", "live"] and parts[3] in LIVE_KINDS:
                if release_url:
                    try:
                        return self.reply(200, read_live(release_url, parts[2], query, parts[3]))
                    except (FileNotFoundError, KeyError, ValueError):
                        pass
            if len(parts) == 4 and parts[:2] == ["v1", "scenarios"]:
                try:
                    return self.reply(200, read(root, parts[2], parts[3], query))
                except (FileNotFoundError, KeyError):
                    pass
            self.reply(404, {"error": "not_found"})

    return Handler


def serve(
    root: Path = FIXTURES, port: int = 0, host: str = "127.0.0.1", release_url: str | None = None
) -> ThreadingHTTPServer:
    """Start serving on a daemon thread; `server.server_address` has the port."""
    server = ThreadingHTTPServer((host, port), handler_for(root, release_url))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> None:
    port = int(os.environ.get("FAKE_MONITORING_PORT", "8080"))
    root = Path(os.environ.get("FAKE_MONITORING_FIXTURES") or FIXTURES)
    release_url = os.environ.get("FAKE_MONITORING_RELEASE_URL") or None
    print(f"fake monitoring source on :{port}, scenarios in {root} (local use only)", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), handler_for(root, release_url)).serve_forever()


if __name__ == "__main__":
    main()
