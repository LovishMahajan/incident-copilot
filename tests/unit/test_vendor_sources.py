"""Artifact 9.4, 13.5 (M5, ADR-75): the real evidence adapters, against recorded responses.

Prometheus, Loki and Datadog answer from tests/fixtures/recorded/ (shaped after each vendor's
documented API, cited there and in each module); CloudWatch from botocore's Stubber, which checks
requests and responses against the AWS service model. None of this shows that an adapter works
against a real account: that is the pilot's first-day check (docs/onboarding.md).

What each test pins: the platform's record shape, the call's scope and window (another
service's series or lines are dropped, FR-44), the row cap and `partial`, failures as
SourceUnavailable (a gap, never an empty answer), credentials by reference only and never in an
error, and construction that reaches nothing (aisre pilot check builds every adapter).
"""

from __future__ import annotations

import urllib.request
from datetime import UTC, datetime, timedelta

import pytest
from recorded_http import Recorded, load

from aisre.agent.tool_registry import LogQuery, MetricQuery
from aisre.evidence import sources
from aisre.evidence.sources import SecretRef, SourceSet, SourceUnavailable

T0 = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)
WINDOW = {"start": T0 - timedelta(minutes=5), "end": T0 + timedelta(minutes=30)}
TOKEN = "glsa-not-a-real-token-1234"


@pytest.fixture
def recorded(monkeypatch):
    def use(name: str) -> Recorded:
        r = Recorded(load(name))
        monkeypatch.setattr(urllib.request, "urlopen", r)
        return r

    return use


def metric(name: str, **kw) -> MetricQuery:
    return MetricQuery(service="orders-api", metric=name, **WINDOW, **kw)


def logs(**kw) -> LogQuery:
    return LogQuery(service="orders-api", **WINDOW, **kw)


PROM_METRICS = {
    "http_5xx_rate": {
        "value": 'sum(rate(http_requests_total{service="$service",environment="$environment",'
        'status=~"5.."}[1m])) / sum(rate(http_requests_total{service="$service"}[1m]))',
        "requests": 'sum(increase(http_requests_total{service="$service"}[1m]))',
    },
    "error_rate_by_version": {
        "value": 'sum by (version) (rate(app_errors_ratio{service="$service"}[1m]))',
        "requests": 'sum by (version) (increase(http_requests_total{service="$service"}[1m]))',
    },
    "http_latency_p95": {"value": "histogram_quantile(0.95, latency_bucket)"},
}


def prometheus(**kw):
    spec = {"adapter": "prometheus", "url": "https://prometheus.example", "metrics": PROM_METRICS}
    return SourceSet.from_config({"sources": {"get_metric": spec | kw}}, production=True)


# ---------------------------------------------------------------------------
# Construction: configuration is checked, nothing is reached, secrets by reference only
# ---------------------------------------------------------------------------
def test_every_real_adapter_is_registered_and_none_is_local_only():
    for name in ("prometheus", "loki", "datadog", "cloudwatch"):
        assert name in sources.ADAPTERS and name not in sources.LOCAL_ONLY


