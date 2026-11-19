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

Fence, backup and restore (ADR-69, T4.4) are the exception: no API route carries them, so
these connect to PostgreSQL directly (AISRE_DATABASE_URL) and the rules are applied here, by
aisre.reliability.admin. The caller is AISRE_TOKEN, verified against AISRE_OIDC_*; a lead's
sign-off is a second verified token, AISRE_SIGN_OFF_TOKEN (a stand-in for a signed record,
ADR-74). Refused (exit 1) if either is missing, invalid or holds the wrong role:

    fence show                          the epoch and every active fence reason
    fence set --why W [--deployment D]  a manual fence (incident lead or platform admin)
    fence lift [--deployment D]         platform admin, with an incident lead's sign-off
    backup take DIR [--command C]       quiesced base backup and its marker (platform admin)
    backup check DIR [--prune]          days missed in the last week; prune past 35 days
    restore begin                       first write on a restored database (platform admin)
    restore report                      reconcile actions not final against the release process
    restore complete [--decide A=O]     reconcile, then lift the restore fence (lead signs,
                                        platform admin runs); --decide is the lead's decision
    document purge-text ID --reason R   remove a document version's text (security lead), as
                                        AISRE_RETENTION_DATABASE_URL; refused until the legal
                                        deletion policy is recorded in AISRE_LEGAL_DELETION_DECISION

The pilot's commands (M5, ADR-75) are aisre.pilot.cli's: `pilot check`, the go-live check.
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

from aisre.pilot import cli as pilot_cli

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


ADMIN = ("fence", "backup", "restore", "document")


def admin_parsers(top: Any) -> None:
    fence = top.add_parser("fence", help="The production-write fence (ADR-69, 12.5)")
    fs = fence.add_subparsers(dest="command", required=True)
    fs.add_parser("show", help="The epoch and the active fence reasons")
    f = fs.add_parser("set", help="Set a manual fence (incident lead or platform admin)")
    f.add_argument("--why", required=True)
    f.add_argument("--deployment")
    f = fs.add_parser("lift", help="Lift a manual fence (platform admin, a lead signs off)")
    f.add_argument("--deployment")
    backup = top.add_parser("backup", help="Backups (12.6, NFR-46)")
    bs = backup.add_subparsers(dest="command", required=True)
    b = bs.add_parser("take", help="A quiesced base backup and its marker")
    b.add_argument("dest")
    b.add_argument(
        "--command", dest="backup_command", help="Default AISRE_BACKUP_COMMAND; {id}, {dest}"
    )
    b.add_argument("--id")
    b = bs.add_parser("check", help="Days without a backup; backups past 35 days")
    b.add_argument("dest")
    b.add_argument("--days", type=int, default=7)
    b.add_argument("--prune", action="store_true")
    restore = top.add_parser("restore", help="Restore steps run on the restored database")
    rs = restore.add_subparsers(dest="command", required=True)
    rs.add_parser("begin", help="New fence epoch and the restore fence (platform admin)")
    rs.add_parser("report", help="Reconcile actions not final, by lookup only")
    r = rs.add_parser("complete", help="Reconcile, then lift the restore fence")
    r.add_argument("--decide", action="append", default=[], metavar="ACTION=OUTCOME")
    document = top.add_parser("document", help="Runbook text (T4.11, behind the legal decision)")
    ds = document.add_subparsers(dest="command", required=True)
    d = ds.add_parser("purge-text", help="Remove one document version's text (security lead)")
    d.add_argument("document_id")
    d.add_argument("--reason", required=True)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aisre", description="The AI SRE platform's CLI.")
    top = p.add_subparsers(dest="noun", required=True)
    admin_parsers(top)
    pilot_cli.parsers(top)
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
    env = os.environ if environ is None else environ
    if args.noun == "pilot":
        return pilot_cli.main(args, env, out or sys.stdout)
    if args.noun in ADMIN:
        from aisre.reliability import admin_cli  # PostgreSQL, only for these commands

        return admin_cli.main(args, env, out or sys.stdout)
    api = Api(env)
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
