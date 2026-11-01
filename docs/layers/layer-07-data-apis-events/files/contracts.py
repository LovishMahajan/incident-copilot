"""Artifact 7.3: Pydantic contracts for the AI SRE platform (first version).

These models are the single definition of every object that crosses a boundary:
API requests and responses, model output, and events. The API (FastAPI) and the
worker both import them (ADR-02). Field names match the PostgreSQL schema.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=500)]
LongText = Annotated[str, StringConstraints(min_length=1, max_length=8000)]


class Strict(BaseModel):
    """Base for every contract: unknown fields are rejected, values are frozen."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


# ---------------------------------------------------------------------------
# Enumerations (mirror the PostgreSQL types)
# ---------------------------------------------------------------------------
class IncidentStatus(str, Enum):
    open = "open"
    investigating = "investigating"
    diagnosed = "diagnosed"
    inconclusive = "inconclusive"
    awaiting_approval = "awaiting_approval"
    executing = "executing"
    verifying = "verifying"
    recovered = "recovered"
    not_recovered = "not_recovered"
    handed_over = "handed_over"
    cancelled = "cancelled"
    closed = "closed"


class Severity(str, Enum):
    sev1 = "sev1"
    sev2 = "sev2"
    sev3 = "sev3"
    sev4 = "sev4"


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


