"""T1.20 (FR-37, ST-32, WR-30): the write-once store over S3, the API's audit lines to the copy,
and the weekly chain check as a timer job. PostgreSQL halves: tests/integration/test_audit_copy.py."""

from __future__ import annotations

import logging
import os
import re
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from pydantic import ValidationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from aisre.api import audit
from aisre.config import Settings
from aisre.record.audit_copy import (
    AlreadyWritten,
    Fault,
    MemoryStore,
    S3Config,
    S3Store,
    read_log_lines,
    store_from_settings,
)
from aisre.reliability import telemetry
from aisre.reliability.audit_jobs import (
    CHECK_EVERY,
    CHECK_JOB,
    COPY_EVERY,
    COPY_JOB,
    AuditJobActivities,
    jobs,
)
from aisre.workflow import activities as act
from aisre.workflow.budget_jobs import BudgetJobWorkflow

TEST_SERVER = os.environ.get("TEMPORAL_TEST_SERVER")
AUTH = re.compile(
    r"^AWS4-HMAC-SHA256 Credential=fake-key/\d{8}/us-east-1/s3/aws4_request, "
    r"SignedHeaders=[a-z0-9;-]+, Signature=[0-9a-f]{64}$"
)


class FakeS3(BaseHTTPRequestHandler):
    """Just enough S3: conditional PUT, GET, ListObjectsV2. Records each request's headers."""

    objects: dict[str, bytes] = {}
    seen: list[dict[str, str]] = []

    def _key(self) -> str:
        return unquote(urlsplit(self.path).path).split("/", 2)[2]

    def do_PUT(self):  # noqa: N802
        FakeS3.seen.append({k.lower(): v for k, v in self.headers.items()})
        assert AUTH.match(self.headers["Authorization"])
        body = self.rfile.read(int(self.headers["Content-Length"]))
        key = self._key()
        if self.headers.get("If-None-Match") == "*" and key in self.objects:
            self.send_response(412)
        else:
            self.objects[key] = body
            self.send_response(200)
        self.end_headers()

    def do_GET(self):  # noqa: N802
        url = urlsplit(self.path)
        q = parse_qs(url.query)
        if "list-type" in q:
            prefix = q.get("prefix", [""])[0]
            keys = "".join(
                f"<Contents><Key>{k}</Key></Contents>"
                for k in sorted(self.objects)
                if k.startswith(prefix)
            )
            body = (
                '<?xml version="1.0"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                f"<IsTruncated>false</IsTruncated>{keys}</ListBucketResult>"
            ).encode()
        else:
            body = self.objects.get(self._key(), b"")
            if self._key() not in self.objects:
                self.send_response(404)
                self.end_headers()
                return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def s3():
    FakeS3.objects, FakeS3.seen = {}, []
    server = HTTPServer(("127.0.0.1", 0), FakeS3)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_s3_store_writes_once_reads_and_lists(s3):
    store = S3Store(S3Config(s3, "aisre-audit", "fake-key", "fake-secret"))
    store.put_once("timeline/i1/000000000001.json", b'{"seq":1}')
    store.put_once("timeline/i1/000000000001.json", b'{"seq":1}')  # a retried copy
    with pytest.raises(AlreadyWritten):
        store.put_once("timeline/i1/000000000001.json", b'{"seq":1,"edited":true}')
    store.put_once("timeline/i1/000000000002.json", b'{"seq":2}')
    assert store.get("timeline/i1/000000000001.json") == b'{"seq":1}'
    assert store.get("timeline/i1/000000000009.json") is None
    assert store.keys("timeline/i1/") == [
        "timeline/i1/000000000001.json",
        "timeline/i1/000000000002.json",
    ]
    assert FakeS3.objects["timeline/i1/000000000001.json"] == b'{"seq":1}'
    assert all(h.get("if-none-match") == "*" for h in FakeS3.seen)


def test_s3_store_asks_for_compliance_lock_when_configured(s3):
    store = S3Store(S3Config(s3, "aisre-audit", "fake-key", "fake-secret", lock_days=365))
    store.put_once("log/x.jsonl", b"{}\n")
    (h,) = FakeS3.seen
    assert h["x-amz-object-lock-mode"] == "COMPLIANCE" and "content-md5" in h


