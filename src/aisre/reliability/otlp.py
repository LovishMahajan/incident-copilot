"""Artifact 12.2 (T1.19, NFR-26): spans to the OpenTelemetry collector, OTLP over HTTP/JSON.

The lock holds the OpenTelemetry API and SDK but no OTLP exporter package, so this is the
exporter: each batch is one POST of an `ExportTraceServiceRequest` in OTLP's JSON encoding to
`<endpoint>/v1/traces` (the collector's HTTP receiver, port 4318). Standard library only.
The SDK's BatchSpanProcessor calls it off the request path; telemetry.build_tracer_provider
wraps it so a failure only counts in `aisre_telemetry_dropped_total`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.request import Request, urlopen

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import StatusCode

TIMEOUT_S = 5.0


def _value(v: Any) -> dict[str, Any]:
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, (list, tuple)):
        return {"arrayValue": {"values": [_value(x) for x in v]}}
    return {"stringValue": str(v)}


def _attributes(attrs: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    return [{"key": k, "value": _value(v)} for k, v in (attrs or {}).items()]


def _span(s: ReadableSpan) -> dict[str, Any]:
    ctx = s.get_span_context()
    assert ctx is not None
    out: dict[str, Any] = {
        "traceId": f"{ctx.trace_id:032x}",
        "spanId": f"{ctx.span_id:016x}",
        "name": s.name,
        "kind": s.kind.value + 1,  # OTLP counts from UNSPECIFIED = 0
        "startTimeUnixNano": str(s.start_time or 0),
        "endTimeUnixNano": str(s.end_time or 0),
        "attributes": _attributes(s.attributes),
        "status": {
            "code": {StatusCode.UNSET: 0, StatusCode.OK: 1, StatusCode.ERROR: 2}[
                s.status.status_code
            ]
        },
    }
    if s.parent is not None:
        out["parentSpanId"] = f"{s.parent.span_id:016x}"
    return out


def encode(spans: Sequence[ReadableSpan]) -> dict[str, Any]:
    """Spans grouped by resource, as OTLP/JSON."""
    groups: dict[int, tuple[Any, list[dict[str, Any]]]] = {}
    for s in spans:
        groups.setdefault(id(s.resource), (s.resource, []))[1].append(_span(s))
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": _attributes(resource.attributes)},
                "scopeSpans": [{"scope": {"name": "aisre"}, "spans": items}],
            }
            for resource, items in groups.values()
        ]
    }


class OtlpJsonExporter(SpanExporter):
    def __init__(self, endpoint: str, timeout_s: float = TIMEOUT_S) -> None:
        self.url = endpoint.rstrip("/") + "/v1/traces"
        self.timeout_s = timeout_s

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        body = json.dumps(encode(spans)).encode()
        req = Request(
            self.url, data=body, method="POST", headers={"Content-Type": "application/json"}
        )
        try:
            with urlopen(req, timeout=self.timeout_s) as resp:  # noqa: S310 (configured URL)
                ok = 200 <= resp.status < 300
        except OSError:
            return SpanExportResult.FAILURE
        return SpanExportResult.SUCCESS if ok else SpanExportResult.FAILURE

    def shutdown(self) -> None:
        return None
