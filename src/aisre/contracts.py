"""Artifact 7.3: Pydantic contracts for the AI SRE platform (first version).

These models are the single definition of every object that crosses a boundary:
API requests and responses, and events. The API (FastAPI) and the worker both
import them (ADR-02). Model output is Layer 9's `DiagnosisDraft` (ADR-52).

Field names and enum values match `schema.sql` (ADR-64), not `schema-tables.sql`.
`CloudEvent` uses lower-case names on the wire and maps to `timeline_events`
through the `timeline_cloudevents` view. test_contracts.py keeps them in step.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Generic, Literal, TypeVar
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
Reason = Annotated[str, StringConstraints(min_length=10, max_length=2000)]

T = TypeVar("T")


class Strict(BaseModel):
    """Base for every contract: unknown fields are rejected, values are frozen."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


# ---------------------------------------------------------------------------
# Enumerations (one per PostgreSQL type in schema.sql; states follow ADR-46)
# ---------------------------------------------------------------------------
class IncidentStatus(str, Enum):
    open = "open"
    investigating = "investigating"
    diagnosed = "diagnosed"
    inconclusive = "inconclusive"
    testing = "testing"
    awaiting_approval = "awaiting_approval"
    executing = "executing"
    reconciling = "reconciling"
    verifying = "verifying"
    recovered = "recovered"
    not_recovered = "not_recovered"
    handed_over = "handed_over"
    cancelled = "cancelled"
    closed = "closed"


class ActionStatus(str, Enum):
    proposed = "proposed"
    testing = "testing"
    test_failed = "test_failed"
    awaiting_approval = "awaiting_approval"
    approved = "approved"
    rejected = "rejected"
    expired = "expired"
    applying = "applying"
    outcome_unknown = "outcome_unknown"
    executed = "executed"
    not_applied = "not_applied"
    verified = "verified"
    undone = "undone"
    withdrawn = "withdrawn"


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


class HypothesisCategory(str, Enum):
    deployment_code = "deployment_code"
    deployment_config = "deployment_config"
    dependency = "dependency"
    capacity = "capacity"
    data = "data"
    infrastructure = "infrastructure"
    unknown = "unknown"


class EvidenceRelation(str, Enum):
    supports = "supports"
    contradicts = "contradicts"


class DiagnosisOutcome(str, Enum):
    established = "established"
    inconclusive = "inconclusive"


class ActionKind(str, Enum):
    """The allowed list of change types in the first version (Artifact 5), plus the
    rollback of one of them, which is its own action (ADR-48)."""

    config_revert = "config_revert"
    commit_revert = "commit_revert"
    release_rollback = "release_rollback"
    rollback = "rollback"


class TestOutcome(str, Enum):
    passed = "passed"
    failed = "failed"
    not_reproduced = "not_reproduced"
    inconclusive = "inconclusive"
    crashed = "crashed"
    timeout = "timeout"


class ApprovalDecision(str, Enum):
    approved = "approved"
    rejected = "rejected"
    revoked = "revoked"


class ExecutionOutcome(str, Enum):
    started = "started"
    applied = "applied"
    not_applied = "not_applied"
    unknown = "unknown"
    failed = "failed"


class VerificationResult(str, Enum):
    verified = "verified"
    failed = "failed"
    inconclusive = "inconclusive"


class ModelTask(str, Enum):
    hypothesise = "hypothesise"
    diagnose = "diagnose"
    critique = "critique"
    draft_patch = "draft_patch"


class ModelAttemptStatus(str, Enum):
    """Layer 10's `ResultStatus` values."""

    ok = "ok"
    invalid_output = "invalid_output"
    truncated = "truncated"
    incomplete = "incomplete"
    refused = "refused"
    timeout = "timeout"
    rate_limited = "rate_limited"
    provider_error = "provider_error"
    auth_error = "auth_error"
    bad_request = "bad_request"
    capability_unsupported = "capability_unsupported"
    budget_refused = "budget_refused"
    deadline_passed = "deadline_passed"


