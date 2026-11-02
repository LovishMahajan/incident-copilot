"""Artifact 10.2: Model Gateway contracts — one internal interface for every provider.

Skills build a ModelRequest; the gateway returns a ModelResult. Nothing outside the
gateway sees a provider's request or response format. Also home to the one token estimator
(ADR-61), shared by the gateway and context assembly.
"""

from __future__ import annotations

import math
from enum import Enum
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


MAX_INPUT_TOKENS_PER_CALL = 16_000             # ADR-61, enforced in the gateway
MAX_OUTPUT_TOKENS_PER_CALL = 16_000


def estimate_tokens(text: str) -> int:
    """The one pre-call estimate (ADR-61): about 3 characters per token (real text averages nearer 4).
    Used to reserve budget and to size context; the provider's reported usage settles it."""
    return math.ceil(len(text) / 3) + 16


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------
class ToolSpec(Strict):
    name: str
    description: str
    input_schema: dict[str, Any]


class Capability(str, Enum):
    STRUCTURED_OUTPUT = "structured_output"   # schema-constrained final answer
    TOOL_USE = "tool_use"
    STRICT_TOOLS = "strict_tools"             # schema-constrained tool inputs
    STREAMING = "streaming"
    USAGE_REPORTING = "usage_reporting"
    PINNED_VERSION = "pinned_version"         # a fixed, reproducible model identity


class ModelRequest(Strict):
    """Q10.13. Everything a call needs; nothing provider-specific."""

    request_id: str                           # idempotency key: incident:step:attempt
    incident_id: str
    task: Literal["hypothesise", "diagnose", "critique", "draft_patch"]
    prompt_id: str                            # e.g. "diagnose"
    prompt_version: str                       # e.g. "diagnose@7" (Q10.64)
    system: str                               # versioned instructions
    context: list[dict[str, Any]]             # assembled context items, each with its source ref (10.11)
    output_schema: Optional[dict[str, Any]] = None    # JSON schema the answer must match
    output_schema_name: Optional[str] = None          # Pydantic model name, for validation
    tools: list[ToolSpec] = Field(default_factory=list)
    required_capabilities: list[Capability] = Field(default_factory=list)
    max_output_tokens: int = Field(ge=1, le=MAX_OUTPUT_TOKENS_PER_CALL)
    temperature: Optional[float] = Field(default=None, ge=0.0, le=1.0)   # sent only to a provider that accepts it (10.1)
    deadline_epoch_s: float                    # absolute: the investigation's remaining window
    call_timeout_s: float = Field(default=60.0, gt=0, le=60.0)
    model_alias: str = "default"               # resolved to a pinned model by configuration
    step_attempts_used: int = Field(default=0, ge=0, le=2)   # attempts already spent on this step, when a skill
                                               # reassembles after CONTEXT_EXCEEDED (ADR-61)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------
class TextBlock(Strict):
    type: Literal["text"] = "text"
    text: str


class ToolProposal(Strict):
    """Q10.16. A request to run a tool. Never executed by the gateway: the ToolGate decides."""

    type: Literal["tool_proposal"] = "tool_proposal"
    proposal_id: str
    tool: str
    arguments: dict[str, Any]


class ReasoningBlock(Strict):
    """Provider reasoning, if returned. Stored for debugging; never shown as evidence."""

    type: Literal["reasoning"] = "reasoning"
    text: str


ContentBlock = Union[TextBlock, ToolProposal, ReasoningBlock]


class StopReason(str, Enum):
    """Q10.18. Normalised across providers. Values are the PostgreSQL enum model_stop_reason in
    schema.sql: change them together."""

    COMPLETE = "complete"                     # natural end
    TOOL_USE = "tool_use"                     # stopped to propose tools
    MAX_TOKENS = "max_tokens"                 # cut off by the output limit
    CONTEXT_EXCEEDED = "context_exceeded"     # input + output exceeded the model's window
    STOP_SEQUENCE = "stop_sequence"
    REFUSAL = "refusal"                       # provider declined (Q10.17)
    PAUSED = "paused"                         # provider-side loop paused; not used by our skills
    INCOMPLETE = "incomplete"                 # stream ended without a final stop event (Q10.19)
    UNKNOWN = "unknown"


class ResultStatus(str, Enum):
    """Values are the PostgreSQL enum model_attempt_status in schema.sql: change them together."""

    OK = "ok"                                 # parsed and schema-valid
    INVALID_OUTPUT = "invalid_output"         # unparseable or schema-invalid (Q10.20, Q10.21)
    TRUNCATED = "truncated"                   # max_tokens or context exceeded
    INCOMPLETE = "incomplete"                 # stream broke
    REFUSED = "refused"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    PROVIDER_ERROR = "provider_error"         # 5xx / overloaded
    AUTH_ERROR = "auth_error"                 # 401/403: permanent (Q10.38)
    BAD_REQUEST = "bad_request"               # 400/413/422: permanent
    CAPABILITY_UNSUPPORTED = "capability_unsupported"   # Q10.22
    BUDGET_REFUSED = "budget_refused"
    DEADLINE_PASSED = "deadline_passed"


RETRYABLE = {ResultStatus.INVALID_OUTPUT, ResultStatus.INCOMPLETE, ResultStatus.TIMEOUT,
             ResultStatus.RATE_LIMITED, ResultStatus.PROVIDER_ERROR}


class Usage(Strict):
    """Q10.25, Q10.26. Tokens, split by direction. `reported=False` means estimated (Q10.35)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    reported: bool = True


class ModelIdentity(Strict):
    """Q10.23. Enough to reproduce or explain the call months later."""

    provider: str                              # "anthropic" | "tinker"
    model: str                                 # exact model ID returned by the provider
    checkpoint: Optional[str] = None           # Tinker: the sampler checkpoint path
    adapter_version: str
    prompt_version: str
    prompt_sha256: str                         # SHA-256 of the prompt text (ADR-63)
    output_schema_sha256: Optional[str] = None
    context_sha256: str
    provider_request_id: Optional[str] = None  # stored with the attempt (ADR-64)


class ModelResult(Strict):
    """Q10.14."""

    request_id: str
    attempt: int
    status: ResultStatus
    stop_reason: StopReason
    content: list[ContentBlock] = Field(default_factory=list)
    parsed: Optional[dict[str, Any]] = None    # schema-valid output, when requested
    validation_errors: list[str] = Field(default_factory=list)
    refusal_detail: Optional[str] = None
    usage: Usage = Usage()
    cost_micros: int = 0
    latency_ms: int = 0
    identity: Optional[ModelIdentity] = None
    provider_request_id: Optional[str] = None
    error: Optional[str] = None
    input_sha256: Optional[str] = None         # the step's reuse key within an incident (ADR-61)
