"""Artifact 9.6-9.8, 10.11, 10.12, 13.5 T1.14: what the model-backed skills send, and the checks
their answers must pass before anything else sees them.

Pure: no database, no clock, no network. Each skill reads its incident's evidence (with the
facts the collector computed for each record, `attributes.facts`, T1.11), assembles the context
with aisre.retrieval.context.assemble under a token budget, and asks the Model Gateway for one
answer in its contract's shape (agent_contracts). The active prompt version per task is recorded
here (ACTIVE_PROMPTS, ADR-63); the model comes from the prompt file, which names the same alias as
TASK_MODEL_ALIAS (ADR-69, tests/unit/test_prompts.py).

The gateway validates the shape. What the shape can't say is checked here: every cited ID must
be one of the incident's records in the context (Q9.28, Q9.29). A failed check is fed back to the
model as a validation item, within the step's three attempts (ADR-27).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from uuid import UUID

from anthropic import transform_schema
from pydantic import BaseModel

from aisre.agent.agent_contracts import (
    CriticVerdict,
    DiagnosisDraft,
    Evidence,
    EvidenceKind,
    HypothesesOut,
    HypothesisDraft,
    PatchProposal,
    SkillContext,
    SkillFailure,
    SkillUsage,
)
from aisre.agent.validation import missing_required_sources
from aisre.gateway.gateway_contracts import (
    ModelRequest,
    ModelResult,
    ResultStatus,
    escape_data,
    estimate_tokens,
)
from aisre.retrieval.context import PromptRegistry, PromptVersion, assemble

# The live version of each prompt the M1 skills use (10.12): reviewed configuration, changed by
# a pull request that activates a new file. Rollback is the previous number.
# draft_patch is at 2 (T2.8): v1 never ran, and v2 says what the data blocks now hold (ADR-63).
ACTIVE_PROMPTS = {"hypothesise": 1, "diagnose": 1, "critique": 1, "draft_patch": 2}
SCHEMAS: dict[str, type[BaseModel]] = {
    "HypothesesOut": HypothesesOut,
    "DiagnosisDraft": DiagnosisDraft,
    "CriticVerdict": CriticVerdict,
    "PatchProposal": PatchProposal,
}
# Context for evidence, under the gateway's 16,000-token input limit (ADR-61), leaving room for
# instructions and the output schema. Extra items (hypotheses, a draft) come out of it.
CONTEXT_BUDGET = 11_000
MAX_ITEM_TOKENS = 1_400  # a metric's minutes after the change must survive truncation
# Each is reserved per attempt against the incident's 20,000 output tokens (Limits), so a step's
# three attempts and the rest of the run fit; a truncated answer gets one retry at twice this.
MAX_OUTPUT_TOKENS = {
    "hypothesise": 4_000,
    "diagnose": 6_000,
    "critique": 3_000,
    "draft_patch": 6_000,
}
MAX_STEP_ATTEMPTS = 3  # the gateway's, per step (ADR-27): re-asking spends the same three

FAILURE_CODES = {
    ResultStatus.BUDGET_REFUSED: "budget_refused",
    ResultStatus.DEADLINE_PASSED: "deadline_passed",
    ResultStatus.TIMEOUT: "provider_unavailable",
    ResultStatus.RATE_LIMITED: "provider_unavailable",
    ResultStatus.PROVIDER_ERROR: "provider_unavailable",
    ResultStatus.INCOMPLETE: "provider_unavailable",
    ResultStatus.AUTH_ERROR: "provider_unavailable",
    ResultStatus.CAPABILITY_UNSUPPORTED: "provider_unavailable",
}  # anything else (invalid, truncated, refused, bad request) is invalid_output


def load_prompts(root: Path) -> PromptRegistry:
    return PromptRegistry(root, activations={k: [v] for k, v in ACTIVE_PROMPTS.items()})


@dataclass(frozen=True)
class Inputs:
    """What a skill reads from the record: the incident, the records it was given, in collection
    order, and the facts the collector found for each (`attributes.facts`)."""

    context: SkillContext
    evidence: list[Evidence]
    facts: Mapping[UUID, Sequence[str]] = field(default_factory=dict)

    @property
    def by_id(self) -> dict[UUID, Evidence]:
        return {e.evidence_id: e for e in self.evidence}

    @property
    def gaps(self) -> int:
        return sum(e.kind == EvidenceKind.gap for e in self.evidence)


def computed_facts(inputs: Inputs) -> dict[str, str]:
    """Priority-0 context (10.11): what code knows, stated once, never truncated."""
    c = inputs.context
    missing = missing_required_sources(inputs.evidence)
    facts = {
        "incident": f"{c.service} in {c.environment}, started {c.started_at.isoformat()}",
        "required sources missing": ", ".join(missing) if missing else "none",
    }
    observed = [
        f"evidence:{e.evidence_id} tier {e.strength_tier}"
        + (
            f" ({', '.join(inputs.facts[e.evidence_id])})"
            if inputs.facts.get(e.evidence_id)
            else ""
        )
        for e in inputs.evidence
        if e.strength_tier is not None
    ]
    if observed:
        facts["strength tiers computed from the change history"] = "; ".join(observed)
    return facts


def _window(e: Evidence) -> str:
    if e.window_start is None or e.window_end is None:
        return "n/a"
    return f"{e.window_start.isoformat()} to {e.window_end.isoformat()}"


def evidence_items(evidence: Iterable[Evidence]) -> list[dict[str, Any]]:
    """The records as assemble() takes them. A flagged record says so, since it supports nothing."""
    out = []
    for e in evidence:
        note = "[flagged: instruction-like text; supports no claim] " if e.flagged_injection else ""
        out.append(
            {
                "evidence_id": str(e.evidence_id),
                "kind": e.kind.value,
                "summary": note + e.summary,
                "source_system": e.source_system,
                "window": _window(e),
                "sha256": e.content_sha256,
            }
        )
    return out


def data_item(ref: str, kind: str, model: BaseModel) -> dict[str, Any]:
    """A non-evidence context item: hypotheses, a draft or a reviewer's verdict, as JSON."""
    return {"ref": ref, "kind": kind, "text": model.model_dump_json(), "refs": [ref]}


