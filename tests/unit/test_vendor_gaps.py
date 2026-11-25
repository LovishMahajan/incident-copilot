"""Artifact 9.4, 13.5 (M5, ADR-75): the four tools no adapter answered at pass 2, per stack.

`get_alert`, `get_traces`, `get_error_groups` and `get_dependency_health` from Prometheus,
Alertmanager, Loki and Tempo; Datadog; and CloudWatch, X-Ray and CloudWatch Logs Insights. HTTP
vendors answer from tests/fixtures/recorded/ (each file's `_source` cites the documented API and
version it is shaped after); AWS from botocore's Stubber, which checks each request and response
against the service model. None of this shows that an adapter works against a real account.

What each test pins: the platform's record shape (the fake monitoring source's, which the
collector and the fact rules read), the call's scope (another service's or environment's data is
dropped, FR-44), the caps and `partial`, and failures as SourceUnavailable: a gap, never health
or an empty answer (ADR-54).
"""

from __future__ import annotations

import json
import urllib.request
from datetime import UTC, datetime, timedelta

import pytest
from recorded_http import Recorded, load

from aisre.agent.agent_contracts import EvidenceKind
from aisre.agent.tool_registry import AlertQuery, DependencyQuery, ErrorGroupQuery, TraceQuery
from aisre.evidence import sources
from aisre.evidence.collector import summarise
from aisre.evidence.facts import ChangeInfo, Observed, facts_for
from aisre.evidence.sources import SourceSet, SourceUnavailable

T0 = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)
WINDOW = {"start": T0 - timedelta(minutes=5), "end": T0 + timedelta(minutes=30)}
AT = T0 + timedelta(minutes=10)
CHANGE = ChangeInfo("1.4.0", "1.3.2", ("TAX_RATE",), ("orders/service.py",), T0, (), None)


@pytest.fixture
def recorded(monkeypatch):
    def use(name: str) -> Recorded:
        r = Recorded(load(name))
        monkeypatch.setattr(urllib.request, "urlopen", r)
        return r

    return use


def alert(service: str = "orders-api", **kw) -> AlertQuery:
    return AlertQuery(service=service, **kw)


def traces(service: str = "orders-api", **kw) -> TraceQuery:
    return TraceQuery(service=service, **WINDOW, **kw)


def groups(service: str = "orders-api", **kw) -> ErrorGroupQuery:
    return ErrorGroupQuery(service=service, **WINDOW, **kw)


def deps(service: str = "orders-api") -> DependencyQuery:
    return DependencyQuery(service=service, at=AT)


def source(tool: str, spec: dict, production: bool = True):
    return SourceSet.from_config({"sources": {tool: spec}}, production=production).for_tool(tool)


def test_the_new_adapters_are_registered_and_none_is_local_only():
    for name in ("alertmanager", "tempo"):
        assert name in sources.ADAPTERS and name not in sources.LOCAL_ONLY


# ---------------------------------------------------------------------------
# Shared rules
# ---------------------------------------------------------------------------
def test_error_groups_merge_by_type_and_message_in_order_of_first_sighting():
    rows = [
        {"type": "B", "message": "m", "version": "2", "count": 1,
         "first_seen": "2026-11-19T10:05:00Z", "last_seen": "2026-11-19T10:06:00Z"},
        {"type": "A", "message": "m", "version": "2", "count": 2,
         "first_seen": "2026-11-19T10:02:00Z", "last_seen": "2026-11-19T10:03:00Z",
         "frames": [["a.py", "1"]]},
        {"type": "A", "message": "m", "version": "1", "count": 3,
         "first_seen": "2026-11-19T10:01:00Z", "last_seen": "2026-11-19T10:02:00Z",
         "frames": [["a.py", "1"], ["b.py", "9"]]},
        {"type": "C", "message": "m", "count": 0,
         "first_seen": "2026-11-19T10:00:00Z", "last_seen": "2026-11-19T10:00:00Z"},
    ]  # fmt: skip
    assert sources.error_group_records(rows) == [
        {
            "type": "A",
            "message": "m",
            "count": 5,
            "first_seen": "2026-11-19T10:01:00Z",
            "last_seen": "2026-11-19T10:03:00Z",
            "versions": ["1", "2"],
            "frames": [["a.py", "1"], ["b.py", "9"]],
        },
        {
            "type": "B",
            "message": "m",
            "count": 1,
            "first_seen": "2026-11-19T10:05:00Z",
            "last_seen": "2026-11-19T10:06:00Z",
            "versions": ["2"],
            "frames": [],
        },
    ]


def test_a_dependency_without_data_is_unknown_never_healthy():
    spec = sources.dependency_specs(
        {"db": {"kind": "postgresql", "error_rate": "q", "latency_p95": "q", "max_latency_s": 0.5}},
        "x",
    )["db"]
    record = sources.dependency_record
    assert record("db", spec, {"error_rate": 0.0, "latency_p95": 0.1}, AT, "")["status"] == (
        "healthy"
    )
    assert record("db", spec, {"error_rate": 0.0, "latency_p95": 0.9}, AT, "")["status"] == (
        "degraded"
    )
    assert record("db", spec, {"error_rate": 0.0, "latency_p95": None}, AT, "")["status"] == (
        "unknown"
    )
    assert record("db", spec, {}, AT, "")["status"] == "unknown"


@pytest.mark.parametrize(
    "deps, error",
    [
        ({}, "dependencies is"),
        ({"a b": {"kind": "x", "error_rate": "q"}}, "not a plain value"),
        ({"db": {"kind": "x"}}, "needs an error_rate or latency_p95"),
        ({"db": {"error_rate": "q"}}, "needs a kind"),
        ({"db": {"kind": "x", "latency_p95": "q"}}, "positive numbers"),
        ({"db": {"kind": "x", "error_rate": "q", "max_error_rate": -1}}, "positive numbers"),
        ({"db": {"kind": "x", "error_rate": "q", "url": "q"}}, "takes"),
    ],
)
def test_bad_dependency_configuration_is_refused_at_start(deps, error):
    with pytest.raises(ValueError, match=error):
        sources.dependency_specs(deps, "x")


