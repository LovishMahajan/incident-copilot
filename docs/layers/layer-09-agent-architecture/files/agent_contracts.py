"""Artifact 9.3, 9.5–9.8: typed contracts for every agent skill.

Every skill takes one Pydantic input and returns one Pydantic output. Model-backed skills
return only these shapes; anything else is rejected whole. Field names follow the canonical
schema.sql (ADR-64). The contracts marked proposed in 9.3 await the team's sign-off.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Generic, Literal, Optional, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from tool_registry import PatchDraftParams

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Short = Annotated[str, StringConstraints(min_length=1, max_length=500)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


# ---------------------------------------------------------------------------
# 9.5 Evidence schema
# ---------------------------------------------------------------------------
class EvidenceKind(str, Enum):
    alert = "alert"
    metric = "metric"
    log = "log"
    trace = "trace"
    error_group = "error_group"
    change = "change"
    dependency = "dependency"
    past_incident = "past_incident"
    runbook = "runbook"
    test_result = "test_result"
    gap = "gap"


NO_TIER_KINDS = {EvidenceKind.gap, EvidenceKind.runbook, EvidenceKind.past_incident}


class Evidence(Strict):
    """One collected fact. Produced only by code (tools), never by the model."""

    evidence_id: UUID
    incident_id: UUID
    kind: EvidenceKind
    source_system: str
    tool: str                                   # registry name of the tool that produced it
    query: Optional[str]                        # exact, rerunnable
    # Three clocks (Q9.21)
    window_start: Optional[datetime]            # when the data happened (from)
    window_end: Optional[datetime]              # when the data happened (to)
    collected_at: datetime                      # when we read it
    source_as_of: Optional[datetime]            # the source's own version or last-modified time
    source_link: Optional[str]
    summary: Annotated[str, StringConstraints(max_length=4000)]   # redacted extract given to the model
    content_sha256: Sha256                      # fingerprint of the snapshot (Q9.22)
    is_partial: bool = False
    flagged_injection: bool = False
    gap_reason: Optional[str] = None            # set only when kind == gap
    strength_tier: Optional[int] = Field(default=None, ge=1, le=8)   # set by code (ADR-53)

    @model_validator(mode="after")
    def gap_has_reason(self) -> Evidence:
        if (self.kind == EvidenceKind.gap) != (self.gap_reason is not None):
            raise ValueError("gap records, and only gap records, carry gap_reason")
        if self.kind != EvidenceKind.gap and not self.source_link:
            raise ValueError("only gap records may omit source_link")
        if self.strength_tier is not None and self.kind in NO_TIER_KINDS:
            raise ValueError("gaps and retrieved guidance carry no strength_tier")
        return self


# ---------------------------------------------------------------------------
# 9.6 Hypothesis schema (model output, then validated)
# ---------------------------------------------------------------------------
class EvidenceRequest(Strict):
    """A bounded, typed request from the hypothesis skill or the critic. READ tools only;
    the coordinator decides whether to run it."""

    tool: str
    reason: Short
    params: dict[str, str | int]


class Claim(Strict):
    """One factual statement, with the evidence that supports it."""

    text: Short
    evidence_ids: list[UUID] = Field(min_length=1)


class HypothesisDraft(Strict):
    statement: Short
    category: Literal["deployment_code", "deployment_config", "dependency", "capacity",
                      "data", "infrastructure", "unknown"]
    claims: list[Claim] = Field(min_length=1, max_length=8)
    contradicting_evidence_ids: list[UUID] = Field(default_factory=list)
    confirm_test: Short                        # what would confirm it
    refute_test: Short                         # what would rule it out
    evidence_needed: list[EvidenceRequest] = Field(default_factory=list, max_length=5)


class HypothesesOut(Strict):
    hypotheses: list[HypothesisDraft] = Field(min_length=2, max_length=5)

    @model_validator(mode="after")
    def includes_non_deployment(self) -> HypothesesOut:
        if all(h.category.startswith("deployment") for h in self.hypotheses):
            raise ValueError("at least one hypothesis must not blame the deployment")
        return self


# ---------------------------------------------------------------------------
# 9.7 Diagnosis schema
# ---------------------------------------------------------------------------
class DiagnosisDraft(Strict):
    outcome: Literal["established", "inconclusive"]
    leading_index: Optional[int] = Field(default=None, ge=0, le=4)
    hypotheses: list[HypothesisDraft] = Field(min_length=2, max_length=5)
    summary: Annotated[str, StringConstraints(min_length=1, max_length=4000)]
    ruled_out: list[Short] = Field(default_factory=list)    # why each alternative was set aside
    missing_evidence: list[Short] = Field(default_factory=list)
    proposed_action: Optional[Literal["config_revert", "commit_revert", "release_rollback"]] = None

    @model_validator(mode="after")
    def consistent(self) -> DiagnosisDraft:
        if (self.outcome == "established") != (self.leading_index is not None):
            raise ValueError("established needs leading_index; inconclusive has none")
        if self.leading_index is not None and self.leading_index >= len(self.hypotheses):
            raise ValueError("leading_index out of range")
        if self.outcome == "inconclusive" and self.proposed_action is not None:
            raise ValueError("no action from an inconclusive diagnosis")
        return self

    def stored_fields(self) -> dict:
        """The model's part of a `diagnoses` row (schema.sql). The leading hypothesis gives
        category and claims; an inconclusive diagnosis has none, so `unknown` and no claims."""
        lead = self.hypotheses[self.leading_index] if self.leading_index is not None else None
        return {"outcome": self.outcome, "summary": self.summary,
                "category": lead.category if lead else "unknown",
                "claims": [c.model_dump(mode="json") for c in lead.claims] if lead else [],
                "ruled_out": list(self.ruled_out), "missing_evidence": list(self.missing_evidence)}


class Confidence(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


# ---------------------------------------------------------------------------
# 9.8 Critic schema
# ---------------------------------------------------------------------------
class IssueKind(str, Enum):
    unsupported_claim = "unsupported_claim"          # claim cites nothing, or cites evidence that doesn't say it
    contradicted = "contradicted"                    # evidence contradicts the leading hypothesis
    alternative_not_ruled_out = "alternative_not_ruled_out"
    correlation_only = "correlation_only"            # only timing links the change to the failure
    missing_evidence = "missing_evidence"            # a decisive check was not collected
    stale_evidence = "stale_evidence"                # cited evidence changed after collection
    scope = "scope"                                  # action outside the incident's service or allowed list
    overconfident = "overconfident"


class CriticIssue(Strict):
    kind: IssueKind
    detail: Short
    claim_index: Optional[int] = None
    evidence_ids: list[UUID] = Field(default_factory=list)
    blocking: bool


class CriticVerdict(Strict):
    verdict: Literal["accept", "revise", "escalate"]
    issues: list[CriticIssue] = Field(default_factory=list, max_length=20)
    evidence_requests: list[EvidenceRequest] = Field(default_factory=list, max_length=3)
    confidence_cap: Confidence

    @model_validator(mode="after")
    def coherent(self) -> CriticVerdict:
        if self.verdict == "accept" and any(i.blocking for i in self.issues):
            raise ValueError("cannot accept with blocking issues")
        if self.verdict == "revise" and not self.issues and not self.evidence_requests:
            raise ValueError("revise must say what to fix or what to collect")
        return self


# ---------------------------------------------------------------------------
# Skill I/O (9.2, 9.3)
# ---------------------------------------------------------------------------
class SkillContext(Strict):
    incident_id: UUID
    service: str
    environment: str
    started_at: datetime


class HypothesisIn(Strict):
    context: SkillContext
    evidence: list[Evidence]
    previous: Optional[DiagnosisDraft] = None
    critic: Optional[CriticVerdict] = None


class DiagnoseIn(Strict):
    context: SkillContext
    evidence: list[Evidence]
    hypotheses: HypothesesOut
    critic: Optional[CriticVerdict] = None


class CritiqueIn(Strict):
    """The critic sees evidence and the diagnosis only, never the diagnoser's reasoning."""

    context: SkillContext
    evidence: list[Evidence]
    diagnosis: DiagnosisDraft


