"""Artifact 10.3, 10.7 (T1.8): the HTTP transport behind ClaudeAdapter.

The gateway's Transport: (provider, body, timeout_s) -> (HttpResponse, stream_completed). The
adapter builds the Messages API body and parses the response; this module only carries it.

It uses the official Anthropic Python SDK (`anthropic` 2.x) as the HTTP client, as the
provider's guidance recommends for Python over hand-written HTTP. 2.0.0 is the first release
that sends API version `2026-10-21` by default, the version Artifact 10.1 names, so no version
header is set here. The SDK is held to the transport's job:

- `max_retries=0`. The gateway owns attempts, backoff and the circuit breaker (10.7, ADR-61);
  SDK retries would add attempts no reservation covers.
- The body goes through as the adapter built it. Fields the SDK's signature doesn't name go in
  `extra_body`, so the request on the wire is exactly the body; the API, not the SDK, rules on it.
- A response is returned as status, JSON body and headers (`with_raw_response`), so
  ClaudeAdapter.parse and classify_http_error see what the API sent, errors included.
- A timeout raises asyncio.TimeoutError, as the gateway expects. A connection failure returns
  CONNECTION_FAILED, a provider error the gateway may retry: usage is unknown, so it is charged
  in full and reconciled (ADR-61).

Calls are not streamed: per-call output is capped at 16,000 tokens (ADR-61) and each call at
60 s (NFR-32), inside the SDK's non-streaming limits, so `stream_completed` is always true.
The key is a SecretStr from the caller; it never appears in a repr, a log or an error here.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any, Optional

import anthropic
from anthropic.resources.messages import AsyncMessages
from pydantic import SecretStr

from aisre.gateway.adapters import HttpResponse

CONNECTION_FAILED = 599  # no HTTP status: the request may or may not have reached the provider
SDK_PARAMS = frozenset(inspect.signature(AsyncMessages.create).parameters) - {
    "self",
    "extra_headers",
    "extra_query",
    "extra_body",
    "timeout",
    "stream",
}


def _body(response: Any) -> dict[str, Any]:
    try:
        data = response.json()
    except (json.JSONDecodeError, ValueError):
        return {"error": {"type": "unparseable_response", "message": response.text[:200]}}
    return data if isinstance(data, dict) else {"error": {"type": "unexpected_response"}}


class AnthropicTransport:
    provider = "anthropic"

    def __init__(self, api_key: SecretStr, base_url: Optional[str] = None) -> None:
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key.get_secret_value(), base_url=base_url, max_retries=0
        )

    def __repr__(self) -> str:
        return f"AnthropicTransport(base_url={str(self._client.base_url)!r})"

    async def close(self) -> None:
        await self._client.close()

    async def __call__(
        self, provider: str, body: dict[str, Any], timeout: float
    ) -> tuple[HttpResponse, bool]:
        if provider != self.provider:
            raise ValueError(f"AnthropicTransport carries {self.provider}, not {provider}")
        params = {k: v for k, v in body.items() if k in SDK_PARAMS}
        extra = {k: v for k, v in body.items() if k not in SDK_PARAMS}
        client = self._client.with_options(timeout=timeout)
        try:
            raw = await client.messages.with_raw_response.create(**params, extra_body=extra or None)
        except anthropic.APITimeoutError:
            raise asyncio.TimeoutError() from None
        except anthropic.APIStatusError as e:
            return HttpResponse(e.status_code, _body(e.response), dict(e.response.headers)), True
        except anthropic.APIConnectionError as e:
            error = {"type": "connection_error", "message": type(e.__cause__ or e).__name__}
            return HttpResponse(CONNECTION_FAILED, {"error": error}, {}), True
        return HttpResponse(raw.status_code, _body(raw.http_response), dict(raw.headers)), True
