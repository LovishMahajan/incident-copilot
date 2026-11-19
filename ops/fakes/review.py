"""LOCAL USE ONLY (Artifact 13.5 T2.8, ADR-72): a fake review system, in the shape of a code host.

Holds one repository, `orders-api`, built at start-up by ops/reference-app/build_repo.py, so its
commit IDs are the ones the Test Runner's mirror has. The platform reads the repository at a base
revision through it and opens draft pull requests in it, with its draft-pull-request credential
(Artifact 11.7). It never merges: the merge endpoint refuses the platform's token, as a branch
protection rule would.

    GET  /healthz
    GET  /v1/repos/{repo}                          name, default branch, head commit
    GET  /v1/repos/{repo}/commits?ref=&limit=      first-parent history from ref, newest first
    GET  /v1/repos/{repo}/commits/{ref}            one commit: parents, message, refs, and each
                                                   file it changed with its SHA-256 before and after
    GET  /v1/repos/{repo}/contents/{path}?ref=     a file's text at ref
    POST /v1/repos/{repo}/pulls                    open a DRAFT pull request (Idempotency-Key)
    GET  /v1/repos/{repo}/pulls/{number}           read it back
    PUT  /v1/repos/{repo}/pulls/{number}/merge     403: the platform may not merge
    GET  /_fake/pulls                              every pull request, for tests and the demo

Every /v1 call needs `Authorization: Bearer <any non-empty token>`. A pull request carries the
patch as `Patch.canonical()`; it is refused with 409 unless every file's `old_sha256` is the file
at `base_revision` and the hunks apply to give its `new_sha256`, which is what the review system
would check by building the branch. One that touches a deployment, test or CI path requires the
security lead's review (Artifact 11.1's third barrier): `required_reviewers`.

FAKE_REVIEW_REPO_SOURCE is build_repo.py (default: ../reference-app/build_repo.py beside this
file); FAKE_REVIEW_DEFAULT_BRANCH is `main`, or `tolerant` for M2's `not_reproduced` case (the
branch whose head doesn't fail). FAKE_REVIEW_PORT sets the port (default 8080). Standard library
and `git` only. Everything here is synthetic.
"""

from __future__ import annotations

import fnmatch
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

REPOSITORY = "orders-api"
SOURCE = Path(__file__).resolve().parent.parent / "reference-app" / "build_repo.py"
REVIEW_PROTECTED = ["deploy/*", "tests/*", "*/tests/*", ".github/*", "ci/*"]
REF = re.compile(r"[\w][\w./-]{0,199}")


class Repo:
    def __init__(self, path: Path, default_branch: str) -> None:
        self.path, self.default_branch = path, default_branch
        self.env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(path),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }

    def git(self, *args: str, binary: bool = False) -> Any:
        out = subprocess.run(
            ["git", "-C", str(self.path), "-c", "core.hooksPath=/dev/null", *args],
            check=True,
            capture_output=True,
            env=self.env,
        ).stdout
        return out if binary else out.decode().strip()

    def resolve(self, ref: str) -> str | None:
        if not REF.fullmatch(ref):
            return None
        try:
            return str(
                self.git(
                    "rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}"
                )
            )
        except subprocess.CalledProcessError:
            return None

    def blob(self, commit: str, path: str) -> bytes | None:
        try:
            return bytes(self.git("cat-file", "blob", f"{commit}:{path}", binary=True))
        except subprocess.CalledProcessError:
            return None

    def refs_at(self, commit: str) -> list[str]:
        out = self.git("for-each-ref", "--points-at", commit, "--format=%(refname:short)")
        return sorted(r for r in str(out).split("\n") if r)

    def commit(self, commit: str) -> dict[str, Any]:
        parents = str(self.git("rev-list", "--parents", "-n", "1", commit)).split()[1:]
        names = str(
            self.git("diff-tree", "-r", "--root", "--no-commit-id", "--name-only", "-z", commit)
        )
        files = []
        for name in (n for n in names.split("\0") if n):
            before = self.blob(parents[0], name) if parents else None
            after = self.blob(commit, name)
            files.append(
                {
                    "path": name,
                    "before_sha256": hashlib.sha256(before).hexdigest()
                    if before is not None
                    else None,
                    "after_sha256": hashlib.sha256(after).hexdigest()
                    if after is not None
                    else None,
                }
            )
        return {
            "sha": commit,
            "parents": parents,
            "message": str(self.git("log", "-1", "--format=%s", commit)),
            "author": str(self.git("log", "-1", "--format=%an", commit)),
            "refs": self.refs_at(commit),
            "files": files,
        }

    def history(self, ref: str, limit: int) -> list[dict[str, Any]]:
        commit = self.resolve(ref)
        if commit is None:
            return []
        shas = str(self.git("rev-list", "--first-parent", f"--max-count={limit}", commit)).split()
        return [self.commit(s) for s in shas]


