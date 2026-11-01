"""Artifact 9.3, 9.5–9.8: typed contracts for every agent skill.

Every skill takes one Pydantic input and returns one Pydantic output. Model-backed skills
return only these shapes; anything else is rejected whole. Field names match Artifact 7.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

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

    @model_validator(mode="after")
    def gap_has_reason(self) -> Evidence:
        if (self.kind == EvidenceKind.gap) != (self.gap_reason is not None):
            raise ValueError("gap records, and only gap records, carry gap_reason")
        return self


# ---------------------------------------------------------------------------
# 9.6 Hypothesis schema (model output, then validated)
# ---------------------------------------------------------------------------
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
    evidence_needed: list[Short] = Field(default_factory=list, max_length=5)


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


class EvidenceRequest(Strict):
    """A bounded, typed request the critic may make. The coordinator decides whether to run it."""

    tool: str
    reason: Short
    params: dict[str, str | int]


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
