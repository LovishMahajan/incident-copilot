"""Artifact 9.2, 9.4, 9.5, 13.5 T1.10: the Evidence Collector and its source adapters, offline.

The fake monitoring source runs in-process on a free port, serving the reference incident or a
copy with files withheld. FR-07, FR-08, FR-44, NFR-18, NFR-29.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from monitoring_scenarios import (
    ALERT_ID,
    CUT_OFF,
    STARTED,
    copy_reference,
    serving,
    sources,
)
from pydantic import BaseModel, SecretStr

from aisre.agent.agent_contracts import EvidenceKind
from aisre.agent.tool_registry import REGISTRY, MemoryToolCalls, ToolGate
from aisre.agent.validation import missing_required_sources
from aisre.evidence.collector import Call, Outcome, Scope, baseline_plan, collect
from aisre.evidence.snapshots import S3Snapshots
from aisre.evidence.sources import SourceResult, SourceSet, SourceUnavailable


def scope() -> Scope:
    return Scope(str(uuid.uuid4()), "orders-api", "production", STARTED, ALERT_ID)


def run(
    src: SourceSet, calls: list[Call] | None = None, s: Scope | None = None, **kw: Any
) -> Outcome:
    s = s or scope()
    gate = kw.pop("gate", None) or ToolGate(s.incident_id, s.service, s.started_at, CUT_OFF)
    return asyncio.run(
        collect(
            s,
            baseline_plan(s) if calls is None else calls,
            gate,
            src,
            collection_key="k",
            now=CUT_OFF,
            **kw,
        )
    )


class Canned:
    """A source answering every call with the same records, counting calls."""

    def __init__(self, records: list[dict[str, Any]], delay: float = 0.0) -> None:
        self.records, self.delay, self.calls = records, delay, 0

    def fetch(self, tool: str, params: BaseModel, environment: str) -> SourceResult:
        self.calls += 1
        time.sleep(self.delay)
        return SourceResult("canned", "ref:canned", self.records)


def by_tool(out: Outcome) -> dict[str, list]:
    found: dict[str, list] = {}
    for c in out.collected:
        found.setdefault(c.evidence.tool, []).append(c)
    return found


# ---------------------------------------------------------------- the baseline plan
def test_baseline_collects_every_source_of_the_reference_incident():
    with serving() as url:
        out = run(sources(url))
    kinds = sorted(c.evidence.kind.value for c in out.collected)
    assert kinds == sorted(
        ["alert", "change", "metric", "metric", "log", "error_group", "trace", "dependency"]
    )
    assert out.gap_count == 0 and not out.refused and len(out.evidence_ids) == 8
    assert missing_required_sources([c.evidence for c in out.collected]) == []


def test_fr07_every_record_has_its_source_collection_time_and_link():
    with serving() as url:
        out = run(sources(url))
    for c in out.collected:
        e = c.evidence
        assert e.source_system and e.source_link and e.collected_at == CUT_OFF
        assert e.query and json.loads(e.query)["tool"] == e.tool


def test_fr44_every_query_stays_inside_the_window_and_nothing_after_the_cut_off_is_kept():
    with serving() as url:
        out = run(sources(url))
    earliest = STARTED - timedelta(minutes=60)
    for c in out.collected:
        e = c.evidence
        assert e.window_start is not None and earliest <= e.window_start <= CUT_OFF
        assert e.window_end is not None and e.window_end <= CUT_OFF
    stamps = [
        r.get("at") or r.get("ts")
        for c in out.collected
        if c.evidence.kind in (EvidenceKind.metric, EvidenceKind.log)
        for r in c.records
    ]
    assert stamps and all(earliest.isoformat()[:16] <= s[:16] <= "2026-10-20T14:20" for s in stamps)


def test_extracts_carry_what_the_model_needs_and_raw_records_stay_in_the_snapshot():
    with serving() as url:
        found = by_tool(run(sources(url)))
    change = found["get_changes"][0]
    assert "ORDER_TAX_RATE_BPS 825 -> 8.25" in change.evidence.summary
    logs = found["get_logs"][0]
    assert "ValueError" in logs.evidence.summary and "2.14.1" in logs.evidence.summary
    assert len(logs.evidence.summary) <= 4000
    snap = json.loads(logs.snapshot)
    assert snap["tool"] == "get_logs" and len(snap["records"]) == len(logs.records)
    assert logs.evidence.content_sha256 == __import__("hashlib").sha256(logs.snapshot).hexdigest()
    assert logs.attributes["terms"] == ["TimeoutError", "ValueError"]


# ---------------------------------------------------------------- FR-08: gaps, never healthy
def test_fr08_withheld_change_history_is_a_gap_and_leaves_a_required_source_missing(tmp_path):
    root = copy_reference(tmp_path, without=("release.json", "config-diff.txt"))
    with serving(root) as url:
        out = run(sources(url))
    gap = by_tool(out)["get_changes"][0].evidence
    assert gap.kind == EvidenceKind.gap and "HTTP 404" in gap.gap_reason
    assert "not a sign of health" in gap.summary and gap.strength_tier is None
    assert by_tool(out)["get_changes"][0].attributes["missing_kind"] == "change"
    assert out.gap_count == 1
    assert missing_required_sources([c.evidence for c in out.collected]) == ["change history"]


def test_fr08_an_unreachable_source_makes_every_call_a_gap():
    with serving() as url:
        dead = url  # the server stops when the block ends
    out = run(sources(dead))
    assert out.gap_count == 8 and all(c.evidence.gap_reason for c in out.collected)
    assert missing_required_sources([c.evidence for c in out.collected]) == [
        "alert",
        "change history",
        "logs or error tracker",
    ]


def test_fr08_a_slow_source_times_out_into_a_gap():
    slow = Canned([], delay=0.5)
    out = run(SourceSet({"*": slow}), [Call("get_logs", {"service": "orders-api"})], timeout_s=0.05)
    gap = out.collected[0].evidence
    assert gap.kind == EvidenceKind.gap and "no answer in 0.05 s" in gap.gap_reason


def test_fr08_no_configured_source_and_an_unreported_metric_are_gaps():
    with serving() as url:
        only_alert = SourceSet({"get_alert": sources(url).sources["*"]})
        out = run(only_alert)
        latency = run(
            sources(url),
            [Call("get_metric", {"service": "orders-api", "metric": "http_latency_p95"})],
        )
    reasons = {c.evidence.tool: c.evidence.gap_reason for c in out.collected}
    assert reasons.pop("get_alert") is None
    assert all("no source is configured" in r for r in reasons.values())
    assert "not reported by this source" in latency.collected[0].evidence.gap_reason


def test_an_empty_answer_is_evidence_not_a_gap():
    out = run(SourceSet({"*": Canned([])}), [Call("get_logs", {"service": "orders-api"})])
    e = out.collected[0].evidence
    assert e.kind == EvidenceKind.log and "no records in the window" in e.summary


# ---------------------------------------------------------------- scope, size, redaction
def test_st22_records_of_another_service_or_environment_are_dropped_by_the_adapter(tmp_path):
    root = copy_reference(tmp_path)
    logs = root / "reference-incident/logs.ndjson"
    line = json.loads(logs.read_text().splitlines()[1])
    planted = [
        line | {"service": "payments-api", "ts": "2026-10-20T14:05:00Z"},
        line | {"environment": "staging", "ts": "2026-10-20T14:06:00Z"},
    ]
    logs.write_text(logs.read_text() + "".join(json.dumps(p) + "\n" for p in planted))
    with serving(root) as url:
        out = run(sources(url), [Call("get_logs", {"service": "orders-api"})])
    stamps = [r["ts"] for r in out.collected[0].records]
    assert "2026-10-20T14:05:00Z" not in stamps and "2026-10-20T14:06:00Z" not in stamps


def test_nfr29_an_oversize_result_is_cut_by_whole_records_and_marked_partial():
    big = [{"ts": "2026-10-20T14:10:00Z", "level": "ERROR", "message": "x" * 1000}] * 400
    out = run(SourceSet({"*": Canned(big)}), [Call("get_logs", {"service": "orders-api"})])
    c = out.collected[0]
    assert c.evidence.is_partial and len(c.snapshot) <= REGISTRY["get_logs"].max_output_bytes
    assert len(json.loads(c.snapshot)["records"]) == len(c.records) < 400


SEEDED = [
    "password=hunter2-canary",
    "Authorization: Bearer abcdef0123456789abcdef",
    "AKIAABCDEFGHIJKLMNOP",
    "sk-canary0123456789abcdefgh",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJjYW5hcnkifQ.c2lnbmF0dXJl",
    "alice.canary@example.com",
    "card 4111 1111 1111 1111",
    "from 10.20.30.40",
]


@pytest.mark.parametrize("secret", SEEDED)
def test_nfr18_secrets_and_personal_data_are_redacted_before_storage(secret):
    record = {
        "ts": "2026-10-20T14:10:00Z",
        "level": "ERROR",
        "message": f"failed: {secret}",
        "error_type": "RuntimeError",
        "error_message": secret,
        "stack": f"line\n{secret}\nRuntimeError",
        "path": "POST /v1/orders",
        "version": "2.14.1",
        "instance": "a",
        "trace_id": "t",
        "status": 500,
    }
    out = run(SourceSet({"*": Canned([record])}), [Call("get_logs", {"service": "orders-api"})])
    c = out.collected[0]
    stored = c.evidence.summary + c.snapshot.decode() + json.dumps(c.attributes)
    canary = secret.split()[-1].split("=")[-1]
    assert canary not in stored and "[REDACTED]" in stored


# ---------------------------------------------------------------- the gate, repeats, scope
def test_a_repeated_call_is_answered_from_the_stored_result_without_a_query():
    src = Canned([])
    s = scope()
    store = MemoryToolCalls()
    call = Call("get_logs", {"service": "orders-api"})
    first = run(
        SourceSet({"*": src}),
        [call],
        s,
        gate=ToolGate(s.incident_id, s.service, STARTED, CUT_OFF, store=store),
    )
    row = first.calls[0]
    store.add(
        s.incident_id,
        {
            "tool": row.tool,
            "hash": row.call_hash,
            "evidence_ids": list(row.evidence_ids),
            "new_evidence": 1,
            "skill": "collect_evidence",
            "params": row.params,
        },
    )
    again = run(
        SourceSet({"*": src}),
        [call],
        s,
        gate=ToolGate(s.incident_id, s.service, STARTED, CUT_OFF, store=store),
    )
    assert src.calls == 1 and not again.collected
    assert again.calls[0].evidence_ids == row.evidence_ids and again.calls[0].new_evidence == 0


# ---------------------------------------------------------------- configuration
def test_sources_are_chosen_per_tool_by_configuration():
    s = SourceSet.from_config(
        {
            "sources": {
                "*": {"adapter": "fake_monitoring", "url": "http://a/v1/scenarios/x"},
                "get_logs": {
                    "adapter": "fake_monitoring",
                    "url": "http://b/v1/scenarios/x",
                    "name": "logs-b",
                },
            },
            "replay_as_of": "2026-10-20T14:20:00Z",
        }
    )
    assert s.for_tool("get_logs").url == "http://b/v1/scenarios/x"
    assert s.for_tool("get_alert").url == "http://a/v1/scenarios/x"
    assert s.replay_as_of == CUT_OFF


@pytest.mark.parametrize(
    "config",
    [
        {"sources": {"*": {"adapter": "fake_monitoring", "url": "http://a"}}},
        {"sources": {}, "replay_as_of": "2026-10-20T14:20:00Z"},
    ],
)
def test_local_only_adapters_and_replay_are_refused_in_production(config):
    with pytest.raises(ValueError):
        SourceSet.from_config(config, production=True)


def test_an_unknown_adapter_is_refused():
    with pytest.raises(ValueError, match="unknown evidence adapter"):
        SourceSet.from_config({"sources": {"*": {"adapter": "nope"}}})


def test_source_unavailable_is_the_only_failure_a_source_reports():
    class Broken:
        def fetch(self, tool: str, params: BaseModel, environment: str) -> SourceResult:
            raise SourceUnavailable("down for maintenance")

    out = run(SourceSet({"*": Broken()}), [Call("get_alert", {"service": "orders-api"})])
    assert out.collected[0].evidence.gap_reason == "down for maintenance"


# ---------------------------------------------------------------- snapshots
def test_s3_signature_is_sigv4_and_never_holds_the_secret():
    store = S3Snapshots("http://127.0.0.1:59000", "b", "AKIDEXAMPLE", SecretStr("wJalr-canary"))
    when = datetime(2026, 10, 20, 14, 20, tzinfo=UTC)
    h = store.signed_headers("PUT", "/b/k.json", b"{}", "application/json", when)
    assert h["x-amz-date"] == "20261020T142000Z"
    assert h["authorization"].startswith(
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20261020/us-east-1/s3/aws4_request, "
        "SignedHeaders=content-type;host;x-amz-content-sha256;x-amz-date, Signature="
    )
    assert "canary" not in json.dumps(h)
    assert h == store.signed_headers("PUT", "/b/k.json", b"{}", "application/json", when)
    assert h != store.signed_headers("PUT", "/b/k.json", b"{ }", "application/json", when)


# ---------------------------------------------------------------- T4.2: bounds on a source
class Flaky:
    """Fails its first `failures` calls, then answers; counts calls."""

    name = "flaky"

    def __init__(self, failures: int) -> None:
        self.failures, self.calls = failures, 0

    def fetch(self, tool: str, params: BaseModel, environment: str) -> SourceResult:
        self.calls += 1
        if self.calls <= self.failures:
            raise SourceUnavailable("flaky: HTTP 503")
        return SourceResult("flaky", "ref:flaky", [])


LOGS = [Call("get_logs", {"service": "orders-api"})]


def test_nfr47_a_failing_call_is_retried_at_most_twice_with_backoff():
    once = Flaky(1)
    t0 = time.monotonic()
    out = run(SourceSet({"*": once}), LOGS, backoff_s=(0.2, 0.4))
    assert once.calls == 2 and out.gap_count == 0 and time.monotonic() - t0 >= 0.2
    always = Flaky(99)
    out = run(SourceSet({"*": always}), LOGS, backoff_s=(0, 0))
    assert always.calls == 3 and out.gap_count == 1
    assert "HTTP 503" in out.collected[0].evidence.gap_reason


def test_nfr28_the_breaker_opens_and_calls_stop_reaching_the_source():
    from aisre.reliability import telemetry

    dead = Flaky(10_000)
    src = SourceSet({"*": dead})
    for _ in range(4):  # 3 tries each: the 10th failure opens it (ADR-61: half of 10 in 60 s)
        run(src, LOGS, backoff_s=(0, 0))
    reached = dead.calls
    assert reached in (10, 11, 12)
    out = run(src, LOGS, backoff_s=(0, 0))
    assert dead.calls == reached  # refused at once while open: the source isn't called
    assert "circuit open" in out.collected[0].evidence.gap_reason
    gauge = telemetry.REGISTRY.get_sample_value(
        "aisre_circuit_open", {"dependency": "evidence:flaky"}
    )
    assert gauge == 1