# ---------------------------------------------------------------------------
# Prometheus stack: Alertmanager, Tempo, Loki error groups, Prometheus dependencies
# ---------------------------------------------------------------------------
AM = {"adapter": "alertmanager", "url": "https://alertmanager.example", "token": {"env": "AM_T"}}


def test_alertmanager_alerts_of_this_service_newest_first(recorded, monkeypatch):
    r = recorded("prometheus-gaps")
    monkeypatch.setenv("AM_T", "am-not-real")
    out = source("get_alert", AM).fetch("get_alert", alert(), "production")
    assert [a["alert_name"] for a in out.records] == [
        "OrdersApiHighErrorRate",
        "OrdersApiLatencyHigh",
    ]
    first = out.records[0]
    assert first == {
        "alert_name": "OrdersApiHighErrorRate",
        "alert_id": "9f8e7d6c5b4a3921",
        "state": "firing",
        "service": "orders-api",
        "environment": "production",
        "rule": 'sum(rate(http_requests_total{service="orders-api",code=~"5.."}[1m])) / '
        'sum(rate(http_requests_total{service="orders-api"}[1m])) > 0.05',
        "value": 0.149,
        "threshold": 0.05,
        "breach_started_at": None,
        "fired_at": "2026-11-19T10:03:00Z",
    }
    sent = r.sent[0]
    assert 'filter=service="orders-api"' in sent.query
    assert 'filter=environment="production"' in sent.query
    assert "silenced=false" in sent.query and "active=true" in sent.query
    assert sent.headers["authorization"] == "Bearer am-not-real"
    assert "payments" not in str(out.records) and "staging" not in str(out.records)
    assert "Alert OrdersApiHighErrorRate (9f8e7d6c5b4a3921) firing" in summarise(
        "get_alert", {"service": "orders-api"}, out.records
    )


def test_alertmanager_alert_id_narrows_to_that_alert(recorded):
    recorded("prometheus-gaps")
    src = source("get_alert", AM | {"token": None})
    out = src.fetch("get_alert", alert(alert_id="a1b2c3d4e5f60718"), "production")
    assert [a["alert_name"] for a in out.records] == ["OrdersApiLatencyHigh"]


def test_alertmanager_failures_are_gaps(recorded):
    recorded("prometheus-gaps")
    src = source("get_alert", AM | {"token": None})
    with pytest.raises(SourceUnavailable, match="alertmanager: HTTP 503"):
        src.fetch("get_alert", alert("checkout"), "production")
    with pytest.raises(SourceUnavailable, match="not a list of alerts"):
        src.fetch("get_alert", alert("weird"), "production")
    with pytest.raises(SourceUnavailable, match="has no get_traces"):
        src.fetch("get_traces", traces(), "production")


def test_a_quote_in_a_value_cannot_change_an_alertmanager_matcher(recorded):
    r = recorded("prometheus-gaps")
    src = source("get_alert", AM | {"token": None})
    src.fetch("get_alert", alert('orders-api",service=~".+'), "production")
    assert 'filter=service="orders-api\\",service=~\\".+"' in r.sent[0].query


TEMPO = {"adapter": "tempo", "url": "https://tempo.example", "tenant": "orders"}


def test_tempo_failing_traces_of_this_service_in_the_platforms_shape(recorded):
    r = recorded("prometheus-gaps")
    out = source("get_traces", TEMPO).fetch("get_traces", traces(), "production")
    assert [t["trace_id"] for t in out.records] == ["4bf92f3577b34da6a3ce929d0e0e4736"]
    spans = out.records[0]["spans"]
    assert [(s["name"], s["status"]) for s in spans] == [
        ("POST /checkout", "ERROR"),
        ("orders.service.place_order", "ERROR"),
        ("SELECT cart", "OK"),
    ]
    assert spans[0]["start"] == "2026-11-19T10:01:05.000Z"
    assert spans[0]["attributes"]["http.response.status_code"] == 500
    assert spans[0]["exceptions"] == [{"type": "KeyError", "message": "'TAX_RATE'"}]
    assert "someone else" not in str(out.records)  # payments-api's span, dropped (FR-44)
    search = r.to("GET", "/api/search")[0]
    assert 'resource.service.name = "orders-api"' in search.query
    assert f"start={int(WINDOW['start'].timestamp())}" in search.query and "limit=5" in search.query
    assert search.headers["x-scope-orgid"] == "orders"
    assert len(r.to("GET", "/api/traces/")) == 2  # the malformed ID is never requested
    assert not out.partial
    # The fact rules read Tempo's spans as they read the fake's.
    facts = facts_for(Observed(EvidenceKind.trace, {}, out.records), [CHANGE])
    assert facts.changed_key_on_failing_path and facts.frames_in_changed_file


def test_tempo_path_filter_trace_cap_and_timeout(recorded):
    r = recorded("prometheus-gaps")
    src = source("get_traces", TEMPO)
    assert src.fetch("get_traces", traces(path="/refunds"), "production").records == []
    capped = source("get_traces", TEMPO | {"max_traces": 1})
    out = capped.fetch("get_traces", traces(), "production")
    assert out.partial and len(r.to("GET", "/api/traces/")) == 3
    with pytest.raises(SourceUnavailable, match="no answer in"):
        src.fetch("get_traces", traces("slow-api"), "production")


@pytest.mark.parametrize(
    "spec, error",
    [
        (TEMPO | {"query": "{ status = error }"}, "\\$service"),
        (TEMPO | {"max_traces": 50}, "max_traces"),
        (AM | {"service_label": "service name"}, "label name"),
    ],
)
def test_bad_prometheus_stack_configuration_is_refused_at_start(spec, error):
    with pytest.raises(ValueError, match=error):
        SourceSet.from_config({"sources": {"*": spec}})