def apply_file(text: str, hunks: list[dict[str, Any]]) -> str:
    """The harness's rule (ops/sandbox/harness/run): context and removed lines must match."""
    old = text.splitlines()
    out: list[str] = []
    at = 0
    for h in hunks:
        start = h["old_start"] - 1 if h["old_lines"] else h["old_start"]
        if start < at:
            raise ValueError("hunks overlap")
        out += old[at:start]
        at = start
        for line in h["lines"]:
            tag, body = line[:1], line[1:]
            if tag in (" ", "-"):
                if at >= len(old) or old[at] != body:
                    raise ValueError(f"hunk at line {h['old_start']} does not match")
                at += 1
                if tag == " ":
                    out.append(body)
            elif tag == "+":
                out.append(body)
            else:
                raise ValueError("malformed hunk line")
    out += old[at:]
    return "\n".join(out) + ("\n" if text.endswith("\n") or not text else "")


class Review:
    def __init__(self, repo: Repo) -> None:
        self.repo = repo
        self.pulls: dict[int, dict[str, Any]] = {}
        self.keys: dict[str, int] = {}
        self.lock = threading.Lock()

    def check_patch(self, base: str, patch: dict[str, Any]) -> list[dict[str, Any]]:
        """Every file applies at `base` and gives its new hash; returns the files' summary."""
        files = []
        for f in patch.get("files") or []:
            path = str(f["path"])
            body = self.repo.blob(base, path)
            if body is None or hashlib.sha256(body).hexdigest() != f["old_sha256"]:
                raise ValueError(f"{path} at {base[:12]} is not the patch's base")
            new = apply_file(body.decode(), f["hunks"]).encode()
            if hashlib.sha256(new).hexdigest() != f["new_sha256"]:
                raise ValueError(f"{path} after the patch is not the patch's result")
            files.append({"path": path, "new_sha256": f["new_sha256"]})
        return files

    def open(self, key: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if body.get("draft") is not True:
            return 422, {"message": "the platform opens draft pull requests only"}
        for field in ("title", "body", "base", "base_revision", "head", "patch"):
            if not body.get(field):
                return 422, {"message": f"{field} is required"}
        base = self.repo.resolve(str(body["base_revision"]))
        if base is None:
            return 422, {"message": "base_revision is not a commit here"}
        try:
            files = self.check_patch(base, body["patch"])
        except (KeyError, TypeError, ValueError) as e:
            return 409, {"message": f"the patch does not apply: {e}"}
        with self.lock:
            if key and key in self.keys:
                return 200, self.pulls[self.keys[key]]
            number = len(self.pulls) + 1
            touched = [f["path"] for f in files]
            pull = {
                "number": number,
                "url": f"http://fake-review.local/{REPOSITORY}/pulls/{number}",
                "state": "open",
                "draft": True,
                "merged": False,
                "title": body["title"],
                "body": body["body"],
                "base": body["base"],
                "base_revision": base,
                "head": body["head"],
                "files": files,
                "labels": list(body.get("labels") or []),
                "links": dict(body.get("links") or {}),
                "required_reviewers": (
                    ["security-lead"]
                    if any(fnmatch.fnmatchcase(p, g) for p in touched for g in REVIEW_PROTECTED)
                    else []
                ),
                "patch": body["patch"],
            }
            self.pulls[number] = pull
            if key:
                self.keys[key] = number
        return 201, pull


def handler_for(review: Review) -> type[BaseHTTPRequestHandler]:
    repo = review.repo

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # never log headers: tokens
            sys.stderr.write(f"fake-review {self.command} {self.path.split('?')[0]}\n")

        def reply(self, status: int, body: Any) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def authorised(self) -> bool:
            token = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            if not token:
                self.reply(401, {"message": "a bearer token is required"})
            return bool(token)

        def route(self) -> tuple[list[str], dict[str, list[str]]]:
            u = urlsplit(self.path)
            return [unquote(p) for p in u.path.split("/") if p], parse_qs(u.query)

        def do_GET(self) -> None:
            parts, query = self.route()
            if parts == ["healthz"]:
                return self.reply(200, {"status": "ok"})
            if parts == ["_fake", "pulls"]:
                return self.reply(200, list(review.pulls.values()))
            if not self.authorised():
                return None
            if parts[:2] != ["v1", "repos"] or len(parts) < 3 or parts[2] != REPOSITORY:
                return self.reply(404, {"message": "not found"})
            rest = parts[3:]
            ref = (query.get("ref") or [repo.default_branch])[0]
            if rest == []:
                head = repo.resolve(repo.default_branch)
                return self.reply(
                    200, {"name": REPOSITORY, "default_branch": repo.default_branch, "head": head}
                )
            if rest == ["commits"]:
                limit = max(1, min(20, int((query.get("limit") or ["5"])[0])))
                return self.reply(200, {"commits": repo.history(ref, limit)})
            if len(rest) == 2 and rest[0] == "commits":
                commit = repo.resolve(rest[1])
                if commit is None:
                    return self.reply(404, {"message": "no such commit"})
                return self.reply(200, repo.commit(commit))
            if len(rest) >= 2 and rest[0] == "contents":
                commit, path = repo.resolve(ref), "/".join(rest[1:])
                body = repo.blob(commit, path) if commit else None
                if body is None:
                    return self.reply(404, {"message": "no such file at that revision"})
                return self.reply(
                    200,
                    {
                        "path": path,
                        "commit": commit,
                        "sha256": hashlib.sha256(body).hexdigest(),
                        "content": body.decode(),
                    },
                )
            if len(rest) == 2 and rest[0] == "pulls" and rest[1].isdigit():
                pull = review.pulls.get(int(rest[1]))
                return (
                    self.reply(200, pull) if pull else self.reply(404, {"message": "no such pull"})
                )
            return self.reply(404, {"message": "not found"})

        def do_POST(self) -> None:
            parts, _ = self.route()
            if not self.authorised():
                return None
            if parts != ["v1", "repos", REPOSITORY, "pulls"]:
                return self.reply(404, {"message": "not found"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            except ValueError:
                return self.reply(400, {"message": "body must be JSON"})
            if not isinstance(body, dict):
                return self.reply(400, {"message": "body must be an object"})
            self.reply(*review.open(self.headers.get("Idempotency-Key", ""), body))

        def do_PUT(self) -> None:
            parts, _ = self.route()
            if not self.authorised():
                return None
            if len(parts) == 6 and parts[3] == "pulls" and parts[5] == "merge":
                return self.reply(
                    403, {"message": "the platform's credential may open draft pull requests only"}
                )
            return self.reply(404, {"message": "not found"})

    return Handler


def build(dest: Path, source: Path = SOURCE) -> dict[str, str]:
    spec = importlib.util.spec_from_file_location("build_repo", source)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tags: dict[str, str] = module.build(dest)
    return tags


def serve(review: Review, port: int = 0, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    """Start serving on a daemon thread; `server.server_address` has the port."""
    server = ThreadingHTTPServer((host, port), handler_for(review))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> None:
    port = int(os.environ.get("FAKE_REVIEW_PORT", "8080"))
    branch = os.environ.get("FAKE_REVIEW_DEFAULT_BRANCH") or "main"
    source = Path(os.environ.get("FAKE_REVIEW_REPO_SOURCE") or SOURCE)
    dest = Path(tempfile.mkdtemp(prefix="fake-review-")) / REPOSITORY
    tags = build(dest, source)
    repo = Repo(dest, branch)
    if repo.resolve(branch) is None:
        raise SystemExit(f"no branch {branch}")
    print(
        f"fake review system on :{port}, {REPOSITORY} default branch {branch} "
        f"at {repo.resolve(branch)}, tags {sorted(tags)} (local use only)",
        flush=True,
    )
    ThreadingHTTPServer(("0.0.0.0", port), handler_for(Review(repo))).serve_forever()


if __name__ == "__main__":
    main()
