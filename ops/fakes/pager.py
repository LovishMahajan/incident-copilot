"""LOCAL USE ONLY (Artifact 13.5 T4.6, ADR-74): a fake pager that records what Alertmanager sends.

Stands in for the paging service and the on-call rota, neither of which exists here (ADR-69,
ADR-74). Alertmanager's webhook receivers post to it, one path per role, and it keeps every
notification in memory, so a test can show that an alert reached "a person": the role it was
routed to. Nothing is delivered anywhere. Pages travel Prometheus -> Alertmanager -> here, never
through the Notifier or PostgreSQL (Artifact 12.3): this process has neither.

    POST /v1/notify/{role}   an Alertmanager webhook body (version 4); recorded, 200
    GET  /v1/pages           {"pages": [...]}, oldest first; ?role=&alertname=&status= filter
    POST /_fake/reset        forget every page
    GET  /healthz

Each page is one alert from one notification: role, alertname, status (firing or resolved),
severity, labels, annotations, startsAt, and when it was received. Roles are those in
ops/compose/alertmanager.yml: platform_owner, incident_lead, security_lead.

Settings, from the environment: FAKE_PAGER_PORT (default 8080). Refuses to start when
AISRE_ENV is production. Standard library only; tests import `Pager` and `serve`.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

ROLES = ("platform_owner", "incident_lead", "security_lead")
MAX_BODY = 1_000_000
MAX_PAGES = 10_000


class Pager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pages: list[dict[str, Any]] = []

    def record(self, role: str, body: dict[str, Any]) -> int:
        """Records each alert in one webhook body for `role`. Returns how many."""
        now = datetime.now(UTC).isoformat()
        alerts = body.get("alerts")
        if not isinstance(alerts, list):
            raise ValueError("no alerts list")
        pages = []
        for a in alerts:
            labels = a.get("labels") or {}
            pages.append(
                {
                    "role": role,
                    "alertname": labels.get("alertname", ""),
                    "status": a.get("status", body.get("status", "")),
                    "severity": labels.get("severity", ""),
                    "labels": labels,
                    "annotations": a.get("annotations") or {},
                    "starts_at": a.get("startsAt", ""),
                    "received_at": now,
                }
            )
        with self._lock:
            self._pages.extend(pages)
            del self._pages[:-MAX_PAGES]
        return len(pages)

    def pages(self, **match: str) -> list[dict[str, Any]]:
        with self._lock:
            return [p for p in self._pages if all(p.get(k) == v for k, v in match.items() if v)]

    def reset(self) -> None:
        with self._lock:
            self._pages.clear()


def handler_for(pager: Pager) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # quiet
            pass

        def _send(self, status: int, doc: dict[str, Any]) -> None:
            body = json.dumps(doc).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            url = urlsplit(self.path)
            if url.path == "/healthz":
                return self._send(200, {"status": "ok"})
            if url.path == "/v1/pages":
                q = {k: v[0] for k, v in parse_qs(url.query).items()}
                match = {k: q.get(k, "") for k in ("role", "alertname", "status")}
                return self._send(200, {"pages": pager.pages(**match)})
            self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            parts = urlsplit(self.path).path.strip("/").split("/")
            if parts == ["_fake", "reset"]:
                pager.reset()
                return self._send(200, {"status": "reset"})
            if len(parts) != 3 or parts[:2] != ["v1", "notify"] or parts[2] not in ROLES:
                return self._send(404, {"error": "not found"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._send(413, {"error": "too large"})
            try:
                n = pager.record(parts[2], json.loads(self.rfile.read(length) or b"{}"))
            except (ValueError, AttributeError) as e:
                return self._send(400, {"error": str(e)})
            self._send(200, {"recorded": n})

    return Handler


def serve(pager: Pager, port: int = 0, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    """Start serving on a daemon thread; `server.server_address` has the port."""
    server = ThreadingHTTPServer((host, port), handler_for(pager))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> None:
    if os.environ.get("AISRE_ENV") == "production":
        sys.exit("fake pager: refusing to run with AISRE_ENV=production")
    port = int(os.environ.get("FAKE_PAGER_PORT", "8080"))
    print(f"fake pager on :{port}, recording pages for {', '.join(ROLES)} (local use only)")
    sys.stdout.flush()
    ThreadingHTTPServer(("0.0.0.0", port), handler_for(Pager())).serve_forever()


if __name__ == "__main__":
    main()