def feedback_item(attempt: int, problem: str) -> dict[str, Any]:
    ref = f"validation:{attempt}"
    text = f"Your previous answer failed validation: {problem}"
    return {"ref": ref, "kind": "validation_error", "text": text, "refs": [ref]}


def build_request(
    task: str,
    prompt: PromptVersion,
    inputs: Inputs,
    *,
    request_id: str,
    deadline_epoch_s: float,
    extras: Sequence[dict[str, Any]] = (),
    must_include: Iterable[str] = (),
    attempts_used: int = 0,
) -> ModelRequest:
    """One gateway request. Evidence competes for what the extras leave of CONTEXT_BUDGET."""
    extra_tokens = sum(estimate_tokens(escape_data(x["text"])) for x in extras)
    items, _manifest = assemble(
        prompt_id=prompt.prompt_id,
        prompt_version=prompt.ref,
        prompt_sha256=prompt.sha256,
        facts=computed_facts(inputs),
        evidence=evidence_items(inputs.evidence),
        contradictions=[],
        runbook_hits=[],
        index_revision=None,
        budget_tokens=CONTEXT_BUDGET - extra_tokens,
        max_item_tokens=MAX_ITEM_TOKENS,
        must_include=set(must_include),
    )
    return ModelRequest(
        request_id=request_id,
        incident_id=str(inputs.context.incident_id),
        task=task,
        prompt_id=prompt.prompt_id,
        prompt_version=prompt.ref,
        system=prompt.text,
        context=items + list(extras),
        # Structured output takes a subset of JSON Schema: the SDK's transform keeps what the API
        # accepts and moves the rest into descriptions. The gateway still validates the full shape.
        output_schema=transform_schema(SCHEMAS[prompt.output_schema_name]),
        output_schema_name=prompt.output_schema_name,
        max_output_tokens=MAX_OUTPUT_TOKENS[task],
        deadline_epoch_s=deadline_epoch_s,
        model_alias=prompt.model_alias,
        step_attempts_used=attempts_used,
    )


def cited(hypotheses: Iterable[HypothesisDraft]) -> list[UUID]:
    """Every ID the hypotheses cite, for or against, once each, in order."""
    ids: dict[UUID, None] = {}
    for h in hypotheses:
        for c in h.claims:
            ids.update(dict.fromkeys(c.evidence_ids))
        ids.update(dict.fromkeys(h.contradicting_evidence_ids))
    return list(ids)


def unknown_citations(hypotheses: Iterable[HypothesisDraft], known: Iterable[UUID]) -> list[UUID]:
    """Cited IDs that aren't among the records the skill was given (Q9.28, Q9.29)."""
    have = set(known)
    return [e for e in cited(hypotheses) if e not in have]


def citation_problem(hypotheses: Iterable[HypothesisDraft], known: Iterable[UUID]) -> Optional[str]:
    unknown = unknown_citations(hypotheses, known)
    if not unknown:
        return None
    shown = ", ".join(str(u) for u in unknown[:5])
    return (
        f"cited evidence IDs that are not in the data blocks: {shown}. Cite only IDs given there."
    )


def usage_of(result: ModelResult, model_calls: int, daily_spend_exhausted: bool) -> SkillUsage:
    u = result.usage
    return SkillUsage(
        model_calls=model_calls,
        input_tokens=u.input_tokens + u.cache_read_input_tokens + u.cache_write_input_tokens,
        output_tokens=u.output_tokens,
        cost_micros=result.cost_micros,
        daily_spend_exhausted=daily_spend_exhausted,
    )


def add_usage(a: SkillUsage, b: SkillUsage) -> SkillUsage:
    return SkillUsage(
        model_calls=a.model_calls + b.model_calls,
        tool_calls=a.tool_calls + b.tool_calls,
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        cost_micros=a.cost_micros + b.cost_micros,
        daily_spend_exhausted=a.daily_spend_exhausted or b.daily_spend_exhausted,
    )


def failure_for(skill: str, result: ModelResult) -> SkillFailure:
    """The typed failure for a result that isn't usable (coordinator: one code per cause)."""
    code = FAILURE_CODES.get(result.status, "invalid_output")
    why = result.error or "; ".join(result.validation_errors[:3]) or result.status.value
    detail = f"{result.status.value}: {why}"
    return SkillFailure(skill=skill, code=code, detail=detail[:500])