class ModelStopReason(str, Enum):
    """Layer 10's `StopReason` values."""

    complete = "complete"
    tool_use = "tool_use"
    max_tokens = "max_tokens"
    context_exceeded = "context_exceeded"
    stop_sequence = "stop_sequence"
    refusal = "refusal"
    paused = "paused"
    incomplete = "incomplete"
    unknown = "unknown"


class ReservationStatus(str, Enum):
    reserved = "reserved"
    settled = "settled"
    released = "released"


class ArtifactKind(str, Enum):
    evidence_snapshot = "evidence_snapshot"
    patch_diff = "patch_diff"
    test_output = "test_output"
    model_input = "model_input"
    model_output = "model_output"
    execution_response = "execution_response"
    report = "report"


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
# API: paged lists
# ---------------------------------------------------------------------------
class Page(Strict, Generic[T]):
    """Every paged list, and the approval history."""

    items: list[T]
    next_cursor: str | None = Field(default=None, description="Null on the last page")
    next_after_seq: int | None = Field(default=None, description="Timeline pages only (Q7.30)")


# ---------------------------------------------------------------------------
# Domain records (stored, returned by the API, and used by the worker)
# ---------------------------------------------------------------------------
class EvidenceRecord(Strict):
    evidence_id: UUID
    incident_id: UUID
    kind: EvidenceKind
    source_system: str
    tool: str = Field(description="Registry name of the tool that produced it")
    collected_by: str
    query: str | None
    window_start: datetime | None
    window_end: datetime | None
    collected_at: datetime
    source_as_of: datetime | None = Field(description="The source's own version time")
    source_link: str | None
    summary: str = Field(max_length=4000, description="Short, redacted extract")
    attributes: dict[str, Any] = Field(description="Kind-specific fields")
    artifact_id: UUID | None
    content_sha256: Sha256
    is_partial: bool
    flagged_injection: bool
    gap_reason: str | None
    strength_tier: int | None = Field(ge=1, le=8, description="Assigned by code (ADR-53)")
    content_removed_at: datetime | None = Field(
        default=None, description="Set by a legal or safety deletion (Q10.55)")
    content_removed_reason: str | None = None

    @model_validator(mode="after")
    def gap_and_window(self) -> EvidenceRecord:
        if (self.kind == EvidenceKind.gap) != (self.gap_reason is not None):
            raise ValueError("gap records, and only gap records, carry gap_reason")
        if self.window_start and self.window_end and self.window_end < self.window_start:
            raise ValueError("window_end is before window_start")
        if (self.content_removed_at is None) != (self.content_removed_reason is None):
            raise ValueError("content_removed_at and content_removed_reason come together")
        return self


class EvidenceLink(Strict):
    evidence_id: UUID
    relation: EvidenceRelation
    note: str | None = None


class Claim(Strict):
    """One factual statement with its evidence; the JSONB shape of `claims` (Layer 9)."""

    text: ShortText
    evidence_ids: list[UUID] = Field(min_length=1)


class Hypothesis(Strict):
    hypothesis_id: UUID
    ordinal: int = Field(ge=1, description="Within the incident, across diagnosis revisions")
    statement: ShortText
    category: HypothesisCategory
    claims: list[Claim]
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
    outcome: DiagnosisOutcome
    leading_hypothesis_id: UUID | None
    category: HypothesisCategory
    confidence: Confidence
    summary: LongText
    claims: list[Claim]
    hypotheses: list[Hypothesis] = Field(min_length=2, max_length=5)
    ruled_out: list[str]
    missing_evidence: list[str]
    stop_reason: str | None
    reviewed: bool
    review_notes: str | None
    produced_by: str = Field(description="Component and model attempt")

    @model_validator(mode="after")
    def leading_matches_outcome(self) -> Diagnosis:
        established = self.outcome == DiagnosisOutcome.established
        if established != (self.leading_hypothesis_id is not None):
            raise ValueError("an established diagnosis needs a leading hypothesis; an inconclusive one has none")
        if established and self.confidence == Confidence.low:
            raise ValueError("low confidence is never established (ADR-53)")
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
    parent_action_id: UUID | None = Field(description="The fix a rollback undoes (ADR-48)")
    kind: ActionKind
    target_service: Slug
    target_environment: Slug
    target_deployment: str = Field(description="Release-process name, e.g. checkout-prod-eu1")
    base_revision: str = Field(description="What the target must run")
    expected_after_revision: str = Field(description="What the target runs afterwards")
    description: ShortText
    expected_effect: ShortText
    risk: ShortText
    undo_plan: ShortText
    recovery_criteria: list[RecoveryCriterion] = Field(min_length=1)
    hold_seconds: int = Field(ge=60, description="How long recovery must hold (provisional: 600)")
    action_digest: Sha256 | None = Field(description="What an approval names (ADR-49); set when testing ends")
    status: ActionStatus
    version: int
    patch: PatchView | None
    latest_test: TestRunView | None

    @model_validator(mode="after")
    def rollback_and_digest(self) -> ActionView:
        if (self.kind == ActionKind.rollback) != (self.parent_action_id is not None):
            raise ValueError("rollbacks, and only rollbacks, carry parent_action_id")
        before_digest = {ActionStatus.proposed, ActionStatus.testing, ActionStatus.test_failed, ActionStatus.withdrawn}
        if self.status not in before_digest and self.action_digest is None:
            raise ValueError(f"an action in {self.status.value} needs its action_digest")
        return self


