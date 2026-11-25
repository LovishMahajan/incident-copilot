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
