"""Artifact 12.1, 12.2 (T1.19, NFR-26): every API request timed, counted and traced.

One middleware: a server span per request, named `<method> <route template>`, with the route,
method, status, request ID and, for incident routes, `aisre.incident_id`; and
`aisre_http_requests_total{route, method, status_class}` and `aisre_http_request_seconds`.
Labels use the route template, never the path, so they stay bounded (Artifact 12.1). A stream
is timed to its first byte. The process's `/metrics` port and tracer are set up by `install`'s
caller, at start-up (aisre.reliability.telemetry).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from opentelemetry import trace
from opentelemetry.trace import SpanKind, StatusCode

from aisre.reliability import telemetry

tracer = trace.get_tracer("aisre.api")


def install(app: FastAPI) -> None:
    @app.middleware("http")
    async def observe(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        started = time.monotonic()
        with tracer.start_as_current_span(request.method, kind=SpanKind.SERVER) as span:
            status = 500
            try:
                response = await call_next(request)
                status = response.status_code
                return response
            finally:
                route = getattr(request.scope.get("route"), "path", "unmatched")
                span.update_name(f"{request.method} {route}")
                span.set_attribute("http.route", route)
                span.set_attribute("http.request.method", request.method)
                span.set_attribute("http.response.status_code", status)
                span.set_attribute("aisre.request_id", getattr(request.state, "request_id", ""))
                if incident := request.scope.get("path_params", {}).get("incident_id"):
                    span.set_attribute("aisre.incident_id", str(incident))
                if status >= 500:
                    span.set_status(StatusCode.ERROR)
                telemetry.http_requests.labels(route, request.method, f"{status // 100}xx").inc()
                telemetry.http_latency.labels(route, request.method).observe(
                    time.monotonic() - started
                )