LOKI_GROUPS = {
    "adapter": "loki",
    "url": "https://loki.example",
    "error_groups": {
        "selector": '{service="$service",env="$environment"} | json | level="error"',
        "step_s": 60,
    },
}


def test_loki_error_groups_derived_from_a_logql_count(recorded):
    r = recorded("prometheus-gaps")
    src = source("get_error_groups", LOKI_GROUPS)
    out = src.fetch("get_error_groups", groups(), "production")
    assert out.records == [
        {
            "type": "KeyError",
            "message": "'TAX_RATE'",
            "count": 9,
            "first_seen": "2026-11-19T10:00:00Z",
            "last_seen": "2026-11-19T10:02:00Z",
            "versions": ["1.3.2", "1.4.0"],
            "frames": [],
        },
        {
            "type": "TimeoutError",
            "message": "upstream timed out",
            "count": 2,
            "first_seen": "2026-11-19T10:05:00Z",
            "last_seen": "2026-11-19T10:06:00Z",
            "versions": ["1.4.0"],
            "frames": [],
        },
    ]
    q = r.sent[0].query
    assert (
        'query=sum by (error_type, error_message, version) (count_over_time({service="orders-api",'
        'env="production"} | json | level="error" [60s]))'
    ) in q
    assert "step=60s" in q
    facts = facts_for(Observed(EvidenceKind.error_group, {}, out.records), [CHANGE])
    assert facts.first_seen_after_change and not facts.frames_in_changed_file  # no stacks


def test_loki_error_groups_release_filter_cap_and_failure(recorded):
    recorded("prometheus-gaps")
    src = source("get_error_groups", LOKI_GROUPS)
    out = src.fetch("get_error_groups", groups(release="1.3.2"), "production")
    assert [g["type"] for g in out.records] == ["KeyError"]
    out = src.fetch("get_error_groups", groups(limit=1), "production")
    assert len(out.records) == 1 and out.partial
    with pytest.raises(SourceUnavailable, match="loki: HTTP 400"):
        src.fetch("get_error_groups", groups("broken-api"), "production")
    with pytest.raises(SourceUnavailable, match="has no get_logs"):
        src.fetch("get_logs", groups(), "production")


@pytest.mark.parametrize(
    "error_groups, error",
    [
        ({"selector": '{job="x"}'}, "\\$service"),
        ({"selector": '{service="$service"}', "type_label": "a-b"}, "label name"),
        ({"selector": '{service="$service"}', "step_s": 1}, "step_s"),
        ({"selector": '{service="$service"}', "by": "x"}, "takes"),
    ],
)
def test_bad_loki_error_group_configuration_is_refused(error_groups, error):
    with pytest.raises(ValueError, match=error):
        SourceSet.from_config({"sources": {"*": LOKI_GROUPS | {"error_groups": error_groups}}})


PROM_DEPS = {
    "adapter": "prometheus",
    "url": "https://prometheus.example",
    "dependencies": {
        "orders-db": {
            "kind": "postgresql",
            "error_rate": 'sum(rate(dep_errors_total{service="$service",dependency="$dependency"}[5m]))',
            "latency_p95": "histogram_quantile(0.95, sum by (le) (rate(dep_latency_bucket"
            '{service="$service",dependency="$dependency"}[5m])))',
            "max_latency_s": 0.25,
        },
        "payment-gateway": {
            "kind": "third_party_http",
            "error_rate": 'sum(rate(dep_errors_total{dependency="$dependency"}[5m]))',
            "latency_p95": 'max(dep_latency{dependency="$dependency"})',
            "max_latency_s": 1,
        },
    },
}


def test_prometheus_dependency_health_from_instant_queries_at_the_call_time(recorded):
    r = recorded("prometheus-gaps")
    out = source("get_dependency_health", PROM_DEPS).fetch(
        "get_dependency_health", deps(), "production"
    )
    assert [(d["name"], d["kind"], d["status"]) for d in out.records] == [
        ("orders-db", "postgresql", "healthy"),
        ("payment-gateway", "third_party_http", "unknown"),  # no latency data: never healthy
    ]
    assert out.records[0]["observed_at"] == "2026-11-19T10:10:00Z"
    assert "error_rate 0.002 (limit 0.05)" in out.records[0]["detail"]
    assert "error_rate 0.2" in out.records[1]["detail"] and "no data" in out.records[1]["detail"]
    assert out.partial  # a dependency without data
    assert all("time=2026-11-19T10:10:00Z" in s.query for s in r.sent)
    assert 'dependency="orders-db"' in r.sent[0].query
    assert "orders-db (postgresql): healthy" in summarise(
        "get_dependency_health", {"service": "orders-api"}, out.records
    )


def test_prometheus_dependency_failures_are_gaps_never_health(recorded):
    recorded("prometheus-gaps")
    for name, error in (("broken-dep", "prometheus: HTTP 422"), ("two-series", "aggregate it")):
        spec = PROM_DEPS | {"dependencies": {name: {"kind": "x", "error_rate": "dep_errors_total"
                                                    '{dependency="$dependency"}'}}}  # fmt: skip
        src = source("get_dependency_health", spec)
        with pytest.raises(SourceUnavailable, match=error):
            src.fetch("get_dependency_health", deps(), "production")
    no_deps = source("get_dependency_health", PROM_DEPS | {"dependencies": None, "metrics": {}})
    with pytest.raises(SourceUnavailable, match="has no get_dependency_health"):
        no_deps.fetch("get_dependency_health", deps(), "production")


# ---------------------------------------------------------------------------
# Datadog: monitors, APM spans, Error Tracking, metrics per dependency
# ---------------------------------------------------------------------------
DD = {
    "adapter": "datadog",
    "site": "datadoghq.eu",
    "api_key": {"env": "DD_API_KEY"},
    "app_key": {"env": "DD_APP_KEY"},
    "dependencies": {
        "orders-db": {
            "kind": "postgresql",
            "error_rate": "avg:dep.errors{service:$service,env:$environment,dependency:$dependency}",
            "latency_p95": "p95:dep.latency{service:$service,dependency:$dependency}",
            "max_latency_s": 0.5,
        }
    },
}


