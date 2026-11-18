"""T1.19 (NFR-26, Artifact 12.1, 12.2): API requests timed and traced, activities timed and
traced with the incident ID, spans exported as OTLP/JSON, and the worker's Notifier registered."""

from __future__ import annotations

import json
import os
import threading
import urllib.request
from dataclasses import dataclass
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import SecretStr
from sqlalchemy import create_engine
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from aisre.api import observe
from aisre.api.app import create_app
from aisre.api.auth import DEV_CALLER_HEADER
from aisre.config import Settings
from aisre.reliability import telemetry
from aisre.reliability.otlp import OtlpJsonExporter
from aisre.reliability.temporal_telemetry import StepTiming
from aisre.workflow import worker

UNREACHABLE = "postgresql+psycopg://aisre_app@127.0.0.1:1/aisre?connect_timeout=1"
TEST_SERVER = os.environ.get("TEMPORAL_TEST_SERVER")


def sample(name: str, **labels: str) -> float:
    return telemetry.REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.fixture
def spans(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(observe, "tracer", provider.get_tracer("test"))
    return exporter


def test_api_requests_are_counted_timed_and_traced_by_route(spans):
    app = create_app(
        Settings(env="test", database_url=SecretStr(UNREACHABLE)), create_engine(UNREACHABLE)
    )
    route = "/v1/incidents/{incident_id}"
    before = sample("aisre_http_requests_total", route=route, method="GET", status_class="4xx")
    incident = uuid4()
    with TestClient(app) as c:
        r = c.get(
            f"/v1/incidents/{incident}",
            headers={DEV_CALLER_HEADER: "m"} | {"X-Aisre-Dev-Groups": "aisre:monitoring"},
        )
    assert r.status_code == 403
    after = sample("aisre_http_requests_total", route=route, method="GET", status_class="4xx")
    assert after == before + 1
    assert sample("aisre_http_request_seconds_count", route=route, method="GET") >= 1
    (span,) = spans.get_finished_spans()
    assert span.name == f"GET {route}"
    assert span.attributes["aisre.incident_id"] == str(incident)
    assert span.attributes["http.response.status_code"] == 403


def test_the_otlp_exporter_posts_json_to_the_collector():
    received: list[dict] = []

    class Collector(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            received.append(
                {
                    "path": self.path,
                    "body": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                }
            )
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Collector)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        provider = telemetry.build_tracer_provider(
            "aisre-test", OtlpJsonExporter(f"http://127.0.0.1:{server.server_address[1]}")
        )
        with provider.get_tracer("t").start_as_current_span("outer") as outer:
            outer.set_attribute("aisre.incident_id", "inc-1")
            outer.set_attribute("prompt", "never exported")
            with provider.get_tracer("t").start_as_current_span("inner"):
                pass
        provider.force_flush()
    finally:
        server.shutdown()
    (post,) = received
    assert post["path"] == "/v1/traces"
    (rs,) = post["body"]["resourceSpans"]
    service = {a["key"]: a["value"] for a in rs["resource"]["attributes"]}["service.name"]
    assert service == {"stringValue": "aisre-test"}
    inner, outer = rs["scopeSpans"][0]["spans"]
    assert inner["parentSpanId"] == outer["spanId"] and inner["traceId"] == outer["traceId"]
    assert outer["attributes"] == [{"key": "aisre.incident_id", "value": {"stringValue": "inc-1"}}]


def test_metrics_are_served_on_their_own_port_only_when_asked():
    assert telemetry.serve_metrics({}) is None
    server = telemetry.serve_metrics({telemetry.METRICS_PORT_VAR: "0"})
    try:
        port = server.server_address[1]
        body = urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5).read()
        assert b"aisre_time_to_first_notification_seconds" in body
    finally:
        server.shutdown()


@dataclass
class StepIn:
    incident_id: str


@activity.defn(name="probe_step")
async def probe_step(inp: StepIn) -> str:
    return inp.incident_id


@workflow.defn(name="ProbeWorkflow", sandboxed=False)
class ProbeWorkflow:
    @workflow.run
    async def run(self, incident_id: str) -> str:
        return await workflow.execute_activity(
            probe_step, StepIn(incident_id), start_to_close_timeout=timedelta(seconds=10)
        )


@pytest.mark.asyncio
async def test_every_activity_is_timed_with_its_outcome():
    before = sample("aisre_step_seconds_count", step="probe_step", outcome="ok")
    async with await WorkflowEnvironment.start_time_skipping(
        test_server_existing_path=TEST_SERVER
    ) as env:
        async with Worker(
            env.client,
            task_queue="probe",
            workflows=[ProbeWorkflow],
            activities=[probe_step],
            interceptors=[StepTiming()],
        ):
            out = await env.client.execute_workflow(
                ProbeWorkflow.run, "inc-7", id="probe-1", task_queue="probe"
            )
    assert out == "inc-7"
    assert sample("aisre_step_seconds_count", step="probe_step", outcome="ok") == before + 1


def test_the_worker_registers_the_real_notify_ahead_of_the_script():
    settings = Settings(env="test", scripted_skills=True)
    chosen = worker.investigation_activities(settings, create_engine(UNREACHABLE))
    names = [worker.activity_name(a) for a in chosen]
    assert len(names) == len(set(names))
    (notify,) = [a for a in chosen if worker.activity_name(a) == "notify"]
    assert notify.__qualname__ == "Notifier.notify"
