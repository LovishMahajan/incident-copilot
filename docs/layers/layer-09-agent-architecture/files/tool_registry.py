"""Artifact 9.4: the tool registry.

The agent can call only tools listed here, and only READ or PROPOSE tools. WRITE tools
exist for the application (dispatcher) and are never offered to the model. Every call is
validated against the tool's schema, the incident's scope, time and size limits, and the
per-incident call and repeat limits — in code, before anything runs.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class ToolKind(str, Enum):
    READ = "read"          # reads operational data; agent may call
    PROPOSE = "propose"    # creates a draft record (e.g. a patch draft); agent may call
    WRITE = "write"        # changes an external system; application only, never the agent


class ToolRefused(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# Parameter schemas
# ---------------------------------------------------------------------------
class _P(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WindowParams(_P):
    service: str
    start: datetime
    end: datetime
    limit: int = Field(default=200, ge=1, le=500)

    @model_validator(mode="after")
    def ordered(self) -> WindowParams:
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class LogQuery(WindowParams):
    level: Optional[str] = Field(default=None, pattern=r"^(error|warning|info)$")
    contains: Optional[str] = Field(default=None, max_length=200)


class MetricQuery(WindowParams):
    metric: str = Field(pattern=r"^(http_5xx_rate|http_latency_p95|http_request_rate|error_rate_by_version)$")


class ChangeQuery(WindowParams):
    pass


class TraceQuery(WindowParams):
    path: Optional[str] = Field(default=None, max_length=300)


class DependencyQuery(_P):
    service: str
    at: datetime


class PatchDraftParams(_P):
    service: str
    action_kind: str = Field(pattern=r"^(config_revert|commit_revert|release_rollback)$")
    target_ref: str = Field(max_length=200)


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Tool:
    name: str
    kind: ToolKind
    description: str
    params: type[BaseModel]
    credential_scope: str                    # the narrow credential the adapter uses
    max_window: Optional[timedelta]          # None for point-in-time tools
    max_output_bytes: int
    max_calls_per_incident: int
    handler: Optional[Callable] = None       # set by the platform at start-up


DEFAULT_LOOKBACK = timedelta(minutes=60)

REGISTRY: dict[str, Tool] = {t.name: t for t in [
    Tool("get_logs", ToolKind.READ, "Error and warning log lines for the incident's service",
         LogQuery, "logs:read:{service}", timedelta(hours=6), 256_000, 8),
    Tool("get_metric", ToolKind.READ, "One named metric series for the incident's service",
         MetricQuery, "metrics:read:{service}", timedelta(hours=24), 64_000, 10),
    Tool("get_changes", ToolKind.READ, "Deployments and configuration changes for the service",
         ChangeQuery, "changes:read:{service}", timedelta(hours=24), 64_000, 4),
    Tool("get_traces", ToolKind.READ, "Failing request traces for the service",
         TraceQuery, "traces:read:{service}", timedelta(hours=2), 128_000, 6),
    Tool("get_dependency_health", ToolKind.READ, "Health of the service's dependencies at a time",
         DependencyQuery, "catalog:read", None, 32_000, 4),
    Tool("draft_patch", ToolKind.PROPOSE, "Create a draft patch from the allowed list; never applied",
         PatchDraftParams, "review:propose:{service}", None, 64_000, 2),
    # WRITE tools: registered for the dispatcher only. agent_tools() never returns them.
    Tool("apply_change", ToolKind.WRITE, "Apply an approved change through the release process",
         PatchDraftParams, "release:write:{service}", None, 16_000, 1),
]}


def agent_tools() -> list[Tool]:
    """What the model is shown. WRITE tools are excluded by construction."""
    return [t for t in REGISTRY.values() if t.kind != ToolKind.WRITE]


def tool_schema_for_model() -> list[dict]:
    return [{"name": t.name, "description": t.description,
             "input_schema": t.params.model_json_schema()} for t in agent_tools()]


# ---------------------------------------------------------------------------
# The gate every agent tool call goes through
# ---------------------------------------------------------------------------
@dataclass
class ToolGate:
    incident_id: str
    service: str
    incident_start: datetime
    now: datetime
    max_total_calls: int = 20
    calls_by_tool: dict[str, int] = field(default_factory=dict)
    seen: dict[str, int] = field(default_factory=dict)       # canonical call hash -> times
    total_calls: int = 0
    log: list[dict] = field(default_factory=list)

    def check(self, name: str, raw_params: dict) -> tuple[Tool, BaseModel, str]:
        """Validate one requested call. Returns (tool, params, call_hash) or raises ToolRefused."""
        tool = REGISTRY.get(name)
        if tool is None:
            raise ToolRefused("unknown_tool", name)
        if tool.kind == ToolKind.WRITE:
            raise ToolRefused("write_forbidden", f"{name} changes external systems; the agent cannot call it")
        raw = dict(raw_params)
        if "start" in tool.params.model_fields and "start" not in raw:
            raw["start"] = (self.incident_start - DEFAULT_LOOKBACK).isoformat()
            raw.setdefault("end", self.now.isoformat())
        try:
            params = tool.params.model_validate(raw)
        except ValidationError as e:
            raise ToolRefused("invalid_params", e.errors()[0]["msg"]) from None
        if getattr(params, "service", self.service) != self.service:
            raise ToolRefused("out_of_scope", f"service {params.service} is not this incident's service")
        if tool.max_window is not None:
            if params.end - params.start > tool.max_window:
                raise ToolRefused("window_too_large", f"max {tool.max_window} for {name}")
            if params.end > self.now + timedelta(minutes=1):
                raise ToolRefused("window_in_future", "end is in the future")
            if params.start < self.incident_start - timedelta(hours=24):
                raise ToolRefused("window_too_old", "more than 24 h before the incident started")
        if self.total_calls >= self.max_total_calls:
            raise ToolRefused("call_budget", f"{self.max_total_calls} tool calls per incident")
        if self.calls_by_tool.get(name, 0) >= tool.max_calls_per_incident:
            raise ToolRefused("tool_budget", f"{tool.max_calls_per_incident} calls of {name} per incident")
        call_hash = hashlib.sha256(json.dumps(
            {"tool": name, "params": params.model_dump(mode="json")}, sort_keys=True).encode()).hexdigest()
        if self.seen.get(call_hash, 0) >= 2:
            raise ToolRefused("repeated_call", "same tool and parameters already called twice")
        return tool, params, call_hash

    def record(self, name: str, call_hash: str, new_evidence: int) -> None:
        self.total_calls += 1
        self.calls_by_tool[name] = self.calls_by_tool.get(name, 0) + 1
        self.seen[call_hash] = self.seen.get(call_hash, 0) + 1
        self.log.append({"tool": name, "hash": call_hash, "new_evidence": new_evidence})

    def is_repeat(self, call_hash: str) -> bool:
        return self.seen.get(call_hash, 0) >= 1
