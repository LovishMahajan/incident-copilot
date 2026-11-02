"""Artifact 8.2: activity inventory — the contracts every activity implements.

Activities do all I/O (database, tools, model, sandbox, release process). The workflow
only calls them. Each definition below is the interface; the real implementations live
in the platform's modules and are registered on the task queue named in TASK_QUEUE.
Every activity is idempotent on the key named in its input, and every input carries one.

Skill activities (collect_evidence, retrieve_knowledge, hypothesise, diagnose,
review_diagnosis, draft_patch) never raise for a failed skill: they return
SkillResult(failure=...), a value the coordinator stops on (ADR-52).

The workflow sends evidence IDs, and each skill activity loads the Evidence rows and builds the
Layer 9 input (CollectIn, HypothesisIn, DiagnoseIn, CritiqueIn, PatchIn) itself, so full evidence
stays out of Temporal's history. Worker and client both use DATA_CONVERTER: SkillResult is a
Pydantic model.

Imported into workflow code only through workflow.unsafe.imports_passed_through().
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, Optional

from temporalio import activity
from temporalio.client import (Client, Schedule, ScheduleActionStartWorkflow, ScheduleIntervalSpec,
                               ScheduleSpec)
from temporalio.contrib.pydantic import pydantic_data_converter

from agent_contracts import (CriticVerdict, DiagnosisDraft, DiagnosisResult, HypothesesOut, KnowledgeOut,
                             ReviewResult, SkillFailure, SkillResult, SkillUsage)
from states import InvalidTransition
from tool_registry import ToolGate, ToolRefused
from validation import UnsafeOutput, check_output

# Task queues (Artifact 6: the worker process and the dispatcher process)
INVESTIGATION_QUEUE = "aisre-investigation"
DISPATCHER_QUEUE = "aisre-dispatcher"

DATA_CONVERTER = pydantic_data_converter
MAX_INVESTIGATIONS = 5                 # running at once, across all workers (8.1)
MAX_SLOTS_PER_SERVICE = 4              # NFR-44, provisional: one service can't take every slot
RECONCILIATION_SCHEDULE = "aisre-reconciliation"
RECONCILIATION_EVERY = timedelta(minutes=5)   # provisional (8.9)
REFUSED_EXPIRED = "approval expired"   # start of the dispatcher's reason when the approval ran out
DB_REFUSALS = {"23514", "23001"}       # check_violation, restrict_violation: the record said no


# ---------------------------------------------------------------------------
# Inputs and outputs (small, serialisable; large items travel as artifact IDs)
# ---------------------------------------------------------------------------
@dataclass
class TransitionIn:
    """One transaction: the incident change, its timeline event, up to two action changes, and
    removal of the investigation_queue row when the incident leaves `investigating` or goes
    `open` -> `cancelled`. The second change is the rollback action's (ADR-48). No seq or hash:
    the database sets those."""
    incident_id: str
    transition_id: str            # idempotency key: deterministic per workflow step
    from_state: str
    to_state: str
    expected_version: int
    reason: str
    action_id: Optional[str] = None
    action_from: Optional[str] = None
    action_to: Optional[str] = None
    second_action_id: Optional[str] = None
    second_action_from: Optional[str] = None
    second_action_to: Optional[str] = None


@dataclass
class TransitionOut:
    new_version: int
    event_seq: int


@dataclass
class IncidentIn:
    incident_id: str
    idempotency_key: str


@dataclass
class IncidentSnapshot:
    incident_id: str
    service: str
    environment: str
    version: int
    status: str
    remediation_enabled: bool = False   # false until the recorded decision that opens Release B (ADR-44)


@dataclass
class SpendOut:
    evidence_only: bool           # ledger.fleet.evidence_only(now): the daily limit is reached (ADR-55)


@dataclass
class NotifyIn:
    incident_id: str
    notification_id: str          # idempotency key
    kind: str                     # opened, summary, approval_needed, handed_over, ...
    audience: list[str]
    detail: Optional[str] = None  # e.g. "not yet reviewed" on the first summary


@dataclass
class EvidenceIn(IncidentIn):
    mode: Literal["baseline", "targeted"] = "baseline"
    requests: list[dict] = field(default_factory=list)     # AgentState.pending_requests, targeted only


@dataclass
class SkillIn(IncidentIn):
    """What a skill activity needs beyond the record. review_diagnosis gets only the diagnosis:
    the critic never sees the diagnoser's reasoning (Artifact 9, CritiqueIn)."""
    evidence_ids: list[str] = field(default_factory=list)
    hypotheses: Optional[HypothesesOut] = None
    diagnosis: Optional[DiagnosisDraft] = None
    diagnosis_id: Optional[str] = None
    critic: Optional[CriticVerdict] = None