class PatchView(Strict):
    patch_id: UUID
    base_revision: str
    patch_sha256: Sha256 = Field(description="Patch fingerprint, covered by the action digest")
    files_changed: int
    lines_added: int
    lines_removed: int
    diff_artifact_id: UUID
    review_ref: str | None


class TestRunView(Strict):
    test_run_id: UUID
    patch_sha256: Sha256
    reproduced: bool
    outcome: TestOutcome
    output_artifact_id: UUID | None
    started_at: datetime
    finished_at: datetime


# ---------------------------------------------------------------------------
# API: approval, rejection, revocation and the other writes
#
# The version travels in If-Match, never in a body (ADR-64): missing is 428, stale is
# 412. Cancellation and notes send no If-Match. Every write sends an Idempotency-Key.
# ---------------------------------------------------------------------------
class ApprovalCreate(Strict):
    """POST /v1/actions/{action_id}/approvals. The approver must name the exact action.

    Adding `rollback_action_digest` also signs the conditional approval of the fix's
    rollback, in the same call (ADR-48).
    """

    action_digest: Sha256
    rollback_action_digest: Sha256 | None = None
    reason: Annotated[str, StringConstraints(max_length=1000)] | None = None


class RejectionCreate(Strict):
    """POST /v1/actions/{action_id}/rejections, permission action:reject. A reason is mandatory."""

    action_digest: Sha256
    reason: Reason
    request_more_evidence: bool = False


class RevocationCreate(Strict):
    """POST /v1/actions/{action_id}/revocations. Withdraws the caller's own approval (ADR-49)."""

    approval_id: UUID
    action_digest: Sha256
    reason: Reason


class ApprovalDecisionView(Strict):
    approval_id: UUID
    action_id: UUID
    decision: ApprovalDecision
    decided_by: str
    decided_by_role: str
    action_digest: Sha256
    patch_sha256: Sha256 | None = Field(description="For reading; null if the action has no patch")
    target_service: Slug
    target_environment: Slug
    decided_at: datetime
    valid_until: datetime | None
    reason: str | None
    signing_key_id: str | None
    revokes_approval_id: UUID | None = Field(description="Revocations only")
    conditional: bool = Field(description="A rollback approval signed with its fix (ADR-48)")
    conditional_on_approval_id: UUID | None

    @model_validator(mode="after")
    def fields_match_decision(self) -> ApprovalDecisionView:
        approved = self.decision == ApprovalDecision.approved
        if approved != (self.valid_until is not None):
            raise ValueError("approvals, and only approvals, carry valid_until")
        if approved and self.signing_key_id is None:
            raise ValueError("an approval must be signed")
        if not approved and self.reason is None:
            raise ValueError("rejections and revocations need a reason")
        if (self.decision == ApprovalDecision.revoked) != (self.revokes_approval_id is not None):
            raise ValueError("revocations, and only revocations, carry revokes_approval_id")
        if self.conditional != (self.conditional_on_approval_id is not None):
            raise ValueError("a conditional approval names the fix's approval, and only then")
        if self.conditional and not approved:
            raise ValueError("only an approval can be conditional")
        return self


