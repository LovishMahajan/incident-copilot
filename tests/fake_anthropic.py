"""An in-process fake of the Claude Messages API, for the transport and contract tests (T1.8, T1.9).

A real HTTP server on 127.0.0.1 (a random port) in a daemon thread, so requests go through the
SDK's client exactly as they would to the provider. It enforces the request rules Artifact 10.1
lists for the pinned models on API version 2026-10-21, with the provider's error shapes, so a
broken rule fails here the way it fails live:

- `anthropic-version: 2026-10-21`, and the key, as `Authorization: Bearer` or `x-api-key`
- `temperature`, `top_p` and `top_k` are rejected with 400, whatever the value
- `thinking` can't be disabled or given a budget; forced `tool_choice` (`any`, `tool`) is 400
- an unknown model is 404; `max_tokens` above 128K is 400

Replies come from `script`, in order, then `default_text`: a dict is a 200 body, a
`(status, body)` tuple an error, `("sleep", seconds, item)` a slow reply.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

API_VERSION = "2026-10-21"
MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"}
SAMPLING = ("temperature", "top_p", "top_k")
MAX_OUTPUT = 128_000


def error(kind: str, message: str) -> dict[str, Any]:
    return {"type": "error", "error": {"type": kind, "message": message}, "request_id": "req_err"}


def rule_broken(headers: dict[str, str], body: dict[str, Any], key: str) -> tuple[int, dict] | None:
    auth = headers.get("authorization", "")
    if auth != f"Bearer {key}" and headers.get("x-api-key") != key:
        return 401, error("authentication_error", "invalid x-api-key")
    if headers.get("anthropic-version") != API_VERSION:
        return 400, error("invalid_request_error", "anthropic-version: unsupported")
    for name in ("model", "max_tokens", "messages"):
        if name not in body:
            return 400, error("invalid_request_error", f"{name}: Field required")
    for name in SAMPLING:
        if name in body:
            return 400, error("invalid_request_error", f"{name}: not supported on this API version")
    if body["model"] not in MODELS:
        return 404, error("not_found_error", f"model: {body['model']}")
    if not 1 <= body["max_tokens"] <= MAX_OUTPUT:
        return 400, error("invalid_request_error", "max_tokens: out of range")
    thinking = body.get("thinking") or {}
    if thinking.get("type") == "disabled" or "budget_tokens" in thinking:
        return 400, error("invalid_request_error", "thinking: adaptive only on this model")
    if (body.get("tool_choice") or {}).get("type") in ("any", "tool"):
        return 400, error("invalid_request_error", "tool_choice: type not supported for this model")
    return None


class FakeAnthropic:
    def __init__(self, key: str = "test-key-not-real", default_text: str = "{}") -> None:
        self.key, self.default_text = key, default_text
        self.script: deque[Any] = deque()
        self.requests: list[tuple[dict[str, str], dict[str, Any]]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # quiet
                pass

            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("content-length", 0)))
                headers = {k.lower(): v for k, v in self.headers.items()}
                body = json.loads(raw or b"{}")
                fake.requests.append((headers, body))
                if self.path.split("?")[0] != "/v1/messages":
                    return self.reply(404, error("not_found_error", self.path))
                broken = rule_broken(headers, body, fake.key)
                if broken:
                    return self.reply(*broken)
                item = fake.script.popleft() if fake.script else None
                if isinstance(item, tuple) and item[0] == "sleep":
                    time.sleep(item[1])
                    item = item[2]
                if isinstance(item, tuple):
                    return self.reply(*item)
                self.reply(200, item or fake.message(body["model"], fake.default_text))

            def reply(self, status: int, payload: dict[str, Any]) -> None:
                data = json.dumps(payload).encode()
                try:
                    self.send_response(status)
                    self.send_header("content-type", "application/json")
                    self.send_header("request-id", f"req_fake_{len(fake.requests)}")
                    self.send_header("content-length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):  # the client timed out first
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @staticmethod
    def message(model: str, text: str, stop: str = "end_turn") -> dict[str, Any]:
        return {
            "id": "msg_fake",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": text}],
            "stop_reason": stop,
            "stop_details": None,
            "usage": {"input_tokens": 120, "output_tokens": 40},
        }

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> FakeAnthropic:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