@dataclass
class EvidenceOut:
    evidence_ids: list[str]
    gap_count: int


@dataclass
class PatchOut:
    action_id: str
    patch_sha256: str
    action_kind: str = "config_revert"
    hold_seconds: int = 600            # on the action: longer per service in the catalogue
    sandbox_available: bool = True     # false in Release A: hand over the untested draft


@dataclass
class TestIn:
    action_id: str
    patch_sha256: str
    test_key: str                 # idempotency key


@dataclass
class TestOut:
    test_run_id: str
    reproduced: bool
    outcome: Literal["passed", "failed", "not_reproduced", "inconclusive", "crashed", "timeout"]
    action_digest: Optional[str] = None   # write-once, set when testing ends (ADR-64)


@dataclass
class ApprovalRequestIn:
    action_id: str
    action_digest: str
    request_key: str              # idempotency key


@dataclass
class ApprovalRequestOut:
    approval_request_id: str
    approvers_needed: int
    rollback_action_id: Optional[str] = None   # the fix's rollback, created here, status `proposed`
    rollback_digest: Optional[str] = None
    conditional_approval: bool = False         # on a rollback's request: one was signed with the fix


@dataclass
class DispatchIn:
    action_id: str                # the execution key; the dispatcher reloads the rest (ADR-47, ADR-51)


@dataclass
class DispatchOut:
    attempt_id: str
    outcome: Literal["applied", "not_applied", "refused", "unknown"]
    external_ref: Optional[str] = None
    refusal_reason: Optional[str] = None       # set with `refused`
    intent_recorded: bool = True               # false on a refusal, or a reconcile that found no intent


@dataclass
class VerifyIn:
    action_id: str
    attempt_id: str
    hold_seconds: int
    verification_key: str         # idempotency key


@dataclass
class VerifyOut:
    verification_id: str
    result: Literal["verified", "failed", "inconclusive"]


@dataclass
class HandOverIn:
    incident_id: str
    reason: str
    handover_key: str


@dataclass
class ReconciliationIn:
    idempotency_key: str


@dataclass
class ReconciliationOut:
    mismatches: int


class NoSlot(Exception):
    """Retryable, and the normal answer while queued: SLOT_RETRY asks again."""


# ---------------------------------------------------------------------------
# Rules the activity implementations apply. Pure, so they can be tested here.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class QueueRow:
    """One investigation_queue row, with its incident's service. `severity` is the confirmed one,
    else the proposed one."""
    incident_id: str
    service: str
    severity: str
    queued_at: datetime
    granted: bool = False


def may_grant(incident_id: str, rows: list[QueueRow]) -> bool:
    """Inside acquire_investigation_slot's locked transaction: fewer than 5 granted, and this
    incident first in order, severity then oldest. A service holding MAX_SLOTS_PER_SERVICE waits, and
    the next eligible incident takes the slot (NFR-44), the one exception to severity order."""
    if sum(r.granted for r in rows) >= MAX_INVESTIGATIONS:
        return False
    held = Counter(r.service for r in rows if r.granted)
    waiting = sorted((r for r in rows if not r.granted and held[r.service] < MAX_SLOTS_PER_SERVICE),
                     key=lambda r: (r.severity, r.queued_at))
    return bool(waiting) and waiting[0].incident_id == incident_id


def precheck_requests(gate: ToolGate, requests: list[dict]) -> tuple[list[dict], list[dict]]:
    """Runs evidence requests through the ToolGate before collect_evidence builds CollectIn.
    Returns (allowed, refused); a refused request becomes a gap, not a SkillFailure."""
    allowed, refused = [], []
    for r in requests:
        try:
            gate.check(r["tool"], r["params"], "collect_evidence")
            allowed.append(r)
        except ToolRefused as e:
            refused.append({**r, "refused": e.code})
    return allowed, refused


def checked_result(skill: str, output, usage: SkillUsage) -> SkillResult:
    """The last check on any model output (ADR-59). A secret is a data risk, so it takes the
    security stop; links and commands are invalid output."""
    try:
        check_output(output)
    except UnsafeOutput as e:
        code = "security_risk" if str(e) == "secret" else "invalid_output"
        return SkillResult(failure=SkillFailure(skill=skill, code=code, detail=f"unsafe model output: {e}"),
                           usage=usage)
    return SkillResult(output=output, usage=usage)