@pytest.fixture
def datadog(monkeypatch, recorded):
    monkeypatch.setenv("DD_API_KEY", "dd-api-not-real")
    monkeypatch.setenv("DD_APP_KEY", "dd-app-not-real")
    r = recorded("datadog-gaps")
    return SourceSet.from_config({"sources": {"*": DD}}, production=True).for_tool("get_alert"), r


def test_datadog_triggered_monitors_of_this_service(datadog):
    src, r = datadog
    out = src.fetch("get_alert", alert(), "production")
    assert [(a["alert_id"], a["state"]) for a in out.records] == [
        ("101", "firing"),
        ("102", "warning"),
    ]
    assert out.records[0] == {
        "alert_name": "orders-api 5xx rate",
        "alert_id": "101",
        "state": "firing",
        "service": "orders-api",
        "environment": "production",
        "rule": "avg(last_5m):sum:trace.http.request.errors{service:orders-api} > 0.05",
        "value": None,  # a monitor doesn't hold the value that triggered it
        "threshold": 0.05,
        "breach_started_at": None,
        "fired_at": "2026-11-19T10:04:10Z",
    }
    assert out.records[1]["fired_at"] == "2026-11-19T10:04:10Z"  # overall_state_modified
    sent = r.sent[0]
    assert sent.url.startswith("https://api.datadoghq.eu/api/v1/monitor?")
    assert "monitor_tags=service:orders-api,env:production" in sent.query
    assert "group_states=alert,warn" in sent.query
    assert sent.headers["dd-application-key"] == "dd-app-not-real"
    for other in ("payments", "staging", "untagged", "disk"):
        assert other not in str(out.records)
    narrowed = src.fetch("get_alert", alert(alert_id="102"), "production")
    assert [a["alert_id"] for a in narrowed.records] == ["102"]


def test_datadog_error_spans_grouped_into_traces(datadog):
    src, r = datadog
    out = src.fetch("get_traces", traces(), "production")
    assert [t["trace_id"] for t in out.records] == [
        "4bf92f3577b34da6a3ce929d0e0e4736",
        "0af7651916cd43dd8448eb211c80319c",
    ]
    first = out.records[0]["spans"]
    assert [(s["name"], s["status"], s["start"]) for s in first] == [
        ("POST /checkout", "ERROR", "2026-11-19T10:01:05.100Z"),
        ("orders.service.place_order", "ERROR", "2026-11-19T10:01:05.100Z"),
    ]
    assert first[0]["exceptions"] == [{"type": "KeyError", "message": "'TAX_RATE'"}]
    assert first[1]["attributes"]["code.filepath"] == "orders/service.py"
    assert out.partial  # the search has a next page
    body = r.to("POST", "/api/v2/spans/events/search")[0].body["data"]["attributes"]
    assert body["filter"] == {
        "query": "service:orders-api env:production status:error",
        "from": "2026-11-19T09:55:00Z",
        "to": "2026-11-19T10:30:00Z",
    }
    assert body["page"] == {"limit": 200}
    assert "someone else" not in str(out.records) and "staging only" not in str(out.records)
    facts = facts_for(Observed(EvidenceKind.trace, {}, out.records), [CHANGE])
    assert facts.changed_key_on_failing_path and facts.frames_in_changed_file
    on_path = src.fetch("get_traces", traces(path="/refunds"), "production")
    assert [t["trace_id"] for t in on_path.records] == ["0af7651916cd43dd8448eb211c80319c"]


def test_datadog_error_tracking_issues_are_native_error_groups(datadog):
    src, r = datadog
    out = src.fetch("get_error_groups", groups(), "production")
    assert out.records == [
        {
            "type": "builtins.KeyError",
            "message": "'TAX_RATE'",
            "count": 82,
            "first_seen": "2026-11-19T10:01:02Z",
            "last_seen": "2026-11-19T10:15:00Z",
            "versions": ["1.4.0"],
            "frames": [["orders/service.py", ""]],
        },
        {
            "type": "builtins.TimeoutError",
            "message": "upstream timed out",
            "count": 4,
            "first_seen": "2026-10-20T10:00:00Z",
            "last_seen": "2026-11-19T10:05:00Z",
            "versions": ["1.1.0", "1.4.0"],
            "frames": [],
        },
    ]
    sent = r.to("POST", "/api/v2/error-tracking/issues/search")[0]
    assert "include=issue" in sent.query
    assert sent.body["data"]["attributes"] == {
        "query": "service:orders-api env:production",
        "from": int(WINDOW["start"].timestamp() * 1000),
        "to": int(WINDOW["end"].timestamp() * 1000),
        "track": "trace",
    }
    facts = facts_for(Observed(EvidenceKind.error_group, {}, out.records), [CHANGE])
    assert facts.frames_in_changed_file and facts.first_seen_after_change
    assert "at orders/service.py -" not in summarise("get_error_groups", {}, out.records)
    assert "versions 1.4.0 at orders/service.py" in summarise("get_error_groups", {}, out.records)
    released = src.fetch("get_error_groups", groups(release="1.1.0"), "production")
    assert [g["type"] for g in released.records] == ["builtins.TimeoutError"]
    capped = src.fetch("get_error_groups", groups(limit=1), "production")
    assert len(capped.records) == 1 and capped.partial


def test_datadog_dependency_health_from_configured_metrics(datadog):
    src, r = datadog
    out = src.fetch("get_dependency_health", deps(), "production")
    assert [(d["name"], d["status"]) for d in out.records] == [("orders-db", "degraded")]
    assert "latency_p95 0.9 s (limit 0.5 s)" in out.records[0]["detail"]
    assert "error_rate 0.002" in out.records[0]["detail"]  # the last point with a value
    first = r.to("GET", "/api/v1/query")[0]
    assert "dependency:orders-db" in first.query
    assert f"to={int(AT.timestamp())}" in first.query
    assert f"from={int((AT - timedelta(minutes=5)).timestamp())}" in first.query
    assert not out.partial


