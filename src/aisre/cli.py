"""Artifact 7.4, 13.5 T1.5 (FR-38): the command-line tool, `python -m aisre.cli`.

A thin client of the HTTP API: it holds no database or Temporal access, so it can do nothing
the caller's own roles don't allow, and every refusal is the API's (ADR-50).

    incident submit FILE [--key K]      POST /v1/incidents (an IncidentCreate, or alert.json)
    incident show ID                    GET  /v1/incidents/{id}
    incident timeline ID [--follow]     the timeline; --follow streams it until it ends (SSE)
    incident cancel ID --reason R       POST .../cancellation
    incident close ID [--note N]        POST .../closure, with the incident's ETag as If-Match

Settings come from the environment:

    AISRE_API_URL       default http://127.0.0.1:8000
    AISRE_TOKEN         a bearer token (the stack's fake issuer mints one: README, Sign-in)
    AISRE_DEV_CALLER    DEVELOPMENT ONLY, instead of a token: the X-Aisre-Dev-Caller header,
                        with AISRE_DEV_GROUPS (and AISRE_DEV_ORG) beside it

Writes send an `Idempotency-Key`: `--key`, else a new random one, so a retried command
replays the first answer only when the key is given. Output is the API's JSON, one document
per line for the timeline. Exit status: 0 on success, 1 when the API refuses (its problem
document goes to standard error), 2 on a usage error.

Fence and restore (ADR-69 gives them to the CLI, run by a Platform admin, with a lead's
sign-off to lift a manual fence) are not here yet: they need the caller checked against
ADR-50 and the lead's sign-off recorded, and no API route or signed record carries either.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections.abc import Iterator, Mapping, Sequence
from typing import IO, Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_URL = "http://127.0.0.1:8000"
TIMEOUT_S = 30


class ApiRefused(Exception):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"{status}")
        self.status, self.body = status, body


class Api:
    def __init__(self, environ: Mapping[str, str]) -> None:
        self.base = environ.get("AISRE_API_URL", DEFAULT_URL).rstrip("/")
        self.auth: dict[str, str] = {}
        if token := environ.get("AISRE_TOKEN"):
            self.auth["Authorization"] = f"Bearer {token}"
        elif caller := environ.get("AISRE_DEV_CALLER"):
            self.auth["X-Aisre-Dev-Caller"] = caller
            for var, header in (
                ("AISRE_DEV_GROUPS", "X-Aisre-Dev-Groups"),
                ("AISRE_DEV_ORG", "X-Aisre-Dev-Org"),
            ):
                if environ.get(var):
                    self.auth[header] = environ[var]

    def call(
        self,
        method: str,
        path: str,
        body: Any = None,
        headers: Mapping[str, str] | None = None,
        *,
        stream: bool = False,
    ) -> Any:
        data = None if body is None else json.dumps(body).encode()
        h = {"Accept": "application/json", **self.auth, **(headers or {})}
        if data is not None:
            h["Content-Type"] = "application/json"
        req = Request(self.base + path, data=data, method=method, headers=h)
        try:
            resp = urlopen(req, timeout=None if stream else TIMEOUT_S)  # noqa: S310 (the API's URL)
        except HTTPError as e:
            raise ApiRefused(e.code, e.read().decode(errors="replace")) from None
        if stream:
            return resp
        with resp:
            return resp.status, dict(resp.headers), json.loads(resp.read() or b"null")


def key(given: str | None) -> dict[str, str]:
    return {"Idempotency-Key": given or f"cli-{uuid.uuid4()}"}


def sse_data(lines: Iterator[bytes]) -> Iterator[str]:
    """The `data:` of each SSE message; comments and other fields are skipped."""
    for raw in lines:
        line = raw.decode().rstrip("\r\n")
        if line.startswith("data: "):
            yield line[len("data: ") :]


def run(args: argparse.Namespace, api: Api, out: IO[str]) -> None:
    base = f"/v1/incidents/{args.id}" if getattr(args, "id", None) else "/v1/incidents"
    if args.command == "submit":
        with open(args.file, encoding="utf-8") as f:
            body = json.load(f)
        _, _, made = api.call("POST", "/v1/incidents", body, key(args.key))
        print(json.dumps(made), file=out)
    elif args.command == "show":
        print(json.dumps(api.call("GET", base)[2]), file=out)
    elif args.command == "timeline" and args.follow:
        with api.call(
            "GET", base + "/timeline/stream", headers={"Accept": "text/event-stream"}, stream=True
        ) as resp:
            for data in sse_data(iter(resp.readline, b"")):
                print(data, file=out, flush=True)
    elif args.command == "timeline":
        after = 0
        while True:
            page = api.call("GET", f"{base}/timeline?after_seq={after}&limit=500")[2]
            for event in page["items"]:
                print(json.dumps(event), file=out)
            if page["next_after_seq"] is None:
                break
            after = page["next_after_seq"]
    elif args.command == "cancel":
        made = api.call("POST", base + "/cancellation", {"reason": args.reason}, key(args.key))
        print(json.dumps(made[2]), file=out)
    elif args.command == "close":
        _, headers, _ = api.call("GET", base)
        body = {"note": args.note} if args.note else {}
        h = key(args.key) | {"If-Match": headers.get("ETag") or headers.get("etag") or ""}
        print(json.dumps(api.call("POST", base + "/closure", body, h)[2]), file=out)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aisre", description="The AI SRE platform's CLI.")
    top = p.add_subparsers(dest="noun", required=True)
    incident = top.add_parser("incident", help="Incidents")
    sub = incident.add_subparsers(dest="command", required=True)
    s = sub.add_parser("submit", help="Submit an alert or an IncidentCreate from a JSON file")
    s.add_argument("file")
    s.add_argument("--key")
    sub.add_parser("show", help="An incident's current state").add_argument("id")
    t = sub.add_parser("timeline", help="An incident's timeline")
    t.add_argument("id")
    t.add_argument("--follow", action="store_true", help="Stream it until it ends")
    c = sub.add_parser("cancel", help="Cancel an investigation (a stop, ADR-69)")
    c.add_argument("id")
    c.add_argument("--reason", required=True)
    c.add_argument("--key")
    cl = sub.add_parser("close", help="Accept the outcome and close (incident lead)")
    cl.add_argument("id")
    cl.add_argument("--note")
    cl.add_argument("--key")
    return p


def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    out: IO[str] | None = None,
) -> int:
    args = parser().parse_args(argv)
    api = Api(os.environ if environ is None else environ)
    try:
        run(args, api, out or sys.stdout)
    except ApiRefused as e:
        print(e.body or f"HTTP {e.status}", file=sys.stderr)
        return 1
    except (URLError, OSError) as e:
        print(f"aisre: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