def dispatch_out(result, attempt_id: str = "", external_ref: Optional[str] = None) -> DispatchOut:
    """Maps a Layer 11 dispatcher.DispatchResult. dispatcher.ReleaseInProgress is left to
    propagate: it isn't in READ_RETRY's non-retryable list, so its backoff is the wait."""
    outcome = result.outcome.value
    return DispatchOut(attempt_id, outcome, external_ref,
                       refusal_reason=result.reason if outcome == "refused" else None,
                       intent_recorded=result.intent_recorded)


def db_error(sqlstate: str, message: str) -> Exception:
    """What record_transition raises for a database error. InvalidTransition isn't retried;
    anything else is an outage, and DB_RETRY waits it out."""
    return InvalidTransition(message) if sqlstate in DB_REFUSALS else ConnectionError(message)


def reconciliation_findings(incidents: dict[str, str], running: dict[str, str], queue: set[str],
                            pending_outbox: set[str]) -> tuple[dict[str, int], list[str]]:
    """The 8.9 table. `incidents` maps ID to the record's status, `running` maps ID to a live
    workflow's current_state, `queue` holds investigation_queue IDs, `pending_outbox` the IDs with
    an undelivered start. Returns (mismatches per state, for aisre_workflows_stalled; queue rows
    to remove). Alerts only: the record wins."""
    settled = {"handed_over", "recovered", "closed", "cancelled"}
    stalled: dict[str, int] = {}
    for iid, status in incidents.items():
        live = running.get(iid)
        if live is None:
            orphan = status == "open" and iid not in queue and iid not in pending_outbox
            mismatch = orphan or (status != "open" and status not in settled)
        else:
            mismatch = status in ("closed", "cancelled") or live != status
        if mismatch:
            stalled[status] = stalled.get(status, 0) + 1
    orphans = [iid for iid in sorted(queue) if incidents.get(iid) not in ("open", "investigating")]
    return stalled, orphans


def reconciliation_schedule() -> Schedule:
    """Every 5 minutes, one ReconciliationWorkflow, one activity on the investigation queue."""
    return Schedule(action=ScheduleActionStartWorkflow("ReconciliationWorkflow",
                                                       id=f"{RECONCILIATION_SCHEDULE}-run",
                                                       task_queue=INVESTIGATION_QUEUE),
                    spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=RECONCILIATION_EVERY)]))


async def ensure_reconciliation_schedule(client: Client) -> None:
    await client.create_schedule(RECONCILIATION_SCHEDULE, reconciliation_schedule())


# ---------------------------------------------------------------------------
# Activity interfaces. Implementations: platform modules (Artifact 6, section 6.3).
# ---------------------------------------------------------------------------
@activity.defn(name="load_incident")
async def load_incident(inp: IncidentIn) -> Optional[IncidentSnapshot]:
    raise NotImplementedError


@activity.defn(name="acquire_investigation_slot")
async def acquire_investigation_slot(inp: IncidentIn) -> None:
    """Queues the incident by severity and notifies on-call it is queued; beyond 50 queued the
    incident lead is alerted. Grants by may_grant, else raises NoSlot. Heartbeat not needed:
    each attempt is one short transaction, and cancellation lands between attempts."""
    raise NotImplementedError


@activity.defn(name="read_spend_state")
async def read_spend_state(inp: IncidentIn) -> SpendOut:
    """ledger.fleet.evidence_only(now), read once at investigation start (ADR-55). An activity
    because workflow code does no I/O."""
    raise NotImplementedError


@activity.defn(name="record_transition")
async def record_transition(inp: TransitionIn) -> TransitionOut:
    """Incident Record: check the transition table and expected version, write the status
    changes, timeline event and queue-row removal in one transaction (see TransitionIn).
    Idempotent on transition_id. Database refusals raise through db_error."""
    raise NotImplementedError


@activity.defn(name="notify")
async def notify(inp: NotifyIn) -> None:
    raise NotImplementedError


@activity.defn(name="record_notification_failed")
async def record_notification_failed(inp: NotifyIn) -> None:
    """Writes aisre.notification.failed.v1 and raises an alert, for a failed notification with
    no further transition to take (8.10)."""
    raise NotImplementedError