class CollectIn(Strict):
    context: SkillContext
    mode: Literal["baseline", "targeted"]
    requests: list[EvidenceRequest] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def requests_match_mode(self) -> CollectIn:
        if (self.mode == "targeted") != bool(self.requests):
            raise ValueError("targeted needs 1-3 requests; baseline has none")
        return self


class KnowledgeIn(Strict):
    """Code builds the search terms from the baseline evidence. Permitted groups come from
    the platform's identity, never this input."""

    context: SkillContext
    evidence_ids: list[UUID]
    k: int = Field(default=5, ge=1, le=10)


class KnowledgeOut(Strict):
    evidence_ids: list[str]                     # passages stored as evidence, gap records included
    gap_count: int = Field(ge=0)
    index_revision: str


class PatchIn(Strict):
    context: SkillContext
    diagnosis_id: UUID
    diagnosis: DiagnosisDraft
    evidence: list[Evidence]                    # cited by the leading hypothesis
    candidate_no: int = Field(ge=1, le=3)

    @model_validator(mode="after")
    def established_with_action(self) -> PatchIn:
        if self.diagnosis.outcome != "established" or self.diagnosis.proposed_action is None:
            raise ValueError("a patch needs an established diagnosis with a proposed action")
        return self


class PatchFileEdit(Strict):
    model_config = ConfigDict(str_strip_whitespace=False)       # new_content keeps its whitespace

    path: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, pattern=r"^[^/]")]
    new_content: str
    rationale: Annotated[str, StringConstraints(strip_whitespace=True, min_length=10, max_length=600)]
    evidence_ids: list[UUID] = Field(min_length=1)