class OutcomeResolutionCreate(Strict):
    """POST /v1/actions/{action_id}/outcome-resolutions. Clears an unknown outcome (ADR-49).

    Stored on the reconciliation row of `execution_attempts`: `outcome`, `observed_revision`,
    `reason` as `resolution_reason`, and the caller as `resolved_by`.
    """

    outcome: Literal[ExecutionOutcome.applied, ExecutionOutcome.not_applied]
    observed_revision: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    reason: Reason


class CancellationCreate(Strict):
    """POST /v1/incidents/{incident_id}/cancellation."""

    reason: Annotated[str, StringConstraints(min_length=5, max_length=1000)]


class SeverityConfirmationCreate(Strict):
    """POST /v1/incidents/{incident_id}/severity-confirmation."""

    severity: Severity


class ClosureCreate(Strict):
    """POST /v1/incidents/{incident_id}/closure. Accepts the outcome and closes."""

    note: LongText | None = None


class ReinvestigationCreate(Strict):
    """POST /v1/incidents/{incident_id}/reinvestigations. Only from handed_over (ADR-46)."""

    reason: Reason


class NoteCreate(Strict):
    """POST /v1/incidents/{incident_id}/notes."""

    note: LongText


# ---------------------------------------------------------------------------
# Artifact metadata (GET /v1/artifacts/{artifact_id}); never exposes storage paths
# ---------------------------------------------------------------------------
class ArtifactView(Strict):
    artifact_id: UUID
    incident_id: UUID
    kind: ArtifactKind
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
    id: UUID = Field(description="Unique per occurrence")  # timeline_events.event_id
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

    @model_validator(mode="after")
    def data_matches_type(self) -> CloudEvent:
        if self.type not in EVENT_DATA:
            raise ValueError(f"unknown event type {self.type}")
        EVENT_DATA[self.type].model_validate(self.data)
        return self


# ---------------------------------------------------------------------------
# Event payloads: one model per type in Artifact 7.5. The person or component that
# acted is the envelope's `actor`; `data` adds what is specific to the event.
# ---------------------------------------------------------------------------
class IncidentCreatedData(Strict):
    incident_id: UUID
    display_ref: str
    service: Slug
    environment: Slug
    title: str
    source: Literal["monitoring", "engineer"]
    started_at: datetime


class AlertAttachedData(Strict):
    alert_id: UUID
    source_alert_id: str | None
    fingerprint: str
    received_at: datetime


class SeverityProposedData(Strict):
    proposed_severity: Severity


class SeverityConfirmedData(Strict):
    confirmed_severity: Severity
    severity_confirmed_by: str


class IncidentStatusChangedData(Strict):
    from_status: IncidentStatus
    to_status: IncidentStatus
    reason: str | None = None


class IncidentHandedOverData(Strict):
    from_status: IncidentStatus
    reason: str


class CancellationRequestedData(Strict):
    reason: str


class IncidentCancelledData(Strict):
    from_status: IncidentStatus
    reason: str


class IncidentClosedData(Strict):
    from_status: IncidentStatus
    closed_at: datetime
    note: str | None = None


class ReinvestigationRequestedData(Strict):
    investigation_run: int = Field(ge=2, description="2 means workflow incident-<id>-r2")
    reason: str


class NoteAddedData(Strict):
    note: str


class EvidenceCollectedData(Strict):
    evidence_id: UUID
    kind: EvidenceKind
    source_system: str
    tool: str
    content_sha256: Sha256
    is_partial: bool
    strength_tier: int | None = Field(ge=1, le=8)


class GapRecordedData(Strict):
    evidence_id: UUID
    source_system: str
    tool: str
    gap_reason: str


class InjectionFlaggedData(Strict):
    evidence_id: UUID
    source_system: str
    tool: str


class EvidenceContentRemovedData(Strict):
    """One per affected incident: a document was deleted, and evidence citing it marked (Q10.55)."""

    doc_id: str
    reason: str


