"""LOCAL USE ONLY (Artifact 13.5 T3.1, ADR-73): a fake release process for the synthetic orders-api.

Stands in for the real release process, which is not confirmed (T3.1). It holds what one
deployment runs, `orders-api-production`: a revision and the configuration files the platform
may change, starting at the reference repository's `main` (ops/reference-app/build_repo.py,
built at start-up as fake-review does). It answers the three keyed operations Layer 11's
dispatcher uses (dispatcher.ReleaseProcess), with the action ID as the execution key (ADR-47):

    GET  /healthz
    GET  /v1/deployments/{dep}                        revision, in-progress key, configuration
                                                      and the history of what ran when (open:
                                                      monitoring reads it, ADR-73)
    POST /v1/deployments/{dep}/apply                  {execution_key, base_revision,
                                                       patch_sha256, to_revision, patch}
    POST /v1/deployments/{dep}/undo                   {execution_key, base_revision, to_revision}
    GET  /v1/deployments/{dep}/changes/{key}          {"status": applied|failed|in_progress}, or 404
    POST /_fake/deployments/{dep}/config              {"set": {KEY: value}, "revision": ...}: an
                                                      out-of-band change, for tests and the demo
    POST /_fake/faults                                {"apply_delay_s": n}: answer applies late
    POST /_fake/reset                                 back to the start

Apply, undo and lookup need `Authorization: Bearer <FAKE_RELEASE_TOKEN>`, the release
credential only the dispatcher process holds. A key is applied at most once: a repeat answers
what the first did and changes nothing. An apply carries the patch's canonical bytes as text
(`patch`): their SHA-256 must be `patch_sha256`, each file's content must hash to its
`old_sha256`, and the hunks must give its `new_sha256`, else `rejected`. The deployment must run
`base_revision`, else `rejected`. An undo puts back a revision this process ran before.

FAKE_REVIEW_REPO_SOURCE (or ../reference-app/build_repo.py), FAKE_RELEASE_BRANCH (default
main), FAKE_RELEASE_TOKEN (required), FAKE_RELEASE_PORT (default 8080). Refuses to start when
AISRE_ENV is production. Standard library and `git` (through review.py) only.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

HERE = Path(__file__).resolve().parent
ENVIRONMENT = "production"
CONFIG = "deploy/production.env"
MAX_DELAY_S = 120.0


def _review() -> Any:
    spec = importlib.util.spec_from_file_location("fake_review_for_release", HERE / "review.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


review = _review()
DEPLOYMENT = f"{review.REPOSITORY}-{ENVIRONMENT}"


def parse_env(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class Release:
    def __init__(self, repo: Any, branch: str = "main") -> None:
        self.repo, self.branch = repo, branch
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        head = self.repo.resolve(self.branch)
        if head is None:
            raise SystemExit(f"no branch {self.branch}")
        body = self.repo.blob(head, CONFIG)
        files = {CONFIG: body.decode() if body is not None else ""}
        self.revision: str = head
        self.files: dict[str, str] = files
        self.snapshots: dict[str, dict[str, str]] = {head: dict(files)}
        self.in_progress: str | None = None
        self.changes: dict[str, dict[str, Any]] = {}
        self.history: list[dict[str, Any]] = []
        self.faults: dict[str, float] = {"apply_delay_s": 0.0}
        self._record(None)

    def _record(self, key: str | None) -> None:
        self.history.append(
            {
                "at": now_iso(),
                "revision": self.revision,
                "execution_key": key,
                "config": parse_env(self.files.get(CONFIG, "")),
            }
        )

    def state(self) -> dict[str, Any]:
        with self.lock:
            return {
                "deployment": DEPLOYMENT,
                "service": review.REPOSITORY,
                "environment": ENVIRONMENT,
                "revision": self.revision,
                "in_progress_key": self.in_progress,
                "config": parse_env(self.files.get(CONFIG, "")),
                "history": copy.deepcopy(self.history),
            }

    def _content(self, path: str) -> str | None:
        if path in self.files:
            return self.files[path]
        body = self.repo.blob(self.snapshot_base(), path)
        return body.decode() if body is not None else None

    def snapshot_base(self) -> str:
        """The git commit the deployment started from: files not held are read there."""
        return str(self.history[0]["revision"])

    def _patched(self, body: dict[str, Any]) -> dict[str, str] | str:
        """The files after the patch, or why it can't apply."""
        text = body.get("patch")
        if not isinstance(text, str):
            return "an apply carries the patch's canonical bytes"
        if hashlib.sha256(text.encode()).hexdigest() != body.get("patch_sha256"):
            return "the patch bytes do not hash to patch_sha256"
        try:
            patch = json.loads(text)
            new = dict(self.files)
            for f in patch.get("files") or []:
                path = str(f["path"])
                old = self._content(path)
                if old is None or hashlib.sha256(old.encode()).hexdigest() != f["old_sha256"]:
                    return f"{path} is not what the patch was made against"
                after = review.apply_file(old, f["hunks"])
                if hashlib.sha256(after.encode()).hexdigest() != f["new_sha256"]:
                    return f"{path} after the patch is not the patch's result"
                new[path] = after
        except (KeyError, TypeError, ValueError) as e:
            return f"the patch does not apply: {e}"
        return new

    def change(self, kind: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        key = body.get("execution_key")
        to = body.get("to_revision")
        if not isinstance(key, str) or not key or not isinstance(to, str) or not to:
            return 422, {"message": "execution_key and to_revision are required"}
        with self.lock:
            seen = self.changes.get(key)
            if seen is not None:  # at most once per key (ADR-47)
                answer = {"applied": "applied", "in_progress": "in_progress"}
                return 200, {"result": answer.get(seen["status"], "rejected")}
            if self.in_progress is not None:
                return 200, {"result": "in_progress", "in_progress_key": self.in_progress}
            why = None
            if body.get("base_revision") != self.revision:
                why = f"deployment runs {self.revision}, not {body.get('base_revision')}"
            files: dict[str, str] | str = {}
            if why is None and kind == "apply":
                files = self._patched(body)
            elif why is None:
                files = self.snapshots.get(to) or f"{to} never ran here; nothing to undo to"
            if why is None and isinstance(files, str):
                why = files
            if why is not None:
                self.changes[key] = {"status": "failed", "kind": kind, "reason": why}
                return 200, {"result": "rejected", "reason": why}
            self.changes[key] = {"status": "in_progress", "kind": kind}
            self.in_progress = key
            delay = self.faults["apply_delay_s"]
        if delay:
            time.sleep(delay)  # the call outlives the caller's timeout: a late apply
        with self.lock:
            assert isinstance(files, dict)
            self.files, self.revision, self.in_progress = dict(files), to, None
            self.snapshots.setdefault(to, dict(files))
            self.changes[key] = {"status": "applied", "kind": kind, "at": now_iso()}
            self._record(key)
        return 200, {"result": "applied", "revision": to}

    def lookup(self, key: str) -> dict[str, Any] | None:
        with self.lock:
            found = self.changes.get(key)
            return None if found is None else {"status": found["status"]}

    def out_of_band(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """A change nobody asked the platform for: ST-40's case, and the demo's bad change."""
        values = body.get("set")
        if not isinstance(values, dict) or not values:
            return 422, {"message": "set names the keys to change"}
        with self.lock:
            lines = self.files.get(CONFIG, "").splitlines()
            for k, v in values.items():
                lines = [f"{k}={v}" if ln.startswith(f"{k}=") else ln for ln in lines]
            self.files[CONFIG] = "\n".join(lines) + "\n"
            self.revision = str(body.get("revision") or f"{self.revision}+manual")
            self.snapshots.setdefault(self.revision, dict(self.files))
            self._record(None)
        return 200, {"revision": self.revision}


def handler_for(release: Release, token: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # no tokens in logs
            sys.stderr.write(f"fake-release {self.command} {self.path.split('?')[0]}\n")

        def reply(self, status: int, body: Any) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def authorised(self) -> bool:
            if self.headers.get("Authorization") == f"Bearer {token}":
                return True
            self.reply(401, {"message": "the release credential is required"})
            return False

        def parts(self) -> list[str]:
            return [unquote(p) for p in urlsplit(self.path).path.strip("/").split("/")]

        def do_GET(self) -> None:
            p = self.parts()
            if p == ["healthz"]:
                return self.reply(200, {"status": "ok"})
            if len(p) >= 3 and p[:2] == ["v1", "deployments"] and p[2] != DEPLOYMENT:
                return self.reply(404, {"message": "no such deployment"})
            if len(p) == 3 and p[:2] == ["v1", "deployments"]:
                return self.reply(200, release.state())
            if len(p) == 5 and p[:2] == ["v1", "deployments"] and p[3] == "changes":
                if not self.authorised():
                    return None
                found = release.lookup(p[4])
                return self.reply(200, found) if found else self.reply(404, {"message": "none"})
            self.reply(404, {"message": "not found"})

        def do_POST(self) -> None:
            p = self.parts()
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("not an object")
            except ValueError:
                return self.reply(400, {"message": "body is not a JSON object"})
            if p == ["_fake", "reset"]:
                release.reset()
                return self.reply(200, {"revision": release.revision})
            if p == ["_fake", "faults"]:
                delay = float(body.get("apply_delay_s", 0))
                release.faults["apply_delay_s"] = min(max(delay, 0.0), MAX_DELAY_S)
                return self.reply(200, release.faults)
            if len(p) == 4 and p[:2] == ["_fake", "deployments"] and p[3] == "config":
                if p[2] != DEPLOYMENT:
                    return self.reply(404, {"message": "no such deployment"})
                return self.reply(*release.out_of_band(body))
            if len(p) == 4 and p[:2] == ["v1", "deployments"] and p[3] in ("apply", "undo"):
                if not self.authorised():
                    return None
                if p[2] != DEPLOYMENT:
                    return self.reply(404, {"message": "no such deployment"})
                return self.reply(*release.change(p[3], body))
            self.reply(404, {"message": "not found"})

    return Handler


def serve(
    release: Release, token: str, port: int = 0, host: str = "127.0.0.1"
) -> ThreadingHTTPServer:
    """Start serving on a daemon thread; `server.server_address` has the port."""
    server = ThreadingHTTPServer((host, port), handler_for(release, token))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> None:
    if os.environ.get("AISRE_ENV") == "production":
        sys.exit("fake release process: refusing to run with AISRE_ENV=production")
    token = os.environ.get("FAKE_RELEASE_TOKEN") or sys.exit("FAKE_RELEASE_TOKEN is required")
    port = int(os.environ.get("FAKE_RELEASE_PORT", "8080"))
    branch = os.environ.get("FAKE_RELEASE_BRANCH") or "main"
    source = Path(os.environ.get("FAKE_REVIEW_REPO_SOURCE") or review.SOURCE)
    dest = Path(tempfile.mkdtemp(prefix="fake-release-")) / review.REPOSITORY
    review.build(dest, source)
    release = Release(review.Repo(dest, branch), branch)
    print(
        f"fake release process on :{port}, {DEPLOYMENT} at {release.revision} (local use only)",
        flush=True,
    )
    ThreadingHTTPServer(("0.0.0.0", port), handler_for(release, str(token))).serve_forever()


if __name__ == "__main__":
    main()