class PatchProposal(Strict):
    """The Patch Proposer's model output. Code computes hunks and hashes from it (Artifact 11)."""

    params: PatchDraftParams
    files: list[PatchFileEdit] = Field(default_factory=list)
    summary: Annotated[str, StringConstraints(min_length=10, max_length=1000)]

    @model_validator(mode="after")
    def files_unless_rollback(self) -> PatchProposal:
        if not self.files and self.params.action_kind != "release_rollback":
            raise ValueError("files may be empty only for release_rollback")
        return self


class DiagnosisResult(Strict):
    """The `diagnose` activity's output: the draft, plus the code-computed facts the coordinator
    reads. 9.3 lists `DiagnosisDraft` alone, which carries neither."""

    diagnosis_id: UUID
    draft: DiagnosisDraft
    fingerprint: Sha256                         # validation.fingerprint
    supported_ratio: float = Field(ge=0, le=1)  # validation.CitationReport


class ReviewResult(Strict):
    """The `review_diagnosis` activity's output, from validation.combine. Both fields None
    means invalid critic output after its retries (9.8), which isn't a SkillFailure."""

    verdict: Optional[CriticVerdict] = None
    confidence: Optional[Confidence] = None     # final: the lower of scored and confidence_cap

    @model_validator(mode="after")
    def both_or_neither(self) -> ReviewResult:
        if (self.verdict is None) != (self.confidence is None):
            raise ValueError("a verdict comes with its final confidence")
        return self


class SkillFailure(Strict):
    skill: str
    code: Literal["invalid_output", "provider_unavailable", "budget_refused", "deadline_passed",
                  "no_allowed_fix", "store_failed", "security_risk"]
    detail: Short                               # recorded in stop_reason


class SkillUsage(Strict):
    model_calls: int = Field(default=0, ge=0)   # gateway attempts; a reused result counts 0
    tool_calls: int = Field(default=0, ge=0)    # calls recorded by the ToolGate
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_micros: int = Field(default=0, ge=0)
    daily_spend_exhausted: bool = False         # ADR-55


T = TypeVar("T")


class SkillResult(Strict, Generic[T]):
    """Every activity's result, so the coordinator gets one shape: coordinator.apply_result."""

    output: Optional[T] = None
    failure: Optional[SkillFailure] = None
    usage: SkillUsage

    @model_validator(mode="after")
    def output_or_failure(self) -> SkillResult:
        if (self.output is None) == (self.failure is None):
            raise ValueError("a skill result has an output or a failure, not both or neither")
        return self
