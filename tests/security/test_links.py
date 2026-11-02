"""ST-29, ST-30: nothing fetches a link, and tool arguments can't aim at internal hosts."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from aisre.gateway.adapters import render_context
from aisre.agent.agent_contracts import DiagnosisDraft
from support import draft_json
from aisre.agent.tool_registry import REGISTRY, ToolGate, ToolRefused
from aisre.agent.validation import UnsafeOutput, check_output

NOW = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
METADATA = "http://169.254.169.254/latest/meta-data/"
INTERNAL = "http://postgres.internal:5432/"


def gate():
    return ToolGate("inc-1", "checkout", NOW, NOW + timedelta(minutes=30))


def window(**over):
    return {"service": "checkout", "start": (NOW - timedelta(hours=1)).isoformat(), "end": NOW.isoformat(), **over}


def link_in_summary(text):
    with pytest.raises(UnsafeOutput) as e:
        check_output(DiagnosisDraft.model_validate(draft_json(summary=f"See {text} for details")))
    assert e.value.code == "link"


# ---------------------------------------------------------------- ST-29
@pytest.mark.parametrize("link", ["http://canary.example/x", "https://canary.example/x", "HTTP://CANARY.EXAMPLE/x",
                                  "ftp://canary.example/x", "file:///etc/passwd", "www.canary.example", METADATA])
def test_st29_a_link_in_model_output_fails_validation(link):
    link_in_summary(link)


@pytest.mark.parametrize("link", ["//canary.example/x", "canary.example.com/collect?d=1", "data:text/html;base64,PHNjcmlwdD4=",
                                  "javascript:alert(1)"])
def test_st29_other_link_forms_fail_validation(link):
    link_in_summary(link)


@pytest.mark.parametrize("text", ["Revert config/checkout/payments.yaml to its old value", "The data: rows were missing",
                                  "payments.timeout_ms changed to 0", "a/b ratio dropped"])
def test_st29_control_ordinary_text_is_not_a_link(text):
    check_output(DiagnosisDraft.model_validate(draft_json(summary=text)))


def test_st29_no_tool_can_fetch_a_link():
    assert not [n for n in REGISTRY if any(w in n for w in ("fetch", "http", "url", "browse", "download"))]
    params = {f for t in REGISTRY.values() for f in t.params.model_fields}
    assert not params & {"url", "uri", "link", "href", "host", "endpoint"}


@pytest.mark.parametrize("tool", ["fetch_url", "http_get", "browse"])
def test_st29_asking_to_fetch_is_refused(tool):
    with pytest.raises(ToolRefused) as e:
        gate().check(tool, {"url": "http://canary.example/x"}, "collect_evidence")
    assert e.value.code == "unknown_tool"


def test_st29_a_link_in_a_log_line_stays_text_inside_the_data_block():
    rendered = render_context([{"ref": "evidence:1", "kind": "log", "text": "see http://canary.example/x"}])
    assert '<data ref="evidence:1" kind="log">\nsee http://canary.example/x\n</data>' in rendered


def test_st29_canary_server_needs_a_running_system():
    pytest.skip("Showing no request reaches a canary needs a deployed investigation and a canary server; the "
                "reference modules hold no HTTP client to observe")


# ---------------------------------------------------------------- ST-30
@pytest.mark.parametrize("name", ["url", "host", "endpoint"])
def test_st30_an_address_as_its_own_argument_is_rejected_by_the_schema(name):
    with pytest.raises(ToolRefused) as e:
        gate().check("get_logs", window(**{name: METADATA}), "collect_evidence")
    assert e.value.code == "invalid_params"


@pytest.mark.parametrize("field,value", [("service", METADATA), ("service", "postgres.internal")])
def test_st30_an_address_as_the_service_is_out_of_scope(field, value):
    with pytest.raises(ToolRefused) as e:
        gate().check("get_logs", window(**{field: value}), "collect_evidence")
    assert e.value.code == "out_of_scope"


def test_st30_metric_names_are_a_closed_list():
    with pytest.raises(ToolRefused) as e:
        gate().check("get_metric", window(metric=METADATA), "collect_evidence")
    assert e.value.code == "invalid_params"


@pytest.mark.parametrize("tool,params", [
    ("get_traces", window(path=METADATA)),
    ("get_logs", window(contains=METADATA)),
    ("get_error_groups", window(release=INTERNAL)),
    ("get_alert", {"service": "checkout", "alert_id": METADATA}),
    ("search_runbooks", {"service": "checkout", "query": INTERNAL}),
], ids=["path", "contains", "release", "alert_id", "query"])
def test_st30_an_address_inside_a_query_parameter_is_rejected_by_the_schema(tool, params):
    with pytest.raises(ToolRefused) as e:
        gate().check(tool, params, "collect_evidence")
    assert e.value.code == "invalid_params"


@pytest.mark.parametrize("value", ["169.254.169.254", "http%3A%2F%2F169.254.169.254%2F", "postgres.internal", "redis.svc",
                                   "localhost:5432", "metadata.google.internal", "[fd00::1]", "//10.0.0.1/admin", "0xA9FEA9FE"])
def test_st30_other_spellings_of_an_address_are_rejected(value):
    with pytest.raises(ToolRefused) as e:
        gate().check("get_logs", window(contains=value), "collect_evidence")
    assert e.value.code == "invalid_params"


@pytest.mark.parametrize("tool,params", [
    ("get_traces", window(path="/v1/pay")), ("get_logs", window(contains="payment failed: timeout_ms=0")),
    ("get_logs", window(contains="std::string [error] [abc]")), ("get_error_groups", window(release="v2.1.3")),
    ("get_alert", {"service": "checkout", "alert_id": "alert-123"}), ("search_runbooks", {"service": "checkout", "query": "5xx after deploy"}),
])
def test_st30_control_ordinary_query_terms_pass(tool, params):
    gate().check(tool, params, "collect_evidence")


def test_st30_adapter_making_no_call_needs_the_evidence_adapters():
    pytest.skip("Evidence adapters don't exist (collect_evidence is NotImplementedError), so no-call and attempt logging "
                "can't be observed")
