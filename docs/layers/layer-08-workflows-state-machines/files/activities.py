"""Artifact 8.2: activity inventory — the contracts every activity implements.

Activities do all I/O (database, tools, model, sandbox, release process). The workflow
only calls them. Each definition below is the interface; the real implementations live
in the platform's modules and are registered on the task queue named in TASK_QUEUE.
Every activity is idempotent on the key named in its input.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

from temporalio import activity

# Task queues (Artifact 6: the worker process and the dispatcher process)
INVESTIGATION_QUEUE = "aisre-investigation"
DISPATCHER_QUEUE = "aisre-dispatcher"


# ---------------------------------------------------------------------------
# Inputs and outputs (small, serialisable; large items travel as artifact IDs)
# ---------------------------------------------------------------------------
@dataclass
class TransitionIn:
    incident_id: str
    transition_id: str            # idempotency key: deterministic per workflow step
    from_state: str
    to_state: str
    expected_version: int
    reason: str
    action_id: Optional[str] = None
    action_from: Optional[str] = None
    action_to: Optional[str] = None


@dataclass
class TransitionOut:
    new_version: int
    event_seq: int


@dataclass
class IncidentIn:
    incident_id: str


@dataclass
class IncidentSnapshot:
    incident_id: str
    service: str
    environment: str
    version: int
    status: str


@dataclass
class NotifyIn:
    incident_id: str
    notification_id: str          # idempotency key
    kind: str                     # opened, summary, approval_needed, handed_over, ...
    audience: list[str]


@dataclass
class EvidenceOut:
    evidence_ids: list[str]
    gap_count: int


@dataclass
class DiagnosisOut:
    diagnosis_id: str
    outcome: Literal["established", "inconclusive"]
    leading_action_kind: Optional[str]
    stop_reason: Optional[str]


@dataclass
class ReviewOut:
    accepted: bool
    diagnosis_id: str
    notes: str


@dataclass
class PatchOut:
    action_id: str
    patch_sha256: str


@dataclass
class TestIn:
    action_id: str
    patch_sha256: str
    test_key: str                 # idempotency key


@dataclass
class TestOut:
    test_run_id: str
    reproduced: bool
    outcome: Literal["passed", "failed", "not_reproduced", "error"]


@dataclass
class ApprovalRequestIn:
    action_id: str
    patch_sha256: str
    request_key: str              # idempotency key
    approvers_needed: int = 1


@dataclass
class ApprovalRequestOut:
    approval_request_id: str


@dataclass
class DispatchIn:
    action_id: str
    approval_id: str
    execution_key: str            # one per approval: at most one 'applied'
    is_undo: bool = False


@dataclass
class DispatchOut:
    attempt_id: str
    outcome: Literal["applied", "not_applied", "failed", "unknown"]
    external_ref: Optional[str] = None


@dataclass
class VerifyIn:
    action_id: str
    attempt_id: str
    hold_seconds: int


@dataclass
class VerifyOut:
    verification_id: str
    result: Literal["recovered", "not_recovered", "inconclusive"]


@dataclass
class HandOverIn:
    incident_id: str
    reason: str
    handover_key: str


# ---------------------------------------------------------------------------
# Activity interfaces. Implementations: platform modules (Artifact 6, section 6.3).
# ---------------------------------------------------------------------------
@activity.defn(name="load_incident")
async def load_incident(inp: IncidentIn) -> IncidentSnapshot:
    raise NotImplementedError


@activity.defn(name="record_transition")
async def record_transition(inp: TransitionIn) -> TransitionOut:
    """Incident Record: check the transition table and expected version, write the
    status change and its timeline event in one transaction. Idempotent on transition_id."""
    raise NotImplementedError


@activity.defn(name="notify")
async def notify(inp: NotifyIn) -> None:
    raise NotImplementedError


@activity.defn(name="collect_evidence")
async def collect_evidence(inp: IncidentIn) -> EvidenceOut:
    """Evidence Collector: parallel read-only queries, redaction, gaps. Heartbeats."""
    raise NotImplementedError


@activity.defn(name="retrieve_knowledge")
async def retrieve_knowledge(inp: IncidentIn) -> list[str]:
    raise NotImplementedError


@activity.defn(name="diagnose")
async def diagnose(inp: IncidentIn) -> DiagnosisOut:
    """Diagnosis Engine via the Model Gateway: reserves budget, calls the model (up to
    3 attempts inside the gateway), validates, settles. The ONLY owner of model retries."""
    raise NotImplementedError


@activity.defn(name="review_diagnosis")
async def review_diagnosis(inp: IncidentIn) -> ReviewOut:
    raise NotImplementedError


@activity.defn(name="draft_patch")
async def draft_patch(inp: IncidentIn) -> PatchOut:
    raise NotImplementedError


@activity.defn(name="run_sandbox_test")
async def run_sandbox_test(inp: TestIn) -> TestOut:
    """Test Runner: fresh isolated environment, reproduce first, apply, check, destroy."""
    raise NotImplementedError


@activity.defn(name="request_approval")
async def request_approval(inp: ApprovalRequestIn) -> ApprovalRequestOut:
    raise NotImplementedError


@activity.defn(name="escalate_approval")
async def escalate_approval(inp: ApprovalRequestIn) -> None:
    raise NotImplementedError


@activity.defn(name="close_approval_request")
async def close_approval_request(inp: ApprovalRequestIn) -> None:
    """Close as expired, withdrawn or voided. Idempotent."""
    raise NotImplementedError


@activity.defn(name="dispatch_change")
async def dispatch_change(inp: DispatchIn) -> DispatchOut:
    """Trusted Dispatcher (dispatcher task queue): verify signature, fingerprint, expiry
    and service state; record intent; apply once with execution_key. A timeout with no
    answer is returned as outcome='unknown', never retried blindly."""
    raise NotImplementedError


@activity.defn(name="reconcile_execution")
async def reconcile_execution(inp: DispatchIn) -> DispatchOut:
    """Trusted Dispatcher: look up execution_key in the release process, then compare the
    live service version and configuration with the target. Read-only."""
    raise NotImplementedError


@activity.defn(name="verify_recovery")
async def verify_recovery(inp: VerifyIn) -> VerifyOut:
    """Recovery Verifier: reads monitoring directly for the hold period. Heartbeats."""
    raise NotImplementedError


@activity.defn(name="hand_over")
async def hand_over(inp: HandOverIn) -> None:
    raise NotImplementedError


ALL_INVESTIGATION_ACTIVITIES = [
    load_incident, record_transition, notify, collect_evidence, retrieve_knowledge,
    diagnose, review_diagnosis, draft_patch, run_sandbox_test, request_approval,
    escalate_approval, close_approval_request, verify_recovery, hand_over,
]
ALL_DISPATCHER_ACTIVITIES = [dispatch_change, reconcile_execution]