def test_building_an_adapter_reaches_nothing_and_reads_no_secret(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("an adapter reached the network while being built")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.delenv("DD_API_KEY", raising=False)
    SourceSet.from_config(
        {
            "services": {
                "orders-api": {
                    "get_metric": {
                        "adapter": "datadog",
                        "api_key": {"env": "DD_API_KEY"},
                        "app_key": {"file": "/nonexistent/app-key"},
                        "metrics": {"http_request_rate": {"value": "sum:x{service:$service}"}},
                    },
                    "get_logs": {
                        "adapter": "loki",
                        "url": "https://loki.example",
                        "query": '{service="$service"}',
                        "token": {"env": "LOKI_TOKEN"},
                    },
                    "*": {"adapter": "cloudwatch", "region": "eu-west-1"},
                }
            }
        },
        production=True,
    )


@pytest.mark.parametrize(
    "credential",
    ["dd-literal-key", {"value": "x"}, {"env": ""}, {"env": "A", "file": "/b"}],
)
def test_a_credential_by_value_is_refused(credential):
    with pytest.raises(ValueError, match="by reference only"):
        SourceSet.from_config(
            {
                "sources": {
                    "*": {"adapter": "datadog", "api_key": credential, "app_key": {"env": "K"}}
                }
            }
        )


@pytest.mark.parametrize(
    "spec, error",
    [
        ({"adapter": "prometheus", "url": "ftp://x", "metrics": {}}, "http"),
        ({"adapter": "prometheus", "url": "https://x", "metrics": {"cpu": {"value": "q"}}}, "cpu"),
        ({"adapter": "loki", "url": "https://x", "query": '{job=~".+"}'}, "\\$service"),
        (
            {"adapter": "datadog", "api_key": {"env": "A"}, "app_key": {"env": "B"}, "site": "x"},
            "site",
        ),
        ({"adapter": "cloudwatch", "region": "mars"}, "region"),
    ],
)
def test_bad_configuration_is_refused_at_start(spec, error):
    with pytest.raises((ValueError, TypeError), match=error):
        SourceSet.from_config({"sources": {"*": spec}})


def test_a_missing_credential_is_a_gap_naming_the_reference_not_a_value(monkeypatch, recorded):
    recorded("prometheus")
    monkeypatch.delenv("PROM_TOKEN", raising=False)
    src = prometheus(token={"env": "PROM_TOKEN"}).for_tool("get_metric")
    with pytest.raises(SourceUnavailable, match=r"\(environment variable PROM_TOKEN\) is not set"):
        src.fetch("get_metric", metric("http_5xx_rate"), "production")


def test_secret_ref_reads_a_file_and_reports_presence(tmp_path):
    f = tmp_path / "token"
    f.write_text(TOKEN + "\n")
    ref = SecretRef({"file": str(f)}, "x")
    assert ref.value() == TOKEN and ref.present()
    assert not SecretRef({"env": "NOPE"}, "x").present({})


# ---------------------------------------------------------------------------
# Prometheus and Loki
# ---------------------------------------------------------------------------
def test_prometheus_5xx_rate_with_requests_in_the_platforms_shape(recorded, monkeypatch):
    r = recorded("prometheus")
    monkeypatch.setenv("PROM_TOKEN", TOKEN)
    src = prometheus(token={"env": "PROM_TOKEN"}).for_tool("get_metric")
    out = src.fetch("get_metric", metric("http_5xx_rate"), "production")
    assert out.records == [
        {"at": "2026-11-19T10:00:00Z", "value": 0.012, "requests": 450.0},
        {"at": "2026-11-19T10:01:00Z", "value": 0.18, "requests": 440.0},
        {"at": "2026-11-19T10:02:00Z", "value": 0.2, "requests": 430.0},
    ]
    assert out.source_system == "prometheus:metric" and not out.partial
    first = r.to("GET", "/api/v1/query_range")[0]
    assert first.headers["authorization"] == f"Bearer {TOKEN}"
    assert 'service="orders-api",environment="production"' in first.query
    assert "start=2026-11-19T09:55:00Z" in first.query and "step=60s" in first.query
    assert TOKEN not in out.source_link


def test_prometheus_by_version_drops_another_services_series_and_nan(recorded):
    recorded("prometheus")
    out = (
        prometheus()
        .for_tool("get_metric")
        .fetch("get_metric", metric("error_rate_by_version"), "production")
    )
    assert out.records == [
        {
            "at": "2026-11-19T10:00:00Z",
            "versions": {
                "1.3.2": {"rate": 0.0, "requests": 35.0},
                "1.4.0": {"rate": 0.21, "requests": 412.0},
            },
        },
        {
            "at": "2026-11-19T10:01:00Z",
            "versions": {
                "1.3.2": {"rate": None, "requests": 40.0},
                "1.4.0": {"rate": 0.24, "requests": 398.0},
            },
        },
    ]


def test_prometheus_row_cap_marks_partial(recorded):
    recorded("prometheus")
    out = (
        prometheus()
        .for_tool("get_metric")
        .fetch("get_metric", metric("http_5xx_rate", limit=2), "production")
    )
    assert len(out.records) == 2 and out.partial


def test_prometheus_errors_and_unconfigured_metrics_are_gaps(recorded):
    recorded("prometheus")
    src = prometheus().for_tool("get_metric")
    with pytest.raises(SourceUnavailable, match="prometheus: HTTP 400"):
        src.fetch("get_metric", metric("http_latency_p95"), "production")
    with pytest.raises(SourceUnavailable, match="no query configured"):
        src.fetch("get_metric", metric("http_request_rate"), "production")
    with pytest.raises(SourceUnavailable, match="has no get_traces"):
        src.fetch("get_traces", metric("http_5xx_rate"), "production")


def test_a_quote_in_a_value_cannot_change_a_promql_query(recorded):
    r = recorded("prometheus")
    src = prometheus().for_tool("get_metric")
    p = MetricQuery(service='x"}or vector(1)#', metric="http_5xx_rate", **WINDOW)
    src.fetch("get_metric", p, "production")
    assert 'service="x\\"}or vector(1)#"' in r.sent[0].query


def test_an_oversized_answer_is_a_gap(recorded):
    recorded("prometheus")
    src = prometheus(max_bytes=100).for_tool("get_metric")
    with pytest.raises(SourceUnavailable, match="over 100 bytes"):
        src.fetch("get_metric", metric("http_5xx_rate"), "production")


def loki(**kw):
    spec = {
        "adapter": "loki",
        "url": "https://loki.example",
        "query": '{service="$service",environment="$environment"} | json | level=~"$levels"',
        "tenant": "team-orders",
        "username": "123456",
        "token": {"env": "LOKI_TOKEN"},
    }
    return SourceSet.from_config({"sources": {"get_logs": spec | kw}}).for_tool("get_logs")


def test_loki_logs_in_the_platforms_shape_within_scope(recorded, monkeypatch):
    r = recorded("prometheus")
    monkeypatch.setenv("LOKI_TOKEN", TOKEN)
    out = loki().fetch("get_logs", logs(), "production")
    assert [(x["ts"], x["level"], x["message"]) for x in out.records] == [
        ("2026-11-19T10:00:05Z", "ERROR", "checkout failed"),
        ("2026-11-19T10:00:07Z", "WARNING", "retrying payment call"),
        ("2026-11-19T10:00:09Z", "ERROR", "plain text line without json"),
    ]
    first = out.records[0]
    assert first["error_type"] == "KeyError" and first["path"] == "POST /checkout"
    assert first["status"] == 500 and first["version"] == "1.4.0"
    sent = r.sent[0]
    assert sent.headers["x-scope-orgid"] == "team-orders"
    assert sent.headers["authorization"].startswith("Basic ")
    assert 'level=~"(?i)ERROR|WARN|WARNING"' in sent.query and "limit=200" in sent.query
    assert "someone else" not in str(out.records)


def test_loki_contains_is_a_quoted_line_filter_and_level_narrows(recorded, monkeypatch):
    r = recorded("prometheus")
    monkeypatch.setenv("LOKI_TOKEN", TOKEN)
    out = loki().fetch("get_logs", logs(level="warning", contains='pay"ment'), "production")
    assert [x["level"] for x in out.records] == ["WARNING"]
    assert '|= "pay\\"ment"' in r.sent[0].query


def test_loki_timeout_is_a_gap(recorded, monkeypatch):
    recorded("prometheus")
    monkeypatch.setenv("LOKI_TOKEN", TOKEN)
    src = loki(query='{service="$service"} |= "timeout"')
    with pytest.raises(SourceUnavailable, match="no answer in"):
        src.fetch("get_logs", logs(), "production")


# ---------------------------------------------------------------------------
# Datadog
# ---------------------------------------------------------------------------
DD = {
    "adapter": "datadog",
    "site": "datadoghq.eu",
    "api_key": {"env": "DD_API_KEY"},
    "app_key": {"env": "DD_APP_KEY"},
    "metrics": {
        "http_5xx_rate": {
            "value": "avg:http.5xx{service:$service,env:$environment}",
            "requests": "sum:http.requests{service:$service,env:$environment}.as_count()",
        },
        "error_rate_by_version": {
            "value": "sum:trace.http.request.errors{service:$service,env:$environment} by {version}",
            "requests": "sum:trace.http.request.hits{service:$service,env:$environment} by {version}",
        },
        "http_latency_p95": {"value": "p95:latency{service:$service}"},
    },
    "logs_query": "service:$service env:$environment status:$levels",
}


@pytest.fixture
def datadog(monkeypatch, recorded):
    monkeypatch.setenv("DD_API_KEY", "dd-api-not-real")
    monkeypatch.setenv("DD_APP_KEY", "dd-app-not-real")
    r = recorded("datadog")
    return SourceSet.from_config({"sources": {"*": DD}}).for_tool("get_metric"), r


def test_datadog_metric_with_keys_by_reference_on_the_site(datadog):
    src, r = datadog
    out = src.fetch("get_metric", metric("http_5xx_rate"), "production")
    assert out.records == [
        {"at": "2026-11-19T10:00:00Z", "value": 0.01, "requests": 450.0},
        {"at": "2026-11-19T10:01:00Z", "value": 0.2, "requests": 445.0},
    ]
    sent = r.sent[0]
    assert sent.url.startswith("https://api.datadoghq.eu/api/v1/query?")
    assert sent.headers["dd-api-key"] == "dd-api-not-real"
    assert sent.headers["dd-application-key"] == "dd-app-not-real"
    assert "service:orders-api,env:production" in sent.query
    assert "dd-api" not in out.source_link


def test_datadog_by_version_drops_another_environments_series(datadog):
    src, _ = datadog
    out = src.fetch("get_metric", metric("error_rate_by_version"), "production")
    assert out.records[0]["versions"] == {
        "1.3.2": {"rate": 0.0, "requests": 30.0},
        "1.4.0": {"rate": 0.22, "requests": 400.0},
    }
    assert "2.0.0" not in str(out.records)


def test_datadog_logs_search_is_a_post_that_reads_and_pages_partial(datadog):
    src, r = datadog
    out = src.fetch("get_logs", logs(), "production")
    assert [(x["level"], x["message"]) for x in out.records] == [
        ("ERROR", "checkout failed"),
        ("WARNING", "retrying payment call"),
    ]
    assert out.records[0]["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert out.partial  # the answer has a next page
    body = r.to("POST", "/api/v2/logs/events/search")[0].body
    assert body["filter"]["query"] == "service:orders-api env:production status:(error OR warn)"
    assert body["filter"]["from"] == "2026-11-19T09:55:00Z" and body["page"] == {"limit": 200}


def test_datadog_events_are_changes_of_this_service_only(datadog):
    src, r = datadog
    out = src.fetch("get_changes", logs(), "production")
    assert len(out.records) == 1
    change = out.records[0]
    assert change["version"] == "1.4.0" and change["previous_version"] == "1.3.2"
    assert change["stages"] == [{"step": "Deployed orders-api 1.4.0", "at": "2026-11-19T09:58:00Z"}]
    assert "tags=service:orders-api,env:production" in r.sent[0].query


def test_datadog_refusals_and_unsafe_values_are_gaps(datadog):
    src, r = datadog
    with pytest.raises(SourceUnavailable, match="datadog: HTTP 403"):
        src.fetch("get_metric", metric("http_latency_p95"), "production")
    bad = MetricQuery(service="orders-api OR *", metric="http_5xx_rate", **WINDOW)
    with pytest.raises(SourceUnavailable, match="plain tag value"):
        src.fetch("get_metric", bad, "production")
    with pytest.raises(SourceUnavailable, match="has no get_dependency_health"):
        src.fetch("get_dependency_health", logs(), "production")  # none configured


# ---------------------------------------------------------------------------
# CloudWatch, through botocore's Stubber
# ---------------------------------------------------------------------------
@pytest.fixture
def cloudwatch(monkeypatch):
    pytest.importorskip("boto3")
    from botocore.stub import Stubber

    for var in ("AWS_PROFILE", "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDNOTREAL")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "not-a-real-secret")
    spec = {
        "adapter": "cloudwatch",
        "region": "eu-west-1",
        "metrics": {
            "http_5xx_rate": {
                "value": "m1 / m2",
                "requests": "SUM(SEARCH('{App,service} service=\"$service\" Requests', 'Sum'))",
            },
            "error_rate_by_version": {
                "value": "SEARCH('{App,service,version} service=\"$service\" ErrorRate', 'Average')",
            },
        },
        "log_group": "/ecs/$service-$environment",
        "filter_pattern": '{ $.service = "$service" }',
    }
    src = SourceSet.from_config({"sources": {"*": spec}}, production=True).for_tool("get_logs")
    stubs = {s: Stubber(src.client(s)) for s in ("cloudwatch", "logs")}
    for s in stubs.values():
        s.activate()
    yield src, stubs
    for s in stubs.values():
        s.deactivate()


def test_cloudwatch_get_metric_data(cloudwatch):
    src, stubs = cloudwatch
    stubs["cloudwatch"].add_response(
        "get_metric_data",
        {
            "MetricDataResults": [
                {
                    "Id": "value",
                    "Label": "value",
                    "Timestamps": [T0, T0 + timedelta(minutes=1)],
                    "Values": [0.01, 0.2],
                    "StatusCode": "Complete",
                },
                {
                    "Id": "requests",
                    "Label": "requests",
                    "Timestamps": [T0, T0 + timedelta(minutes=1)],
                    "Values": [450.0, 445.0],
                    "StatusCode": "Complete",
                },
            ],
            "Messages": [],
        },
        {
            "MetricDataQueries": [
                {
                    "Id": "requests",
                    "Expression": "SUM(SEARCH('{App,service} service=\"orders-api\" Requests', 'Sum'))",
                    "Period": 60,
                    "ReturnData": True,
                },
                {"Id": "value", "Expression": "m1 / m2", "Period": 60, "ReturnData": True},
            ],
            "StartTime": WINDOW["start"],
            "EndTime": WINDOW["end"],
            "ScanBy": "TimestampAscending",
            "MaxDatapoints": 10000,
        },
    )
    out = src.fetch("get_metric", metric("http_5xx_rate"), "production")
    assert out.records == [
        {"at": "2026-11-19T10:00:00Z", "value": 0.01, "requests": 450.0},
        {"at": "2026-11-19T10:01:00Z", "value": 0.2, "requests": 445.0},
    ]
    assert not out.partial
    stubs["cloudwatch"].assert_no_pending_responses()


def test_cloudwatch_by_version_from_dynamic_labels_and_partial_data(cloudwatch):
    src, stubs = cloudwatch
    stubs["cloudwatch"].add_response(
        "get_metric_data",
        {
            "MetricDataResults": [
                {
                    "Id": "value",
                    "Label": "1.4.0",
                    "Timestamps": [T0],
                    "Values": [0.22],
                    "StatusCode": "PartialData",
                },
                {
                    "Id": "value",
                    "Label": "1.3.2",
                    "Timestamps": [T0],
                    "Values": [0.0],
                    "StatusCode": "Complete",
                },
            ]
        },
    )
    out = src.fetch("get_metric", metric("error_rate_by_version"), "production")
    assert out.records == [
        {
            "at": "2026-11-19T10:00:00Z",
            "versions": {
                "1.3.2": {"rate": 0.0, "requests": None},
                "1.4.0": {"rate": 0.22, "requests": None},
            },
        }
    ]
    assert out.partial


def test_cloudwatch_filter_log_events(cloudwatch):
    src, stubs = cloudwatch
    line = '{"level":"error","service":"orders-api","message":"checkout failed","error_type":"KeyError"}'
    stubs["logs"].add_response(
        "filter_log_events",
        {
            "events": [
                {
                    "logStreamName": "ecs/orders/abc",
                    "timestamp": int(T0.timestamp() * 1000) + 5000,
                    "message": line,
                    "ingestionTime": 1,
                    "eventId": "1",
                },
                {
                    "logStreamName": "ecs/orders/abc",
                    "timestamp": int(T0.timestamp() * 1000) + 6000,
                    "message": '{"level":"info","message":"ok"}',
                    "ingestionTime": 1,
                    "eventId": "2",
                },
                {
                    "logStreamName": "ecs/pay/x",
                    "timestamp": int(T0.timestamp() * 1000) + 7000,
                    "message": '{"level":"error","service":"payments-api"}',
                    "ingestionTime": 1,
                    "eventId": "3",
                },
            ],
            "nextToken": "more",
        },
        {
            "logGroupName": "/ecs/orders-api-production",
            "startTime": int(WINDOW["start"].timestamp() * 1000),
            "endTime": int(WINDOW["end"].timestamp() * 1000),
            "limit": 200,
            "filterPattern": '{ $.service = "orders-api" }',
        },
    )
    out = src.fetch("get_logs", logs(), "production")
    assert [(x["ts"], x["level"], x["error_type"], x["instance"]) for x in out.records] == [
        ("2026-11-19T10:00:05Z", "ERROR", "KeyError", "ecs/orders/abc")
    ]
    assert out.partial


def test_cloudwatch_errors_are_gaps(cloudwatch):
    src, stubs = cloudwatch
    stubs["logs"].add_client_error("filter_log_events", "AccessDeniedException", "no")
    with pytest.raises(SourceUnavailable, match="AccessDeniedException"):
        src.fetch("get_logs", logs(), "production")
    with pytest.raises(SourceUnavailable, match="not plain"):
        src.fetch("get_logs", LogQuery(service="a b", **WINDOW), "production")
    with pytest.raises(SourceUnavailable, match="has no get_changes"):
        src.fetch("get_changes", logs(), "production")


def test_cloudwatch_without_boto3_is_refused_at_start(monkeypatch):
    import importlib.util

    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda n, *a: None if n == "boto3" else real(n, *a)
    )
    with pytest.raises(ValueError, match="the `aws` extra"):
        SourceSet.from_config({"sources": {"*": {"adapter": "cloudwatch", "region": "eu-west-1"}}})


def test_the_onboarding_guides_examples_load_in_production():
    import json
    import re
    from pathlib import Path

    guide = (Path(__file__).parents[2] / "docs" / "onboarding.md").read_text()
    examples = [
        json.loads(b)
        for b in re.findall(r"```json\n(.*?)```", guide, re.S)
        if "<" not in b and '"services"' in b
    ]
    adapters = {
        a["adapter"]
        for e in examples
        for scope in e["services"].values()
        for tool, a in scope.items()
        if tool != "allowed_gaps"
    }
    assert adapters == {"prometheus", "loki", "alertmanager", "tempo", "datadog", "cloudwatch"}
    assert len(examples) == 3  # one per stack, each covering all seven source tools
    for example in examples:
        loaded = SourceSet.from_config(example, production=True)
        for tool in sources.SOURCE_TOOLS:
            src = loaded.for_tool(tool, "payments-api", "production")
            answered = src is not None and sources.answers(src, tool)
            allowed = loaded.allowed_gap(tool, "payments-api", "production")
            assert answered != (allowed is not None), (tool, example)