def test_the_store_comes_from_the_environment_or_not_at_all():
    assert store_from_settings(Settings.from_env({})) is None
    with pytest.raises(ValidationError, match="AISRE_AUDIT_STORE_BUCKET"):
        Settings.from_env({"AISRE_AUDIT_STORE_URL": "http://minio:9000"})
    store = store_from_settings(
        Settings.from_env(
            {
                "AISRE_AUDIT_STORE_URL": "http://minio:9000",
                "AISRE_AUDIT_STORE_BUCKET": "aisre-audit",
                "AISRE_AUDIT_STORE_ACCESS_KEY": "fake-key",
                "AISRE_AUDIT_STORE_SECRET_KEY": "fake-secret",
                "AISRE_AUDIT_STORE_LOCK_DAYS": "1",
            }
        )
    )
    assert store is not None and store.c.bucket == "aisre-audit" and store.c.lock_days == 1


def test_audit_lines_reach_the_copy_by_incident_and_sign_in():
    store = MemoryStore()
    audit.install_handler()
    handler = audit.install_copy(store)
    try:
        audit.emit("aisre.content.read", None, incident_id="i9", caller="user:vera")
        audit.emit("aisre.auth.failed", None, reason="missing_token")
        handler.flush()
    finally:
        audit.install_copy(None)
    (read,) = read_log_lines(store, "log/incident/i9/")
    assert read["event"] == "aisre.content.read" and read["caller"] == "user:vera"
    (failed,) = read_log_lines(store, "log/auth/")
    assert failed["reason"] == "missing_token"


def test_a_failed_copy_keeps_its_lines_for_the_next_flush():
    class Down(MemoryStore):
        up = False

        def put_once(self, key, data):
            if not self.up:
                raise ConnectionError("store down")
            super().put_once(key, data)

    store = Down()
    handler = audit.CopyHandler(store, flush_s=3600)
    handler.handle(
        logging.makeLogRecord({"aisre": {"event": "aisre.authz.denied", "incident_id": "i1"}})
    )
    handler.flush()
    assert store.keys("log/") == [] and len(handler.pending) == 1
    store.up = True
    handler.flush()
    assert len(read_log_lines(store, "log/incident/i1/")) == 1
    handler.close()


class StubChecks(AuditJobActivities):
    """The real activities over a canned record: one broken incident, and counts."""

    def __init__(self) -> None:
        super().__init__(engine=None, store=None)  # type: ignore[arg-type]
        self.checks = 0

    def faults(self):
        self.checks += 1
        return [Fault("i1", 3, "hash does not match the event"), Fault("i1", 4, "gap")]


def test_the_jobs_copy_every_minute_and_check_every_week():
    assert (COPY_EVERY, CHECK_EVERY) == (timedelta(minutes=1), timedelta(days=7))
    assert [(i, j.activity, j.every_s) for i, j in jobs()] == [
        (COPY_JOB, "copy_audit_trail", 60.0),
        (CHECK_JOB, "check_audit_chain", 7 * 86400.0),
    ]


@pytest.mark.asyncio
async def test_the_chain_check_runs_weekly_and_sets_the_gauge():
    """The check job alone: with the minute-by-minute copy beside it, a simulated week takes
    10,080 runs. (Terminating the copy instead stops the test server's time-skipping.)"""
    stub = StubChecks()
    telemetry.audit_chain_faults.set(0)
    job_id, inp = jobs()[1]
    async with await WorkflowEnvironment.start_time_skipping(
        test_server_existing_path=TEST_SERVER, data_converter=act.DATA_CONVERTER
    ) as env:
        async with Worker(
            env.client, task_queue="audit", workflows=[BudgetJobWorkflow], activities=stub.all()
        ):
            await env.client.start_workflow(
                BudgetJobWorkflow.run, inp, id=job_id, task_queue="audit"
            )
            await env.sleep(timedelta(hours=1))
            assert stub.checks == 1
            assert telemetry.REGISTRY.get_sample_value("aisre_audit_chain_faults") == 1
            await env.sleep(timedelta(days=6))
            assert stub.checks == 1
            await env.sleep(timedelta(days=1))
            assert stub.checks == 2