def test_datadog_failures_are_gaps_never_health(datadog, monkeypatch):
    src, _ = datadog
    with pytest.raises(SourceUnavailable, match="datadog: HTTP 403"):
        src.fetch("get_alert", alert("checkout"), "production")
    with pytest.raises(SourceUnavailable, match="datadog: HTTP 429"):
        src.fetch("get_traces", traces("checkout"), "production")
    with pytest.raises(SourceUnavailable, match="datadog: HTTP 400"):
        src.fetch("get_error_groups", groups("checkout"), "production")
    with pytest.raises(SourceUnavailable, match="plain tag value"):
        src.fetch("get_alert", alert("orders-api OR *"), "production")
    broken = DD | {
        "dependencies": {"broken-dep": {"kind": "x", "error_rate": "dep.errors{$dependency}"}}
    }
    b = SourceSet.from_config({"sources": {"*": broken}}).for_tool("get_dependency_health")
    with pytest.raises(SourceUnavailable, match="the query failed"):
        b.fetch("get_dependency_health", deps(), "production")
    nodata = DD | {
        "dependencies": {"cache": {"kind": "redis", "error_rate": "dep.errors{$dependency}"}}
    }
    n = SourceSet.from_config({"sources": {"*": nodata}}).for_tool("get_dependency_health")
    out = n.fetch("get_dependency_health", deps(), "production")
    assert out.records[0]["status"] == "unknown" and out.partial
    plain = SourceSet.from_config({"sources": {"*": DD | {"dependencies": None}}})
    with pytest.raises(SourceUnavailable, match="has no get_dependency_health"):
        plain.for_tool("x").fetch("get_dependency_health", deps(), "production")


@pytest.mark.parametrize(
    "extra, error",
    [
        ({"monitor_tags": "team:orders"}, "monitor_tags"),
        ({"spans_query": "env:prod"}, "spans_query"),
        ({"error_tracking_track": "rum"}, "trace or logs"),
        ({"max_traces": 0}, "max_traces"),
        ({"dependency_lookback_s": 5}, "dependency_lookback_s"),
    ],
)
def test_bad_datadog_configuration_is_refused_at_start(extra, error):
    with pytest.raises(ValueError, match=error):
        SourceSet.from_config({"sources": {"*": DD | extra}})


# ---------------------------------------------------------------------------
# CloudWatch, X-Ray and Logs Insights, through botocore's Stubber
# ---------------------------------------------------------------------------
CW = {
    "adapter": "cloudwatch",
    "region": "eu-west-1",
    "alarm_dimensions": {"Service": "$service", "Environment": "$environment"},
    "log_group": "/ecs/$service-$environment",
    "error_groups_query": "fields error.type as type, error.message as message, version"
    ' | filter level = "error" and service = "$service"'
    " | stats count(*) as count, min(@timestamp) as first_seen, max(@timestamp) as last_seen,"
    " latest(error.stack) as stack by type, message, version",
    "dependencies": {
        "orders-db": {
            "kind": "postgresql",
            "error_rate": 'SUM(SEARCH(\'{App,Service,Dependency} Service="$service" '
            'Dependency="$dependency" MetricName="Errors"\', \'Average\', 60))',
            "latency_p95": 'MAX(SEARCH(\'{App,Service,Dependency} Service="$service" '
            'Dependency="$dependency" MetricName="LatencyP95"\', \'Maximum\', 60))',
            "max_latency_s": 0.5,
        },
        "payment-gateway": {
            "kind": "third_party_http",
            "error_rate": "SUM(SEARCH('Dependency=\"$dependency\" Errors', 'Average', 60))",
        },
    },
    "poll_s": 0,
}


@pytest.fixture
def aws(monkeypatch):
    pytest.importorskip("boto3")
    from botocore.stub import Stubber

    for var in ("AWS_PROFILE", "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDNOTREAL")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "not-a-real-secret")

    def build(**extra):
        src = SourceSet.from_config({"sources": {"*": CW | extra}}, production=True).for_tool("x")
        stubs = {s: Stubber(src.client(s)) for s in ("cloudwatch", "logs", "xray")}
        for s in stubs.values():
            s.activate()
        return src, stubs

    return build


def metric_alarm(name, dims, *, reason=None, metrics=None, fired=T0 + timedelta(minutes=4)):
    alarm = {
        "AlarmName": name,
        "AlarmArn": f"arn:aws:cloudwatch:eu-west-1:123456789012:alarm:{name}",
        "AlarmDescription": "5xx rate over 5%",
        "StateValue": "ALARM",
        "StateReason": "Threshold Crossed: 3 datapoints were greater than the threshold (0.05).",
        "StateUpdatedTimestamp": fired,
        "StateTransitionedTimestamp": fired,
        "ComparisonOperator": "GreaterThanThreshold",
        "Threshold": 0.05,
        "EvaluationPeriods": 3,
        "Period": 60,
    }
    if metrics is None:
        alarm |= {
            "MetricName": "5xxRate",
            "Namespace": "App",
            "Statistic": "Average",
            "Dimensions": [{"Name": k, "Value": v} for k, v in dims.items()],
        }
    else:
        alarm["Metrics"] = metrics
    if reason is not None:
        alarm["StateReasonData"] = reason
    return alarm


REASON = (
    '{"version":"1.0","queryDate":"2026-11-19T10:04:10.123+0000","startDate":'
    '"2026-11-19T10:01:00.000+0000","statistic":"Average","period":60,"recentDatapoints":'
    '[0.12,0.13,0.149],"threshold":0.05,"evaluatedDatapoints":[{"timestamp":'
    '"2026-11-19T10:03:00.000+0000","sampleCount":5.0,"value":0.149},{"timestamp":'
    '"2026-11-19T10:01:00.000+0000","sampleCount":5.0,"value":0.12}]}'
)


