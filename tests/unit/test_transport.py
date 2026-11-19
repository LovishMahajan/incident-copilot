"""T1.8: the real HTTP transport, through the gateway and ClaudeAdapter, against a fake server.

Offline: tests/fake_anthropic.py serves the Messages API on 127.0.0.1 and enforces Artifact
10.1's request rules. Covers what the paid contract test checks live (13.7, Q13.27): a plain
request, structured output, usage, truncation, refusal, a bad key, errors that stop and errors
that retry, the 60 s ceiling (NFR-32) and at most 3 attempts per step (NFR-33).
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

import pytest
from fake_anthropic import FakeAnthropic, error
from pydantic import BaseModel, SecretStr

from aisre.gateway.adapters import CLAUDE_EFFORT, CLAUDE_MODEL_IDS, ClaudeAdapter
from aisre.gateway.gateway import CLAUDE_PRICES, BudgetLedger, GatewayConfig, ModelGateway
from aisre.gateway.gateway_contracts import TASK_MODEL_ALIAS, ModelRequest, ResultStatus, StopReason
from aisre.gateway.transport import CONNECTION_FAILED, AnthropicTransport

KEY = "test-key-not-real"


class Out(BaseModel):
    leading: str


GOOD = json.dumps({"leading": "config revert"})


def req(**kw) -> ModelRequest:
    base = dict(
        request_id=f"inc:{uuid.uuid4()}",
        incident_id="inc-1",
        task="diagnose",
        prompt_id="diagnose",
        prompt_version="diagnose@1",
        system="Diagnose.",
        context=[{"ref": "evidence:1", "kind": "log", "text": "ERROR timeout_ms=0"}],
        output_schema=Out.model_json_schema(),
        output_schema_name="Out",
        max_output_tokens=500,
        deadline_epoch_s=time.time() + 600,
    )
    return ModelRequest(**(base | kw))


async def nosleep(_):
    return None


def call(server: FakeAnthropic, request: ModelRequest, key: str = KEY):
    async def go():
        transport = AnthropicTransport(SecretStr(key), base_url=server.url)
        try:
            g = ModelGateway(
                ClaudeAdapter(),
                transport,
                BudgetLedger(),
                CLAUDE_PRICES,
                {"Out": Out},
                GatewayConfig(),
                sleep=nosleep,
            )
            return await g.call(request)
        finally:
            await transport.close()

    return asyncio.run(go())


@pytest.fixture
def server():
    with FakeAnthropic(KEY, default_text=GOOD) as s:
        yield s


def test_a_structured_call_round_trips_with_version_key_usage_and_request_id(server):
    r = call(server, req())
    assert r.status == ResultStatus.OK and r.parsed == {"leading": "config revert"}
    assert r.usage.input_tokens == 120 and r.usage.reported and r.cost_micros > 0
    assert r.provider_request_id == "req_fake_1" and r.identity.model == "claude-opus-5-5"
    [(headers, body)] = server.requests
    assert headers["anthropic-version"] == "2026-10-21"
    assert headers.get("authorization") == f"Bearer {KEY}" or headers.get("x-api-key") == KEY
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert not {"temperature", "top_p", "top_k"} & set(body)


@pytest.mark.parametrize("task", sorted(TASK_MODEL_ALIAS))
def test_each_task_goes_to_its_model_and_effort_on_the_wire(server, task):
    """ADR-69: model per task, through to the request the provider receives."""
    assert call(server, req(task=task)).status == ResultStatus.OK
    body = server.requests[0][1]
    alias = TASK_MODEL_ALIAS[task]
    assert body["model"] == CLAUDE_MODEL_IDS[alias]
    assert body["output_config"]["effort"] == CLAUDE_EFFORT[alias]


def test_the_wire_body_is_exactly_what_the_adapter_built(server):
    call(server, req())
    assert server.requests[0][1] == ClaudeAdapter().build(req())


def test_a_bad_key_is_permanent_and_not_retried(server):
    r = call(server, req(), key="wrong-key")
    assert r.status == ResultStatus.AUTH_ERROR and len(server.requests) == 1


@pytest.mark.parametrize(
    ("status", "kind", "expected"),
    [
        (400, "invalid_request_error", ResultStatus.BAD_REQUEST),
        (402, "billing_error", ResultStatus.AUTH_ERROR),
        (404, "not_found_error", ResultStatus.BAD_REQUEST),
        (413, "request_too_large", ResultStatus.BAD_REQUEST),
    ],
)
def test_permanent_errors_stop_after_one_request(server, status, kind, expected):
    server.script.append((status, error(kind, "no")))
    r = call(server, req())
    assert r.status == expected and len(server.requests) == 1 and kind in r.error


@pytest.mark.parametrize(
    ("status", "kind", "expected"),
    [
        (429, "rate_limit_error", ResultStatus.RATE_LIMITED),
        (500, "api_error", ResultStatus.PROVIDER_ERROR),
        (529, "overloaded_error", ResultStatus.PROVIDER_ERROR),
    ],
)
def test_retryable_errors_take_three_attempts_and_no_sdk_retries(server, status, kind, expected):
    """NFR-33: at most 3 attempts per step, each one request: the SDK doesn't retry."""
    server.script.extend([(status, error(kind, "busy"))] * 3)
    r = call(server, req())
    assert r.status == expected and r.attempt == 3 and len(server.requests) == 3


def test_a_retry_after_a_transient_error_succeeds(server):
    server.script.append((529, error("overloaded_error", "busy")))
    r = call(server, req())
    assert r.status == ResultStatus.OK and r.attempt == 2 and len(server.requests) == 2


def test_a_slow_provider_times_out_at_the_call_timeout(server):
    """NFR-32: no call runs past its timeout; usage is unknown, so it's charged in full."""
    server.script.extend([("sleep", 2.0, None)] * 3)
    started = time.monotonic()
    r = call(server, req(call_timeout_s=0.3))
    assert r.status == ResultStatus.TIMEOUT and not r.usage.reported and r.cost_micros > 0
    assert time.monotonic() - started < 1.9 * 3


def test_truncation_and_refusal_are_classified(server):
    server.script.append(FakeAnthropic.message("claude-opus-5-5", '{"lead', stop="max_tokens"))
    server.script.append(FakeAnthropic.message("claude-opus-5-5", "", stop="refusal"))
    r = call(server, req())
    assert r.status == ResultStatus.REFUSED and r.stop_reason == StopReason.REFUSAL
    assert [b["max_tokens"] for _, b in server.requests] == [500, 1000]  # one larger retry


def test_no_server_is_a_provider_error_never_an_exception():
    with FakeAnthropic(KEY) as s:
        url = s.url
    r = call(type("Gone", (), {"url": url, "requests": []})(), req())
    assert r.status == ResultStatus.PROVIDER_ERROR and "connection_error" in r.error
    assert CONNECTION_FAILED >= 500


def test_the_key_is_never_in_the_transport_repr():
    t = AnthropicTransport(SecretStr("sk-test-secret"), base_url="http://127.0.0.1:9")
    assert "sk-test-secret" not in repr(t) and "sk-test-secret" not in str(t)
    asyncio.run(t.close())


def test_the_transport_refuses_another_providers_call():
    t = AnthropicTransport(SecretStr(KEY), base_url="http://127.0.0.1:9")
    with pytest.raises(ValueError):
        asyncio.run(t("tinker", {}, 1.0))
    asyncio.run(t.close())
