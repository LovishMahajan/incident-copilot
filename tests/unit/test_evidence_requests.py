"""Artifact 9.4, 9.6, 9.8: evidence requests a model can express under strict structured output.

The skills send `transform_schema(<contract>)` as `output_config.format` (reasoning.py). A free-form
dict reaches the model as an object with no properties, so it could only send `{}`. These tests
check the generated schema itself: it admits the reference incident's targeted requests, it stays
within the provider's limits for strict schemas, and the ToolGate still checks what comes back.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from anthropic import transform_schema
from pydantic import ValidationError

from aisre.agent.agent_contracts import (
    CriticVerdict,
    HypothesesOut,
    evidence_request,
)
from aisre.agent.skills.reasoning import SCHEMAS
from aisre.agent.tool_registry import ToolGate, ToolRefused

STARTED = datetime(2026, 10, 20, 14, 7, tzinfo=timezone.utc)  # breach_started_at, alert.json
NOW = datetime(2026, 10, 20, 14, 20, tzinfo=timezone.utc)
SERVICE = "orders-api"

# What an investigator of the reference incident (tests/fixtures/reference-incident/README.md)
# asks for after the baseline: the failing setting in the logs, the version split, the failing
# path, the new release's error group, the change itself, dependencies at the breach, a runbook.
REFERENCE_TARGETED = [
    {
        "tool": "get_logs",
        "reason": "errors that name the changed setting",
        "params": {
            "service": SERVICE,
            "start": "2026-10-20T13:55:00Z",
            "end": "2026-10-20T14:20:00Z",
            "level": "error",
            "contains": "ORDER_TAX_RATE_BPS",
        },
    },
    {
        "tool": "get_metric",
        "reason": "does only 2.14.1 fail",
        "params": {"service": SERVICE, "metric": "error_rate_by_version"},
    },
    {
        "tool": "get_traces",
        "reason": "a failing order request",
        "params": {"service": SERVICE, "path": "/v1/orders"},
    },
    {
        "tool": "get_error_groups",
        "reason": "errors new in the release",
        "params": {"service": SERVICE, "release": "2.14.1"},
    },
    {
        "tool": "get_changes",
        "reason": "what 2.14.1 changed",
        "params": {"service": SERVICE, "start": "2026-10-20T13:00:00Z"},
    },
    {
        "tool": "get_dependency_health",
        "reason": "rule out the database",
        "params": {"service": SERVICE, "at": "2026-10-20T14:07:00Z"},
    },
    {
        "tool": "search_runbooks",
        "reason": "the service's runbook for HTTP 500s",
        "params": {"service": SERVICE, "query": "orders-api HTTP 500 after a config change"},
    },
]


def conforms(value: Any, schema: dict[str, Any], defs: dict[str, Any]) -> bool:
    """The subset of JSON Schema that strict structured output uses (Claude API docs)."""
    if "$ref" in schema:
        return conforms(value, defs[schema["$ref"].rsplit("/", 1)[-1]], defs)
    if "anyOf" in schema:
        return any(conforms(value, s, defs) for s in schema["anyOf"])
    if "const" in schema:
        return value == schema["const"]
    if "enum" in schema:
        return value in schema["enum"]
    kind = schema.get("type")
    if kind == "object":
        props = schema.get("properties", {})
        assert schema.get("additionalProperties") is False
        return (
            isinstance(value, dict)
            and set(schema.get("required", [])) <= set(value)
            and set(value) <= set(props)
            and all(conforms(v, props[k], defs) for k, v in value.items())
        )
    if kind == "array":
        return isinstance(value, list) and all(conforms(v, schema["items"], defs) for v in value)
    if kind == "string":
        if not isinstance(value, str):
            return False
        if schema.get("format") == "date-time":
            datetime.fromisoformat(value)
        return True
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    raise AssertionError(f"unexpected schema {schema}")


def critic_answer(requests: list[dict]) -> dict:
    return {
        "verdict": "revise",
        "issues": [],
        "evidence_requests": requests,
        "confidence_cap": "medium",
    }


def test_the_schema_sent_to_the_model_allows_the_reference_targeted_requests():
    schema = transform_schema(CriticVerdict)
    defs = schema["$defs"]
    for i in range(0, len(REFERENCE_TARGETED), 3):
        answer = critic_answer(REFERENCE_TARGETED[i : i + 3])
        assert conforms(answer, schema, defs), answer
        CriticVerdict.model_validate(answer)  # and the contract the gateway validates with


def test_the_schema_no_longer_reduces_params_to_an_empty_object():
    defs = transform_schema(HypothesesOut)["$defs"]
    params = [d for name, d in defs.items() if name.endswith("RequestParams")]
    assert len(params) == 8
    assert all(d["properties"] and "service" in d["required"] for d in params)
    assert not conforms(
        {"service": SERVICE, "password": "x"}, defs["LogRequestParams"], defs
    )  # still no free-form keys


def test_a_request_the_shape_cannot_express_fails_the_schema():
    defs = transform_schema(CriticVerdict)["$defs"]
    item = transform_schema(CriticVerdict)["properties"]["evidence_requests"]["items"]
    for bad in (
        {"tool": "apply_change", "reason": "fix", "params": {"service": SERVICE}},
        {"tool": "get_metric", "reason": "r", "params": {"service": SERVICE, "metric": "cpu"}},
        {"tool": "get_logs", "reason": "r", "params": {"service": SERVICE, "level": "debug"}},
    ):
        assert not conforms(bad, item, defs)


def count(schema: Any, acc: dict[str, int]) -> dict[str, int]:
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            required = set(schema.get("required", []))
            for key, prop in schema.get("properties", {}).items():
                acc["optional"] += key not in required
                union = "anyOf" in prop or "anyOf" in prop.get("items", {})
                acc["union"] += union or isinstance(prop.get("type"), list)
        for v in schema.values():
            count(v, acc)
    elif isinstance(schema, list):
        for v in schema:
            count(v, acc)
    return acc


@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_each_output_schema_is_within_the_strict_schema_limits(name):
    """Claude API structured outputs: at most 24 optional and 16 union-typed parameters across
    the strict schemas in one request. Each skill request carries one output schema, no tools."""
    used = count(transform_schema(SCHEMAS[name]), {"optional": 0, "union": 0})
    assert used["optional"] <= 24 and used["union"] <= 16, used


def test_the_gate_still_decides_on_what_the_model_sent():
    gate = ToolGate("inc-ref", SERVICE, STARTED, NOW)
    for raw in REFERENCE_TARGETED:
        r = evidence_request(**raw)
        gate.check(r.tool, r.model_dump()["params"], "critique")  # every one runs
    for params, code in (
        ({"service": "payments-api"}, "out_of_scope"),
        (
            {"service": SERVICE, "start": "2026-10-19T13:00:00Z", "end": "2026-10-19T14:00:00Z"},
            "window_too_old",
        ),
        ({"service": SERVICE, "end": "2026-10-20T14:30:00Z"}, "window_in_future"),
        ({"service": SERVICE, "start": "2026-10-20T14:00:00"}, "invalid_params"),  # no zone
        ({"service": SERVICE, "contains": "http://metadata.internal/x"}, "invalid_params"),
    ):
        r = evidence_request(tool="get_logs", reason="r", params=params)
        with pytest.raises(ToolRefused) as e:
            gate.check(r.tool, r.model_dump()["params"], "critique")
        assert e.value.code == code, params


def test_an_omitted_field_is_left_out_not_sent_as_null():
    r = evidence_request(tool="get_logs", reason="r", params={"service": SERVICE})
    assert r.model_dump()["params"] == {"service": SERVICE}
    assert r.model_dump(mode="json")["params"] == {"service": SERVICE}
    with pytest.raises(ValidationError):  # and the contract keeps its own bounds
        evidence_request(
            tool="get_logs", reason="r", params={"service": SERVICE, "contains": "x" * 201}
        )