def test_cloudwatch_alarms_in_alarm_for_the_services_dimensions(aws):
    src, stubs = aws()
    mine = {"Service": "orders-api", "Environment": "production"}
    math = [
        {
            "Id": "m1",
            "MetricStat": {
                "Metric": {
                    "Namespace": "App",
                    "MetricName": "Errors",
                    "Dimensions": [{"Name": k, "Value": v} for k, v in mine.items()],
                },
                "Period": 60,
                "Stat": "Sum",
            },
            "ReturnData": False,
        },
        {"Id": "e1", "Expression": "m1 / 100", "ReturnData": True},
    ]
    stubs["cloudwatch"].add_response(
        "describe_alarms",
        {
            "MetricAlarms": [
                metric_alarm("orders-5xx", mine, reason=REASON),
                metric_alarm("orders-math", {}, metrics=math, fired=T0 + timedelta(minutes=2)),
                metric_alarm("payments-5xx", mine | {"Service": "payments-api"}),
                metric_alarm("orders-staging", mine | {"Environment": "staging"}),
                metric_alarm("account-wide", {"Region": "eu-west-1"}),
            ],
            "CompositeAlarms": [],
        },
        {
            "StateValue": "ALARM",
            "AlarmTypes": ["MetricAlarm", "CompositeAlarm"],
            "MaxRecords": 100,
        },
    )
    out = src.fetch("get_alert", alert(), "production")
    assert [a["alert_name"] for a in out.records] == ["orders-5xx", "orders-math"]
    assert out.records[0] == {
        "alert_name": "orders-5xx",
        "alert_id": "arn:aws:cloudwatch:eu-west-1:123456789012:alarm:orders-5xx",
        "state": "firing",
        "service": "orders-api",
        "environment": "production",
        "rule": "Average of App 5xxRate GreaterThanThreshold 0.05 for 3 x 60 s",
        "value": 0.149,
        "threshold": 0.05,
        "breach_started_at": "2026-11-19T10:01:00Z",
        "fired_at": "2026-11-19T10:04:00Z",
    }
    assert out.records[1]["rule"] == "m1 / 100 GreaterThanThreshold 0.05"
    assert out.records[1]["value"] is None and not out.partial
    stubs["cloudwatch"].assert_no_pending_responses()


def test_cloudwatch_alarms_by_name_prefix_and_tags(aws):
    src, stubs = aws(alarm_name_prefix="$service-", alarm_tags={"env": "$environment"})
    composite = {
        "AlarmName": "orders-api-composite",
        "AlarmArn": "arn:aws:cloudwatch:eu-west-1:123456789012:alarm:orders-api-composite",
        "AlarmRule": 'ALARM("orders-5xx") OR ALARM("orders-latency")',
        "StateValue": "ALARM",
        "StateUpdatedTimestamp": T0 + timedelta(minutes=5),
    }
    other = metric_alarm("orders-api-staging", {})
    stubs["cloudwatch"].add_response(
        "describe_alarms",
        {"MetricAlarms": [other], "CompositeAlarms": [composite], "NextToken": "more"},
        {
            "StateValue": "ALARM",
            "AlarmTypes": ["MetricAlarm", "CompositeAlarm"],
            "MaxRecords": 100,
            "AlarmNamePrefix": "orders-api-",
        },
    )
    stubs["cloudwatch"].add_response(
        "list_tags_for_resource",
        {"Tags": [{"Key": "env", "Value": "staging"}]},
        {"ResourceARN": other["AlarmArn"]},
    )
    stubs["cloudwatch"].add_response(
        "list_tags_for_resource",
        {"Tags": [{"Key": "env", "Value": "production"}]},
        {"ResourceARN": composite["AlarmArn"]},
    )
    out = src.fetch("get_alert", alert(), "production")
    assert [(a["alert_name"], a["rule"]) for a in out.records] == [
        ("orders-api-composite", 'composite: ALARM("orders-5xx") OR ALARM("orders-latency")')
    ]
    assert out.records[0]["fired_at"] == "2026-11-19T10:05:00Z"
    assert out.partial  # DescribeAlarms had another page


def segment(name, start, *, fault, env="production", subsegments=(), cause=None, url=None):
    doc = {
        "id": "70de5b6f19ff9a0a",
        "name": name,
        "trace_id": "1-6a3c8f00-4bf92f3577b34da6a3ce929d",
        "start_time": start,
        "end_time": start + 0.012,
        "fault": fault,
        "annotations": {"environment": env, "version": "1.4.0"},
        "subsegments": list(subsegments),
    }
    if url:
        doc["http"] = {"request": {"method": "POST", "url": url}, "response": {"status": 500}}
    if cause:
        doc["cause"] = cause
    return {"Id": "70de5b6f19ff9a0a", "Document": json.dumps(doc)}