class Confidence(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class HypothesisStatus(str, Enum):
    open = "open"
    supported = "supported"
    ruled_out = "ruled_out"
    confirmed = "confirmed"


class ActionKind(str, Enum):
    """The allowed list of change types in the first version (Artifact 5)."""

    config_revert = "config_revert"
    commit_revert = "commit_revert"
    release_rollback = "release_rollback"


# ---------------------------------------------------------------------------
# API: incident intake  (POST /v1/incidents)
# ---------------------------------------------------------------------------
class IncidentCreate(Strict):
    service: Slug = Field(description="Service identifier from the service catalogue")
    environment: Slug
    started_at: datetime = Field(description="When the symptom started, or the alert time")
    title: ShortText = Field(description="Alert name or short description of the symptom")
    source: Literal["monitoring", "engineer"]
    source_alert_id: Annotated[str, StringConstraints(max_length=200)] | None = None
    fingerprint: Annotated[str, StringConstraints(max_length=200)] | None = None
    severity_hint: Severity | None = None
    alert_value: float | None = None
    alert_threshold: float | None = None
    affected_paths: list[Annotated[str, StringConstraints(max_length=300)]] = Field(
        default_factory=list, max_length=20
    )
    links: list[AnyHttpUrl] = Field(default_factory=list, max_length=10)
    description: LongText | None = None


class IncidentAccepted(Strict):
    incident_id: UUID
    display_ref: str
    status: IncidentStatus
    deduplicated: bool = Field(description="True if the alert joined an existing open incident")
    links: dict[str, str] = Field(description="self, timeline, stream")


# ---------------------------------------------------------------------------
# API: incident state  (GET /v1/incidents/{incident_id})
# ---------------------------------------------------------------------------
class IncidentView(Strict):
    incident_id: UUID
    display_ref: str
    service: Slug
    environment: Slug
    title: str
    status: IncidentStatus
    proposed_severity: Severity | None
    confirmed_severity: Severity | None
    started_at: datetime
    created_at: datetime
    updated_at: datetime
    version: int = Field(description="Use as If-Match when changing the incident")
    last_event_seq: int
    current_diagnosis_id: UUID | None
    open_action_ids: list[UUID]
    budget: BudgetView


class BudgetView(Strict):
    max_calls: int
    used_calls: int
    max_cost_micros: int
    spent_cost_micros: int
    reserved_cost_micros: int


# ---------------------------------------------------------------------------
# Domain records (stored, returned by the API, and used by the worker)
# ---------------------------------------------------------------------------
class EvidenceRecord(Strict):
    evidence_id: UUID
    incident_id: UUID
    kind: EvidenceKind
    source_system: str
    collected_by: str
    query: str | None
    window_start: datetime | None
    window_end: datetime | None
    collected_at: datetime
    source_link: str | None
    summary: str = Field(max_length=4000, description="Short, redacted extract")
    attributes: dict[str, Any] = Field(description="Kind-specific fields")
    artifact_id: UUID | None
    content_sha256: Sha256
    is_partial: bool
    flagged_injection: bool


class EvidenceLink(Strict):
    evidence_id: UUID
    relation: Literal["supports", "contradicts"]
    note: str | None = None


class Hypothesis(Strict):
    hypothesis_id: UUID
    ordinal: int = Field(ge=1, le=5)
    statement: ShortText
    confirm_test: ShortText
    refute_test: ShortText
    status: HypothesisStatus
    confidence: Confidence
    status_reason: str | None
    evidence: list[EvidenceLink]


class Diagnosis(Strict):
    diagnosis_id: UUID
    incident_id: UUID
    revision: int = Field(ge=1)
    outcome: Literal["established", "inconclusive"]
    leading_hypothesis_id: UUID | None
    confidence: Confidence
    summary: LongText
    hypotheses: list[Hypothesis] = Field(min_length=2, max_length=5)
    missing_evidence: list[str]
    stop_reason: str | None

    @model_validator(mode="after")
    def leading_matches_outcome(self) -> Diagnosis:
        if (self.outcome == "established") != (self.leading_hypothesis_id is not None):
            raise ValueError("an established diagnosis needs a leading hypothesis; an inconclusive one has none")
        if self.leading_hypothesis_id and self.leading_hypothesis_id not in {h.hypothesis_id for h in self.hypotheses}:
            raise ValueError("leading hypothesis must be one of the listed hypotheses")
        return self


class RecoveryCriterion(Strict):
    metric: str = Field(description="e.g. http_5xx_rate for the failing path")
    comparison: Literal["<=", ">=", "=="]
    threshold: float
    baseline_note: str | None = None


class ActionView(Strict):
    action_id: UUID
    incident_id: UUID
    diagnosis_id: UUID
    kind: ActionKind
    target_service: Slug
    target_environment: Slug
    description: ShortText
    expected_effect: ShortText
    risk: ShortText
    undo_plan: ShortText
    recovery_criteria: list[RecoveryCriterion] = Field(min_length=1)
    hold_seconds: int = Field(ge=60, description="How long recovery must hold (provisional: 600)")
    status: str
    version: int
    patch: PatchView | None
    latest_test: TestRunView | None


class PatchView(Strict):
    patch_id: UUID
    base_revision: str
    diff_sha256: Sha256 = Field(description="The fingerprint an approval must name")
    files_changed: int
    lines_added: int
    lines_removed: int
    diff_artifact_id: UUID
    review_ref: str | None


class TestRunView(Strict):
    test_run_id: UUID
    patch_sha256: Sha256
    reproduced: bool
    outcome: Literal["passed", "failed", "not_reproduced", "error"]
    output_artifact_id: UUID | None
    started_at: datetime
    finished_at: datetime


# ---------------------------------------------------------------------------
# Model output contract (validated by the Model Gateway; never trusted)
# ---------------------------------------------------------------------------
class ModelHypothesis(Strict):
    statement: ShortText
    supporting_evidence_ids: list[UUID] = Field(min_length=1)
    contradicting_evidence_ids: list[UUID]
    confirm_test: ShortText
    refute_test: ShortText
    proposed_action: ActionKind | None = None


class ModelDiagnosisOutput(Strict):
    """What the model must return for the 'diagnose' task. Nothing else is accepted."""

    hypotheses: list[ModelHypothesis] = Field(min_length=2, max_length=5)
    leading_index: int | None = Field(ge=0, le=4, description="Index into hypotheses, or null if inconclusive")
    summary: LongText
    missing_evidence: list[ShortText]

    @model_validator(mode="after")
    def leading_in_range(self) -> ModelDiagnosisOutput:
        if self.leading_index is not None and self.leading_index >= len(self.hypotheses):
            raise ValueError("leading_index out of range")
        return self


# ---------------------------------------------------------------------------
# API: approval, rejection and cancellation
# ---------------------------------------------------------------------------
class ApprovalCreate(Strict):
    """POST /v1/actions/{action_id}/approvals. The approver must name the exact patch."""

    patch_sha256: Sha256
    expected_action_version: int = Field(ge=1)
    note: Annotated[str, StringConstraints(max_length=1000)] | None = None


class RejectionCreate(Strict):
    """POST /v1/actions/{action_id}/rejections. A reason is mandatory."""

    patch_sha256: Sha256
    reason: Annotated[str, StringConstraints(min_length=10, max_length=2000)]
    request_more_evidence: bool = False


class ApprovalDecisionView(Strict):
    approval_id: UUID
    action_id: UUID
    decision: Literal["approved", "rejected"]
    decided_by: str
    decided_by_role: str
    patch_sha256: Sha256
    target_service: Slug
    target_environment: Slug
    decided_at: datetime
    valid_until: datetime | None
    reason: str | None
    signing_key_id: str | None


class CancellationCreate(Strict):
    """POST /v1/incidents/{incident_id}/cancellation."""

    reason: Annotated[str, StringConstraints(min_length=5, max_length=1000)]


# ---------------------------------------------------------------------------
# Artifact metadata (GET /v1/artifacts/{artifact_id}); never exposes storage paths
# ---------------------------------------------------------------------------
class ArtifactView(Strict):
    artifact_id: UUID
    incident_id: UUID
    kind: str
    media_type: str
    size_bytes: int
    content_sha256: Sha256
    redacted: bool
    created_at: datetime
    content_url: str = Field(description="Relative API path to /content; never an internal path")


# ---------------------------------------------------------------------------
# Errors (RFC 9457 problem details)
# ---------------------------------------------------------------------------
class Problem(Strict):
    type: str = Field(description="e.g. https://errors.aisre.internal/forbidden")
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
    code: str = Field(description="Stable machine-readable code, e.g. 'forbidden_service'")
    request_id: str
    errors: list[dict[str, Any]] | None = Field(default=None, description="Field errors for 422")


# ---------------------------------------------------------------------------
# Events: CloudEvents 1.0 envelope (structured JSON mode)
# ---------------------------------------------------------------------------
class CloudEvent(Strict):
    specversion: Literal["1.0"] = "1.0"
    id: UUID = Field(description="Unique per occurrence")
    source: str = Field(description="Producing component, e.g. /aisre/worker/evidence-collector")
    type: str = Field(pattern=r"^aisre\.[a-z_]+\.[a-z_]+\.v[0-9]+$")
    subject: str = Field(description="incidents/<incident_id>")
    time: datetime = Field(description="When it happened")
    datacontenttype: Literal["application/json"] = "application/json"
    dataschema: str | None = None
    # Extension attributes (lower-case alphanumeric, as CloudEvents requires)
    incidentid: UUID
    sequence: int = Field(ge=1, description="Per-incident, gapless")
    orgid: UUID
    actor: str
    correlationid: UUID
    causationid: UUID | None = None
    traceparent: str | None = None
    data: dict[str, Any]


class IncidentCreatedData(Strict):
    incident_id: UUID
    display_ref: str
    service: Slug
    environment: Slug
    title: str
    source: Literal["monitoring", "engineer"]
    started_at: datetime


class ApprovalGrantedData(Strict):
    approval_id: UUID
    action_id: UUID
    patch_sha256: Sha256
    decided_by: str
    valid_until: datetime


class ExecutionCompletedData(Strict):
    attempt_id: UUID
    action_id: UUID
    execution_key: str
    attempt_no: int
    outcome: Literal["applied", "not_applied", "unknown", "failed"]
    external_ref: str | None


class VerificationCompletedData(Strict):
    verification_id: UUID
    action_id: UUID
    result: Literal["recovered", "not_recovered", "inconclusive"]
    hold_seconds: int


# Forward references
IncidentView.model_rebuild()
ActionView.model_rebuild()