@activity.defn(name="collect_evidence")
async def collect_evidence(inp: EvidenceIn) -> SkillResult[EvidenceOut]:
    """Evidence Collector: fixed plan or requested tools, after precheck_requests. Parallel
    read-only queries, redaction, gaps. Heartbeats, and stops on cancellation."""
    raise NotImplementedError


@activity.defn(name="retrieve_knowledge")
async def retrieve_knowledge(inp: SkillIn) -> SkillResult[KnowledgeOut]:
    raise NotImplementedError


@activity.defn(name="hypothesise")
async def hypothesise(inp: SkillIn) -> SkillResult[HypothesesOut]:
    """Model Gateway: 2-5 hypotheses and evidence requests. Model rules as for `diagnose`."""
    raise NotImplementedError


@activity.defn(name="diagnose")
async def diagnose(inp: SkillIn) -> SkillResult[DiagnosisResult]:
    """Diagnosis Engine via the Model Gateway: reserves budget, calls the model (up to
    3 attempts inside the gateway), validates, settles. The gateway is the ONLY owner of model
    retries for hypothesise, diagnose, review_diagnosis and draft_patch; each returns
    checked_result, heartbeats through the call, and stops on cancellation."""
    raise NotImplementedError


@activity.defn(name="review_diagnosis")
async def review_diagnosis(inp: SkillIn) -> SkillResult[ReviewResult]:
    """Code checks, then the independent critic. Invalid critic output is ReviewResult(),
    not a failure: the coordinator runs the critique again."""
    raise NotImplementedError


@activity.defn(name="draft_patch")
async def draft_patch(inp: SkillIn) -> SkillResult[PatchOut]:
    """Patch Proposer. No allowed fix is a SkillFailure with code `no_allowed_fix`."""
    raise NotImplementedError


@activity.defn(name="run_sandbox_test")
async def run_sandbox_test(inp: TestIn) -> TestOut:
    """Test Runner: fresh isolated environment, reproduce first, apply, check, destroy.
    Heartbeats; on cancellation it kills and destroys the sandbox before the activity ends."""
    raise NotImplementedError


@activity.defn(name="request_approval")
async def request_approval(inp: ApprovalRequestIn) -> ApprovalRequestOut:
    """For a fix: open the request, set approvers_needed from action.ApprovalPolicy.approvers_needed
    (derived, ADR-49; never read from the action's flag alone), create the rollback action. For a
    rollback: start its conditional approval if one was signed, else open a fresh request."""
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
    """Trusted Dispatcher (dispatcher task queue): reload the action and signatures, verify
    fingerprint, expiry and service state; record the `started` attempt and action `applying`
    in one transaction, then the outcome; apply once, with the action ID as execution key.
    A timeout with no answer is returned as outcome='unknown', never retried blindly."""
    raise NotImplementedError


@activity.defn(name="reconcile_execution")
async def reconcile_execution(inp: DispatchIn) -> DispatchOut:
    """Trusted Dispatcher: look up the execution key in the release process, then compare the
    live service version and configuration with the target. Read-only. Raises a retryable
    error while the release is in progress; no attempt row gives not_applied with
    intent_recorded=False."""
    raise NotImplementedError


@activity.defn(name="verify_recovery")
async def verify_recovery(inp: VerifyIn) -> VerifyOut:
    """Recovery Verifier: reads monitoring directly for the hold period. Heartbeats. The
    implementation counts aisre_verification_outcomes_total with action_kind and category."""
    raise NotImplementedError


@activity.defn(name="hand_over")
async def hand_over(inp: HandOverIn) -> None:
    raise NotImplementedError


@activity.defn(name="reconcile_workflows")
async def reconcile_workflows(inp: ReconciliationIn) -> ReconciliationOut:
    """The 8.9 job: reconciliation_findings over open incidents, running workflows and the
    queue. Sets aisre_workflows_stalled, which raises AisreWorkflowStalled (RB-04), and removes
    orphaned queue rows, its one write."""
    raise NotImplementedError


ALL_INVESTIGATION_ACTIVITIES = [
    load_incident, acquire_investigation_slot, read_spend_state, record_transition, notify,
    record_notification_failed, collect_evidence, retrieve_knowledge, hypothesise, diagnose,
    review_diagnosis, draft_patch, run_sandbox_test, request_approval, escalate_approval,
    close_approval_request, verify_recovery, hand_over, reconcile_workflows,
]
ALL_DISPATCHER_ACTIVITIES = [dispatch_change, reconcile_execution]