def test_xray_faulted_traces_of_this_service_as_spans(aws):
    src, stubs = aws()
    t = T0.timestamp() + 65
    cause = {
        "working_directory": "/app",
        "exceptions": [
            {
                "id": "e1",
                "type": "KeyError",
                "message": "'TAX_RATE'",
                "stack": [{"path": "orders/service.py", "line": 88, "label": "place_order"}],
            }
        ],
    }
    sub = {
        "id": "a1",
        "name": "orders.service.place_order",
        "start_time": t + 0.001,
        "end_time": t + 0.01,
        "fault": True,
        "cause": cause,
        "annotations": {"app.config.read": "TAX_RATE"},
    }
    stubs["xray"].add_response(
        "get_trace_summaries",
        {
            "TraceSummaries": [{"Id": "1-6a3c8f00-4bf92f3577b34da6a3ce929d", "HasFault": True}],
            "ApproximateTime": T0,
            "TracesProcessedCount": 40,
        },
        {
            "StartTime": WINDOW["start"],
            "EndTime": WINDOW["end"],
            "Sampling": False,
            "FilterExpression": 'service("orders-api") { fault = true }',
        },
    )
    stubs["xray"].add_response(
        "batch_get_traces",
        {
            "Traces": [
                {
                    "Id": "1-6a3c8f00-4bf92f3577b34da6a3ce929d",
                    "Duration": 0.012,
                    "Segments": [
                        segment(
                            "orders-api",
                            t,
                            fault=True,
                            subsegments=[sub],
                            url="https://orders.example/checkout",
                        ),
                        segment(
                            "payments-api",
                            t,
                            fault=True,
                            cause={
                                "exceptions": [
                                    {"id": "x", "type": "Other", "message": "someone else"}
                                ]
                            },
                        ),  # fmt: skip
                        segment("orders-api", t + 1, fault=True, env="staging"),
                    ],
                }
            ],
            "UnprocessedTraceIds": [],
        },
        {"TraceIds": ["1-6a3c8f00-4bf92f3577b34da6a3ce929d"]},
    )
    out = src.fetch("get_traces", traces(), "production")
    spans = out.records[0]["spans"]
    assert [(s["name"], s["status"]) for s in spans] == [
        ("POST /checkout", "ERROR"),
        ("orders.service.place_order", "ERROR"),
    ]
    assert spans[0]["start"] == "2026-11-19T10:01:05.000Z"
    assert spans[0]["attributes"]["http.response.status_code"] == 500
    assert spans[1]["exceptions"] == [{"type": "KeyError", "message": "'TAX_RATE'"}]
    assert spans[1]["attributes"]["code.filepath"] == "orders/service.py"
    assert "someone else" not in str(out.records) and not out.partial
    facts = facts_for(Observed(EvidenceKind.trace, {}, out.records), [CHANGE])
    assert facts.changed_key_on_failing_path and facts.frames_in_changed_file


def test_xray_trace_cap_and_errors_are_gaps(aws):
    src, stubs = aws(max_traces=1)
    ids = [f"1-6a3c8f00-{i:024x}" for i in range(3)]
    stubs["xray"].add_response(
        "get_trace_summaries", {"TraceSummaries": [{"Id": i} for i in ids], "NextToken": "n"}
    )
    stubs["xray"].add_response("batch_get_traces", {"Traces": []}, {"TraceIds": ids[:1]})
    out = src.fetch("get_traces", traces(), "production")
    assert out.records == [] and out.partial
    stubs["xray"].add_client_error("get_trace_summaries", "ThrottledException", "slow down")
    with pytest.raises(SourceUnavailable, match="get_trace_summaries: ThrottledException"):
        src.fetch("get_traces", traces(), "production")


def insights_row(**fields):
    return [{"field": k, "value": str(v)} for k, v in fields.items()]


STACK = 'Traceback:\n  File "orders/api.py", line 12, in post\n  File "orders/service.py", line 88, in place_order\nKeyError: \'TAX_RATE\''


def test_logs_insights_error_groups_derived_from_a_stats_query(aws):
    src, stubs = aws()
    stubs["logs"].add_response(
        "start_query",
        {"queryId": "q-1"},
        {
            "logGroupNames": ["/ecs/orders-api-production"],
            "startTime": int(WINDOW["start"].timestamp()),
            "endTime": int(WINDOW["end"].timestamp()),
            "queryString": CW["error_groups_query"].replace("$service", "orders-api"),
            "limit": 2000,
        },
    )
    stubs["logs"].add_response("get_query_results", {"status": "Running", "results": []})
    stubs["logs"].add_response(
        "get_query_results",
        {
            "status": "Complete",
            "results": [
                insights_row(
                    type="KeyError",
                    message="'TAX_RATE'",
                    version="1.4.0",
                    count=40,
                    first_seen="2026-11-19 10:01:05.000",
                    last_seen="2026-11-19 10:20:00.000",
                    stack=STACK,
                ),
                insights_row(
                    type="KeyError",
                    message="'TAX_RATE'",
                    version="1.3.2",
                    count=2,
                    first_seen="2026-11-19 10:02:00.000",
                    last_seen="2026-11-19 10:03:00.000",
                ),
                insights_row(
                    type="TimeoutError",
                    message="upstream",
                    count=1,
                    first_seen="2026-11-19 09:58:00.000",
                    last_seen="2026-11-19 09:58:00.000",
                ),
            ],  # fmt: skip
            "statistics": {"recordsMatched": 43.0, "recordsScanned": 9000.0, "bytesScanned": 1e6},
        },
        {"queryId": "q-1"},
    )
    out = src.fetch("get_error_groups", groups(), "production")
    assert out.records == [
        {
            "type": "TimeoutError",
            "message": "upstream",
            "count": 1,
            "first_seen": "2026-11-19T09:58:00Z",
            "last_seen": "2026-11-19T09:58:00Z",
            "versions": [],
            "frames": [],
        },
        {
            "type": "KeyError",
            "message": "'TAX_RATE'",
            "count": 42,
            "first_seen": "2026-11-19T10:01:05Z",
            "last_seen": "2026-11-19T10:20:00Z",
            "versions": ["1.3.2", "1.4.0"],
            "frames": [["orders/api.py", "12"], ["orders/service.py", "88"]],
        },
    ]
    assert not out.partial
    facts = facts_for(Observed(EvidenceKind.error_group, {}, out.records[1:]), [CHANGE])
    assert facts.frames_in_changed_file and facts.first_seen_after_change
    stubs["logs"].assert_no_pending_responses()


