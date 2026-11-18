"""Test helpers for the model-backed skills (T1.14): the reference incident's evidence as the
collector stores it, an in-memory SkillStore, and a Model Gateway whose transport is the Compose
stack's fake provider (ops/fakes/model_provider.py) answering in-process from its scripts."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any, Optional
from uuid import UUID

from monitoring_scenarios import ALERT_ID, CUT_OFF, FIXTURES, STARTED, serving, sources

from aisre.agent.agent_contracts import (
    Confidence,
    DiagnosisDraft,
    ReviewResult,
    SkillContext,
)
from aisre.agent.skills import reasoning
from aisre.agent.skills.activities import ModelSkills
from aisre.agent.tool_registry import ToolGate
from aisre.evidence.collector import Scope, baseline_plan, collect
from aisre.gateway.adapters import ClaudeAdapter, HttpResponse
from aisre.gateway.gateway import CLAUDE_PRICES, BudgetLedger, ModelGateway

ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "ops/fakes/provider-scripts"
NOW = CUT_OFF.timestamp()


def _load_provider() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "fake_model_provider", ROOT / "ops/fakes/model_provider.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


model_provider = _load_provider()


def collected(root: Path = FIXTURES, incident_id: Optional[str] = None) -> reasoning.Inputs:
    """The reference incident's baseline round, as the collector would store it."""
    incident_id = incident_id or str(uuid.uuid4())
    scope = Scope(incident_id, "orders-api", "production", STARTED, ALERT_ID)
    gate = ToolGate(incident_id, scope.service, scope.started_at, CUT_OFF)
    with serving(root) as url:
        out = asyncio.run(
            collect(
                scope, baseline_plan(scope), gate, sources(url), collection_key="k", now=CUT_OFF
            )
        )
    evidence = [c.evidence for c in out.collected]
    facts = {c.evidence.evidence_id: list(c.attributes.get("facts", [])) for c in out.collected}
    context = SkillContext(
        incident_id=UUID(incident_id),
        service="orders-api",
        environment="production",
        started_at=STARTED,
    )
    return reasoning.Inputs(context, evidence, facts)


class MemorySkillStore:
    def __init__(self, inputs: reasoning.Inputs) -> None:
        self.inputs_ = inputs
        self.diagnoses: dict[UUID, dict[str, Any]] = {}
        self.reviews: dict[UUID, ReviewResult] = {}

    async def inputs(self, incident_id: str, evidence_ids: Sequence[str]) -> reasoning.Inputs:
        wanted = set(evidence_ids)
        ev = [e for e in self.inputs_.evidence if str(e.evidence_id) in wanted]
        return reasoning.Inputs(self.inputs_.context, ev, self.inputs_.facts)

    async def save_diagnosis(
        self,
        diagnosis_id: UUID,
        inputs: reasoning.Inputs,
        draft: DiagnosisDraft,
        confidence: Confidence,
        hypothesis_confidence: list[Confidence],
        produced_by: str,
    ) -> None:
        self.diagnoses.setdefault(
            diagnosis_id,
            {"draft": draft, "confidence": confidence, "each": hypothesis_confidence},
        )

    async def save_review(
        self, diagnosis_id: UUID, inputs: reasoning.Inputs, draft: DiagnosisDraft, r: ReviewResult
    ) -> None:
        self.reviews.setdefault(diagnosis_id, r)


class ScriptedTransport:
    """The fake provider's answers, in-process; `override` replaces them per request."""

    def __init__(
        self,
        scripts: Path = SCRIPTS,
        override: Optional[Callable[[dict[str, Any]], Optional[HttpResponse]]] = None,
    ) -> None:
        self.provider = model_provider.Provider(model_provider.load_scripts(scripts))
        self.override = override
        self.bodies: list[dict[str, Any]] = []

    async def __call__(
        self, provider: str, body: dict[str, Any], timeout: float
    ) -> tuple[HttpResponse, bool]:
        self.bodies.append(body)
        if self.override is not None:
            given = self.override(body)
            if given is not None:
                return given, True
        headers = {"x-api-key": "fake-key-not-real", "anthropic-version": "2026-10-21"}
        status, msg, extra = self.provider.answer(headers, json.dumps(body).encode())
        return HttpResponse(status, msg, extra), True


def gateway(transport: Any, ledger: Optional[BudgetLedger] = None) -> ModelGateway:
    async def no_wait(_: float) -> None:
        return None

    return ModelGateway(
        ClaudeAdapter(),
        transport,
        ledger or BudgetLedger(clock=lambda: NOW),
        CLAUDE_PRICES,
        reasoning.SCHEMAS,
        sleep=no_wait,
        clock=lambda: NOW,
    )


def skills(
    inputs: reasoning.Inputs, transport: Any = None, ledger: Optional[BudgetLedger] = None
) -> tuple[ModelSkills, MemorySkillStore, Any]:
    transport = transport or ScriptedTransport()
    store = MemorySkillStore(inputs)
    prompts = reasoning.load_prompts(ROOT / "prompts")
    return (
        ModelSkills(gateway(transport, ledger), prompts, store, clock=lambda: NOW),
        store,
        transport,
    )
