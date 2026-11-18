"""Artifact 13.5 T1.9 (13.7, Q13.27): the live contract test for each pinned Claude model.

`check_contract` sends what the platform sends, through ClaudeAdapter and AnthropicTransport,
and asserts the provider accepts it and answers in the shape the adapter reads: structured
output, usage, the pinned model echoed, a request ID, truncation at the output limit, a tool
proposal, and a bad key refused as permanent. It runs live once per pinned model (`paid`),
and is the check that the request rules in Artifact 10.1 still hold: an adapter that breaks
one (for example by sending `temperature`) gets a 400 and fails it.

Paid runs need both AISRE_PAID_TESTS=1 and AISRE_TEST_PROVIDER_KEY (13.7); without them they
skip. The key is never printed. `ANTHROPIC_API_KEY` is not read: the session guard
(tests/conftest.py) stops any run where it is set.

The unmarked tests below run the same function against tests/fake_anthropic.py, which enforces
the same rules offline, to prove the failure path: a rule broken on purpose fails the contract.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import Any

import pytest
from fake_anthropic import FakeAnthropic
from pydantic import BaseModel, SecretStr

from aisre.gateway.adapters import CLAUDE_MODEL_IDS, ClaudeAdapter
from aisre.gateway.gateway import CLAUDE_PRICES, BudgetLedger, ModelGateway
from aisre.gateway.gateway_contracts import (
    ModelRequest,
    ResultStatus,
    StopReason,
    ToolProposal,
    ToolSpec,
)
from aisre.gateway.transport import AnthropicTransport

PAID = os.environ.get("AISRE_PAID_TESTS") == "1" and bool(os.environ.get("AISRE_TEST_PROVIDER_KEY"))
TASK_FOR_ALIAS = {"default": "diagnose", "standard": "hypothesise"}


class Answer(BaseModel):
    answer: str


ANSWER_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}
LOGS_TOOL = ToolSpec(
    name="get_logs",
    description="Fetch recent error logs for one service.",
    input_schema={
        "type": "object",
        "properties": {"service": {"type": "string"}},
        "required": ["service"],
        "additionalProperties": False,
    },
)


def request(alias: str, **kw: Any) -> ModelRequest:
    base: dict[str, Any] = dict(
        request_id=f"contract:{uuid.uuid4()}",
        incident_id="contract",
        task=TASK_FOR_ALIAS[alias],
        prompt_id="contract",
        prompt_version="contract@1",
        system="Answer the question in the data block. Reply with the JSON object only.",
        context=[{"ref": "fact:question", "kind": "fact", "text": "What is 2 + 2?"}],
        output_schema=ANSWER_SCHEMA,
        output_schema_name="Answer",
        max_output_tokens=2048,
        deadline_epoch_s=time.time() + 300,
        model_alias=alias,
    )
    return ModelRequest(**(base | kw))


async def check_contract(
    key: SecretStr, alias: str, adapter: ClaudeAdapter, base_url: str | None = None
) -> None:
    """Every assertion here is a request rule or a response shape the platform depends on."""
    model = adapter.model_ids[alias]
    transport = AnthropicTransport(key, base_url=base_url)
    try:
        # Structured output through the whole gateway: accepted, valid, usage and identity.
        gw = ModelGateway(adapter, transport, BudgetLedger(), CLAUDE_PRICES, {"Answer": Answer})
        r = await gw.call(request(alias))
        assert r.status == ResultStatus.OK, f"structured call: {r.status.value}: {r.error}"
        assert r.parsed is not None and r.usage.reported and r.usage.output_tokens > 0
        assert r.identity is not None and r.identity.model == model, "pinned model not echoed"
        assert r.provider_request_id, "no request-id header"

        # Truncation at the output limit is reported as such, with usage.
        small = request(alias, output_schema=None, output_schema_name=None, max_output_tokens=16)
        resp, done = await transport("anthropic", adapter.build(small), 60.0)
        t = adapter.parse(small, 1, resp, 0, done)
        assert resp.status == 200, f"truncation call: HTTP {resp.status}: {t.error}"
        assert t.stop_reason == StopReason.MAX_TOKENS and t.usage.reported

        # A tool proposal comes back as a proposal, with arguments matching the strict schema.
        ask = request(
            alias,
            system="Call the get_logs tool for the service named in the data block.",
            context=[{"ref": "fact:service", "kind": "fact", "text": "service: checkout"}],
            output_schema=None,
            output_schema_name=None,
            tools=[LOGS_TOOL],
        )
        resp, done = await transport("anthropic", adapter.build(ask), 60.0)
        p = adapter.parse(ask, 1, resp, 0, done)
        assert resp.status == 200, f"tool call: HTTP {resp.status}: {p.error}"
        proposals = [b for b in p.content if isinstance(b, ToolProposal)]
        assert p.stop_reason == StopReason.TOOL_USE and proposals, "no tool proposal"
        assert proposals[0].tool == "get_logs" and set(proposals[0].arguments) == {"service"}
    finally:
        await transport.close()

    # A bad key is refused as permanent, so the gateway never retries it.
    bad = AnthropicTransport(SecretStr("invalid-key-for-contract-test"), base_url=base_url)
    try:
        resp, done = await bad("anthropic", adapter.build(request(alias)), 60.0)
        b = adapter.parse(request(alias), 1, resp, 0, done)
        assert b.status == ResultStatus.AUTH_ERROR, f"bad key gave {b.status.value}"
    finally:
        await bad.close()


# ---------------------------------------------------------------- live, per pinned model
@pytest.mark.paid
@pytest.mark.skipif(not PAID, reason="paid: needs AISRE_PAID_TESTS=1 and AISRE_TEST_PROVIDER_KEY")
@pytest.mark.parametrize("alias", sorted(CLAUDE_MODEL_IDS))
def test_live_contract_for_each_pinned_model(alias):
    key = SecretStr(os.environ["AISRE_TEST_PROVIDER_KEY"])
    asyncio.run(check_contract(key, alias, ClaudeAdapter()))


# ---------------------------------------------------------------- the failure path, offline
KEY = "fake-contract-key"


def fake_replies(server: FakeAnthropic, model: str) -> None:
    server.script.extend(
        [
            server.message(model, json.dumps({"answer": "4"})),
            server.message(model, "Four", stop="max_tokens"),
            {
                **server.message(model, "", stop="tool_use"),
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "get_logs",
                        "input": {"service": "checkout"},
                    }
                ],
            },
        ]
    )


class SendsTemperature(ClaudeAdapter):
    def build(self, req: ModelRequest) -> dict[str, Any]:
        return {**super().build(req), "temperature": 0.0}


class DisablesThinking(ClaudeAdapter):
    def build(self, req: ModelRequest) -> dict[str, Any]:
        return {**super().build(req), "thinking": {"type": "disabled"}}


class ForcesTool(ClaudeAdapter):
    def build(self, req: ModelRequest) -> dict[str, Any]:
        return {**super().build(req), "tool_choice": {"type": "any"}}


@pytest.mark.parametrize("alias", sorted(CLAUDE_MODEL_IDS))
def test_the_contract_passes_when_every_rule_is_kept(alias):
    with FakeAnthropic(KEY) as server:
        fake_replies(server, CLAUDE_MODEL_IDS[alias])
        asyncio.run(check_contract(SecretStr(KEY), alias, ClaudeAdapter(), server.url))


@pytest.mark.parametrize("broken", [SendsTemperature, DisablesThinking, ForcesTool])
@pytest.mark.parametrize("alias", sorted(CLAUDE_MODEL_IDS))
def test_a_rule_broken_on_purpose_fails_the_contract(alias, broken):
    with FakeAnthropic(KEY) as server:
        fake_replies(server, CLAUDE_MODEL_IDS[alias])
        with pytest.raises(AssertionError, match="bad_request"):
            asyncio.run(check_contract(SecretStr(KEY), alias, broken(), server.url))
        assert all(r[0].get("anthropic-version") == "2026-10-21" for r in server.requests)