class HypothesisCreatedData(Strict):
    hypothesis_id: UUID
    ordinal: int = Field(ge=1)
    statement: str
    category: HypothesisCategory
    confidence: Confidence


class HypothesisStatusChangedData(Strict):
    hypothesis_id: UUID
    from_status: HypothesisStatus
    to_status: HypothesisStatus
    confidence: Confidence
    status_reason: str | None


class DiagnosisProducedData(Strict):
    diagnosis_id: UUID
    revision: int = Field(ge=1)
    leading_hypothesis_id: UUID
    category: HypothesisCategory
    confidence: Confidence
    produced_by: str


class DiagnosisInconclusiveData(Strict):
    diagnosis_id: UUID
    revision: int = Field(ge=1)
    stop_reason: str | None
    missing_evidence: list[str]
    produced_by: str


class DiagnosisReviewedData(Strict):
    diagnosis_id: UUID
    revision: int = Field(ge=1)
    reviewed: bool
    review_notes: str | None


class ActionProposedData(Strict):
    action_id: UUID
    diagnosis_id: UUID
    parent_action_id: UUID | None
    kind: ActionKind
    target_service: Slug
    target_environment: Slug
    target_deployment: str


class ActionTestedData(Strict):
    action_id: UUID
    test_run_id: UUID
    patch_sha256: Sha256
    reproduced: bool
    outcome: TestOutcome


class ActionWithdrawnData(Strict):
    action_id: UUID
    reason: str


class ActionUndoneData(Strict):
    action_id: UUID
    rollback_action_id: UUID


class ApprovalRequestedData(Strict):
    approval_request_id: UUID
    action_id: UUID
    action_digest: Sha256
    patch_sha256: Sha256 | None
    requested_by: str
    escalate_at: datetime
    expires_at: datetime
    approvers_needed: int = Field(ge=1, le=2)


class ApprovalGrantedData(Strict):
    approval_id: UUID
    action_id: UUID
    action_digest: Sha256
    patch_sha256: Sha256 | None
    decided_by: str
    valid_until: datetime
    conditional: bool = False


class ApprovalRejectedData(Strict):
    approval_id: UUID
    action_id: UUID
    action_digest: Sha256
    decided_by: str
    reason: str
    request_more_evidence: bool = False


class ApprovalRevokedData(Strict):
    approval_id: UUID
    revokes_approval_id: UUID
    action_id: UUID
    action_digest: Sha256
    decided_by: str
    reason: str


class ApprovalRefusedData(Strict):
    """A failed attempt: it writes no approval row (ADR-49)."""

    action_id: UUID
    decision: ApprovalDecision = Field(description="The decision that was attempted")
    code: str = Field(description="The refusal's Problem code, e.g. digest_mismatch")
    security_alert: bool


class ApprovalEscalatedData(Strict):
    approval_request_id: UUID
    action_id: UUID
    escalate_at: datetime


class ApprovalExpiredData(Strict):
    approval_request_id: UUID
    action_id: UUID
    expires_at: datetime


class ApprovalVoidedData(Strict):
    approval_request_id: UUID
    action_id: UUID
    reason: str


class ExecutionStartedData(Strict):
    attempt_id: UUID
    action_id: UUID
    approval_id: UUID
    action_digest: Sha256
    execution_key: str
    attempt_no: int = Field(ge=1)


class ExecutionCompletedData(Strict):
    attempt_id: UUID
    action_id: UUID
    execution_key: str
    attempt_no: int
    outcome: Literal[
        ExecutionOutcome.applied, ExecutionOutcome.not_applied,
        ExecutionOutcome.unknown, ExecutionOutcome.failed,
    ]
    external_ref: str | None
    reconciliation: bool = False


class ExecutionRefusedData(Strict):
    """A dispatcher pre-check stopped the change: nothing was sent, so no attempt exists."""

    action_id: UUID
    execution_key: str
    reason: str


class ExecutionOutcomeResolvedData(Strict):
    """A person cleared an unknown outcome; the envelope's `actor` is that Incident lead."""

    attempt_id: UUID = Field(description="The reconciliation row that holds the resolution")
    action_id: UUID
    execution_key: str
    outcome: Literal[ExecutionOutcome.applied, ExecutionOutcome.not_applied]
    observed_revision: str
    reason: str