def test_logs_insights_failure_and_a_query_past_its_time_are_gaps(aws, monkeypatch):
    import itertools
    from types import SimpleNamespace

    from aisre.evidence import cloudwatch

    clock = itertools.count()  # each reading a second later; no real sleeping
    monkeypatch.setattr(
        cloudwatch, "time", SimpleNamespace(monotonic=lambda: next(clock), sleep=lambda s: None)
    )
    src, stubs = aws(timeout_s=1)
    stubs["logs"].add_response("start_query", {"queryId": "q-2"})
    stubs["logs"].add_response("get_query_results", {"status": "Failed", "results": []})
    with pytest.raises(SourceUnavailable, match="Logs Insights query: Failed"):
        src.fetch("get_error_groups", groups(), "production")
    stubs["logs"].add_response("start_query", {"queryId": "q-3"})
    stubs["logs"].add_response("get_query_results", {"status": "Running", "results": []})
    with pytest.raises(SourceUnavailable, match="still Running after 1 s"):
        src.fetch("get_error_groups", groups(), "production")
    stubs["logs"].add_client_error("start_query", "MalformedQueryException", "bad")
    with pytest.raises(SourceUnavailable, match="start_query: MalformedQueryException"):
        src.fetch("get_error_groups", groups(), "production")


def test_cloudwatch_dependency_health_from_get_metric_data(aws):
    src, stubs = aws()
    stubs["cloudwatch"].add_response(
        "get_metric_data",
        {
            "MetricDataResults": [
                {
                    "Id": "d0_error_rate",
                    "Label": "e",
                    "Timestamps": [AT],
                    "Values": [0.01],
                    "StatusCode": "Complete",
                },
                {
                    "Id": "d0_latency_p95",
                    "Label": "l",
                    "Timestamps": [AT],
                    "Values": [0.02],
                    "StatusCode": "Complete",
                },
                {
                    "Id": "d1_error_rate",
                    "Label": "e",
                    "Timestamps": [],
                    "Values": [],
                    "StatusCode": "Complete",
                },
            ],  # fmt: skip
        },
        {
            "MetricDataQueries": [
                {
                    "Id": "d0_error_rate",
                    "Expression": CW["dependencies"]["orders-db"]["error_rate"]
                    .replace("$service", "orders-api")
                    .replace("$dependency", "orders-db"),
                    "Period": 60,
                    "ReturnData": True,
                },
                {
                    "Id": "d0_latency_p95",
                    "Expression": CW["dependencies"]["orders-db"]["latency_p95"]
                    .replace("$service", "orders-api")
                    .replace("$dependency", "orders-db"),
                    "Period": 60,
                    "ReturnData": True,
                },
                {
                    "Id": "d1_error_rate",
                    "Expression": "SUM(SEARCH('Dependency=\"payment-gateway\" Errors', 'Average', 60))",
                    "Period": 60,
                    "ReturnData": True,
                },
            ],
            "StartTime": AT - timedelta(minutes=5),
            "EndTime": AT,
            "ScanBy": "TimestampDescending",
        },
    )
    out = src.fetch("get_dependency_health", deps(), "production")
    assert [(d["name"], d["status"]) for d in out.records] == [
        ("orders-db", "healthy"),
        ("payment-gateway", "unknown"),
    ]
    assert out.partial


def test_cloudwatch_dependency_errors_and_unserved_tools_are_gaps(aws):
    src, stubs = aws()
    stubs["cloudwatch"].add_client_error("get_metric_data", "InvalidParameterValue", "bad")
    with pytest.raises(SourceUnavailable, match="get_metric_data: InvalidParameterValue"):
        src.fetch("get_dependency_health", deps(), "production")
    two = {"Timestamps": [AT], "Values": [0.1], "StatusCode": "Complete"}
    stubs["cloudwatch"].add_response(
        "get_metric_data",
        {"MetricDataResults": [{"Id": "d0_error_rate", **two}, {"Id": "d0_error_rate", **two}]},
    )
    with pytest.raises(SourceUnavailable, match="aggregate it"):
        src.fetch("get_dependency_health", deps(), "production")
    with pytest.raises(SourceUnavailable, match="not plain"):
        src.fetch("get_alert", alert("orders api"), "production")
    with pytest.raises(SourceUnavailable, match="has no get_changes"):
        src.fetch("get_changes", traces(), "production")


@pytest.mark.parametrize(
    "extra, error",
    [
        ({"alarm_dimensions": {"Env": "$environment"}}, "select the service's alarms"),
        ({"traces_filter": "fault = true"}, "traces_filter"),
        ({"error_groups_query": "fields @message"}, "stats"),
        ({"max_traces": 6}, "max_traces"),
        ({"dependency_lookback_s": 7200}, "dependency_lookback_s"),
    ],
)
def test_bad_cloudwatch_configuration_is_refused_at_start(extra, error):
    pytest.importorskip("boto3")
    with pytest.raises(ValueError, match=error):
        SourceSet.from_config({"sources": {"*": CW | extra}})


def test_each_adapter_says_which_tools_its_configuration_answers():
    """aisre pilot check reads `serves()`, so a `"*"` that would only ever be a gap for a tool
    (CloudWatch's or Prometheus's get_changes) fails the check instead of passing it."""
    pytest.importorskip("boto3")
    dd = {"adapter": "datadog", "api_key": {"env": "A"}, "app_key": {"env": "B"}}
    cases = [
        ({"adapter": "prometheus", "url": "https://p", "metrics": {"http_request_rate": {"value": "q"}}}, {"get_metric"}),
        (PROM_DEPS, {"get_dependency_health"}),
        ({"adapter": "loki", "url": "https://l", "query": '{service="$service"}'}, {"get_logs"}),
        (LOKI_GROUPS, {"get_error_groups"}),
        (AM, {"get_alert"}),
        (TEMPO, {"get_traces"}),
        (dd, {"get_changes", "get_alert", "get_traces", "get_error_groups"}),
        (DD | {"logs_query": "service:$service"}, set(sources.SOURCE_TOOLS) - {"get_metric"}),
        ({"adapter": "cloudwatch", "region": "eu-west-1"}, {"get_alert", "get_traces"}),
        (CW, {"get_alert", "get_traces", "get_logs", "get_error_groups", "get_dependency_health"}),
    ]  # fmt: skip
    for spec, tools in cases:
        src = SourceSet.from_config({"sources": {"*": spec}}).for_tool("get_alert")
        assert src.serves() == tools, spec["adapter"]
