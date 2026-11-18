"""LOCAL USE ONLY (Artifact 13.5 T1.1): a fake model provider in the Anthropic Messages API shape.

Lets the Model Gateway's real transport run with no key and no cost: point its base URL here
and send any non-empty `x-api-key`. Answers are scripted from JSON files, so every run gives
the same result.

    POST /v1/messages          Messages API request -> message, or an error in the API's shape
    GET  /_fake/requests       the last 100 request bodies, newest last (headers left out)
    GET  /healthz

Checks what the real API refuses: a missing `x-api-key` is 401 `authentication_error`; a
missing `anthropic-version`, or a body without `model`, `max_tokens` and `messages`, is 400
`invalid_request_error`.

Scripts are the `*.json` files in FAKE_PROVIDER_SCRIPTS (default: `provider-scripts/` beside
this file), tried in file-name order, `default.json` last. A script answers when its `match`
string appears in the request's `system`, `messages` or `tools`, serialised as JSON:

    {"match": "FAKE_RATE_LIMIT", "status": 429,
     "error": {"type": "rate_limit_error", "message": "..."}, "headers": {"retry-after": "1"}}
    {"match": "", "response": {"content": [{"type": "text", "text": "..."}],
                               "stop_reason": "end_turn", "usage": {...}}}

A message's `id`, `type`, `role` and `model` (echoed from the request) are filled in, and
`usage` defaults to a rough count (4 characters a token). FAKE_PROVIDER_PORT sets the port
(default 8080). Needs only the standard library.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).parent / "provider-scripts"
REQUIRED = ("model", "max_tokens", "messages")


def load_scripts(directory: Path) -> list[dict[str, Any]]:
    files = sorted(directory.glob("*.json"), key=lambda p: (p.name == "default.json", p.name))
    return [json.loads(p.read_text()) | {"name": p.stem} for p in files]


def error_body(kind: str, message: str, request_id: str) -> dict[str, Any]:
    return {"type": "error", "error": {"type": kind, "message": message}, "request_id": request_id}


class Provider:
    def __init__(self, scripts: list[dict[str, Any]]) -> None:
        self.scripts = scripts
        self.requests: deque[dict[str, Any]] = deque(maxlen=100)
        self.count = 0
        self.lock = threading.Lock()

    def answer(
        self, headers: dict[str, str], raw: bytes
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        with self.lock:
            self.count += 1
            n = self.count
        request_id = f"req_fake_{n:06d}"
        if not headers.get("x-api-key"):
            return (
                401,
                error_body("authentication_error", "x-api-key header is required", request_id),
                {},
            )
        if not headers.get("anthropic-version"):
            return (
                400,
                error_body(
                    "invalid_request_error", "anthropic-version header is required", request_id
                ),
                {},
            )
        try:
            req = json.loads(raw)
            missing = [f for f in REQUIRED if f not in req]
        except (ValueError, TypeError):
            req, missing = None, ["body"]
        if not isinstance(req, dict) or missing:
            return (
                400,
                error_body("invalid_request_error", f"missing: {', '.join(missing)}", request_id),
                {},
            )
        self.requests.append(req)
        text = json.dumps([req.get("system"), req["messages"], req.get("tools")])
        script = next((s for s in self.scripts if s.get("match", "") in text), None)
        if script is None:
            return (
                500,
                error_body("api_error", "no script matched and no default.json", request_id),
                {},
            )
        extra = {str(k): str(v) for k, v in script.get("headers", {}).items()}
        if "error" in script:
            err = script["error"]
            return (
                int(script.get("status", 500)),
                error_body(err["type"], err["message"], request_id),
                extra,
            )
        content = script["response"].get("content", [])
        usage = {"input_tokens": len(text) // 4, "output_tokens": len(json.dumps(content)) // 4}
        message = {
            "id": f"msg_fake_{n:06d}",
            "type": "message",
            "role": "assistant",
            "model": req["model"],
            "content": content,
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": usage,
        } | script["response"]
        return int(script.get("status", 200)), message, extra | {"request-id": request_id}


def handler_for(provider: Provider) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # never log headers: keys
            sys.stderr.write(f"fake-provider {self.command} {self.path.split('?')[0]}\n")

        def reply(self, status: int, body: Any, headers: dict[str, str] | None = None) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            path = self.path.split("?")[0]
            if path == "/healthz":
                self.reply(200, {"status": "ok"})
            elif path == "/_fake/requests":
                self.reply(200, list(provider.requests))
            else:
                self.reply(404, error_body("not_found_error", "not found", ""))

        def do_POST(self) -> None:
            if self.path.split("?")[0] != "/v1/messages":
                return self.reply(404, error_body("not_found_error", "not found", ""))
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            headers = {k.lower(): v for k, v in self.headers.items()}
            self.reply(*provider.answer(headers, raw))

    return Handler


def serve(provider: Provider, port: int = 0, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    """Start serving on a daemon thread; `server.server_address` has the port."""
    server = ThreadingHTTPServer((host, port), handler_for(provider))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> None:
    port = int(os.environ.get("FAKE_PROVIDER_PORT", "8080"))
    scripts = load_scripts(Path(os.environ.get("FAKE_PROVIDER_SCRIPTS") or SCRIPTS))
    print(f"fake model provider on :{port}, {len(scripts)} scripts (local use only)", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), handler_for(Provider(scripts))).serve_forever()


if __name__ == "__main__":
    main()
