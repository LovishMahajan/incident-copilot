"""Recorded vendor responses (M5, ADR-75): `urllib.request.urlopen`, answered from a file.

Each file in tests/fixtures/recorded/ holds `_source` (the vendor's API reference and version the
responses are shaped after) and `exchanges`: `{method, path, when?, status, body, once?, headers?}`. A
request is answered by the first exchange with its method and path whose `when` strings all
appear in its URL or body; `once` exchanges are used up. Nothing reaches a network: an
unanswered request fails the test. Every request is kept, headers included, for the assertions.
"""

from __future__ import annotations

import email.message
import io
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

RECORDED = Path(__file__).parent / "fixtures" / "recorded"


def load(name: str) -> list[dict[str, Any]]:
    return json.loads((RECORDED / f"{name}.json").read_text())["exchanges"]


@dataclass
class Sent:
    method: str
    url: str
    headers: dict[str, str]
    body: Any

    @property
    def path(self) -> str:
        return urlsplit(self.url).path

    @property
    def query(self) -> str:
        return unquote(urlsplit(self.url).query.replace("+", " "))


def _headers(given: dict[str, str] | None) -> email.message.Message:
    out = email.message.Message()
    for k, v in (given or {}).items():
        out[k] = v
    return out


class _Response:
    def __init__(self, status: int, raw: bytes, headers: email.message.Message) -> None:
        self.status, self._raw, self.headers = status, raw, headers

    def read(self, n: int = -1) -> bytes:
        return self._raw if n < 0 else self._raw[:n]

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


@dataclass
class Recorded:
    exchanges: list[dict[str, Any]]
    sent: list[Sent] = field(default_factory=list)

    def __call__(self, req: Any, timeout: float | None = None) -> _Response:
        if isinstance(req, str):
            req = urllib.request.Request(req)
        raw = req.data or b""
        body = json.loads(raw) if raw else None
        headers = {k.lower(): v for k, v in req.header_items()}
        sent = Sent(req.get_method(), req.full_url, headers, body)
        self.sent.append(sent)
        haystack = sent.query + " " + unquote(sent.url) + " " + raw.decode(errors="replace")
        for ex in self.exchanges:
            if ex.get("used") or ex["method"] != sent.method or ex["path"] != sent.path:
                continue
            if not all(w in haystack for w in ex.get("when", [])):
                continue
            if ex.get("once"):
                ex["used"] = True
            if ex.get("timeout"):
                raise TimeoutError()
            out = ex.get("raw", "").encode() if "raw" in ex else json.dumps(ex["body"]).encode()
            if ex["status"] >= 400:
                raise urllib.error.HTTPError(
                    req.full_url,
                    ex["status"],
                    "recorded",
                    _headers(ex.get("headers")),
                    io.BytesIO(out),
                )
            return _Response(ex["status"], out, _headers(ex.get("headers")))
        raise AssertionError(f"no recorded answer for {sent.method} {sent.url}")

    def to(self, method: str, path_part: str) -> list[Sent]:
        return [s for s in self.sent if s.method == method and path_part in s.path]