class VerificationCompletedData(Strict):
    verification_id: UUID
    action_id: UUID
    result: VerificationResult
    hold_seconds: int = Field(ge=60)


class VerificationAcceptedData(Strict):
    verification_id: UUID
    action_id: UUID
    accepted_by: str
    accepted_at: datetime


class LimitReachedData(Strict):
    limit: str = Field(description="What ran out, e.g. calls, cost_micros or wall_clock")
    used: int = Field(ge=0)
    maximum: int = Field(ge=0)


class ModelAttemptFailedData(Strict):
    model_attempt_id: UUID
    task: ModelTask
    step_attempt_no: int = Field(ge=1, le=3)
    provider: str
    model: str
    status: ModelAttemptStatus
    stop_reason: ModelStopReason


class NotificationFailedData(Strict):
    event_id: UUID = Field(description="The event the notification carried")
    channel: str
    error: str


EVENT_DATA: dict[str, type[Strict]] = {
    "aisre.incident.created.v1": IncidentCreatedData,
    "aisre.incident.alert_attached.v1": AlertAttachedData,
    "aisre.incident.severity_proposed.v1": SeverityProposedData,
    "aisre.incident.severity_confirmed.v1": SeverityConfirmedData,
    "aisre.incident.status_changed.v1": IncidentStatusChangedData,
    "aisre.incident.handed_over.v1": IncidentHandedOverData,
    "aisre.incident.cancellation_requested.v1": CancellationRequestedData,
    "aisre.incident.cancelled.v1": IncidentCancelledData,
    "aisre.incident.closed.v1": IncidentClosedData,
    "aisre.incident.reinvestigation_requested.v1": ReinvestigationRequestedData,
    "aisre.incident.note_added.v1": NoteAddedData,
    "aisre.evidence.collected.v1": EvidenceCollectedData,
    "aisre.evidence.gap_recorded.v1": GapRecordedData,
    "aisre.evidence.injection_flagged.v1": InjectionFlaggedData,
    "aisre.evidence.content_removed.v1": EvidenceContentRemovedData,
    "aisre.hypothesis.created.v1": HypothesisCreatedData,
    "aisre.hypothesis.status_changed.v1": HypothesisStatusChangedData,
    "aisre.diagnosis.produced.v1": DiagnosisProducedData,
    "aisre.diagnosis.inconclusive.v1": DiagnosisInconclusiveData,
    "aisre.diagnosis.reviewed.v1": DiagnosisReviewedData,
    "aisre.action.proposed.v1": ActionProposedData,
    "aisre.action.tested.v1": ActionTestedData,
    "aisre.action.withdrawn.v1": ActionWithdrawnData,
    "aisre.action.undone.v1": ActionUndoneData,
    "aisre.approval.requested.v1": ApprovalRequestedData,
    "aisre.approval.granted.v1": ApprovalGrantedData,
    "aisre.approval.rejected.v1": ApprovalRejectedData,
    "aisre.approval.revoked.v1": ApprovalRevokedData,
    "aisre.approval.refused.v1": ApprovalRefusedData,
    "aisre.approval.escalated.v1": ApprovalEscalatedData,
    "aisre.approval.expired.v1": ApprovalExpiredData,
    "aisre.approval.voided.v1": ApprovalVoidedData,
    "aisre.execution.started.v1": ExecutionStartedData,
    "aisre.execution.completed.v1": ExecutionCompletedData,
    "aisre.execution.refused.v1": ExecutionRefusedData,
    "aisre.execution.outcome_resolved.v1": ExecutionOutcomeResolvedData,
    "aisre.verification.completed.v1": VerificationCompletedData,
    "aisre.verification.accepted.v1": VerificationAcceptedData,
    "aisre.limit.reached.v1": LimitReachedData,
    "aisre.model.attempt_failed.v1": ModelAttemptFailedData,
    "aisre.notification.failed.v1": NotificationFailedData,
}


# Forward references
IncidentView.model_rebuild()
ActionView.model_rebuild()
