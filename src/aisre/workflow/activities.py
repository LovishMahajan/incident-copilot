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

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal, Optional

from temporalio import activity
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleIntervalSpec,
    ScheduleSpec,
)
from temporalio.contrib.pydantic import pydantic_data_converter

from aisre.agent.agent_contracts import (
    CriticVerdict,
    DiagnosisDraft,
    DiagnosisResult,
    HypothesesOut,
    KnowledgeOut,
    ReviewResult,
    SkillFailure,
    SkillResult,
    SkillUsage,
)
from aisre.agent.tool_registry import ToolGate, ToolRefused
from aisre.agent.validation import UnsafeOutput, check_output, mask_secrets
from aisre.reliability import telemetry
from aisre.workflow.states import InvalidTransition

# Task queues (Artifact 6: the worker process and the dispatcher process)
INVESTIGATION_QUEUE = "aisre-investigation"
DISPATCHER_QUEUE = "aisre-dispatcher"
TEST_RUNNER_QUEUE = "aisre-test-runner"  # run_sandbox_test and the reaper only (ADR-71)

DATA_CONVERTER = pydantic_data_converter
MAX_INVESTIGATIONS = 5  # running at once, across all workers (8.1)
MAX_SLOTS_PER_SERVICE = 4  # NFR-44, provisional: one service can't take every slot
RECONCILIATION_SCHEDULE = "aisre-reconciliation"
RECONCILIATION_EVERY = timedelta(minutes=5)  # provisional (8.9)
RELEASE_RECONCILIATION_SCHEDULE = "aisre-release-reconciliation"
RELEASE_RECONCILIATION_EVERY = timedelta(days=1)  # provisional (Artifact 5.2, ST-40)
RELEASE_RECONCILIATION_WINDOW = timedelta(hours=25)  # each run overlaps the last by an hour
REFUSED_EXPIRED = "approval expired"  # start of the dispatcher's reason when the approval ran out
DB_REFUSALS = {"23514", "23001"}  # check_violation, restrict_violation: the record said no
MAX_PATCH_CANDIDATES = 3  # per diagnosis, one action each, drafted one at a time (ADR-69)
VOIDED = "voided"  # close reason for an approval the dispatcher found tampered or outdated
UNSAFE_EVENT = "aisre.model_output.unsafe"  # the audit line for output stopped by ADR-59
MASKED_COPY_MAX = 4000  # characters of the masked answer kept in that line

audit = logging.getLogger("aisre.audit")


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
    transition_id: str  # idempotency key: deterministic per workflow step
    from_state: str
    to_state: str
    expected_version: int  # the last version the workflow saw; StaleVersion if a person moved it on
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
class SlotIn(IncidentIn):
    run_id: str = ""  # the workflow ID; stored on the investigation_queue row (ADR-69)


@dataclass
class IncidentSnapshot:
    incident_id: str
    service: str
    environment: str
    version: int
    status: str
    remediation_enabled: bool = (
        False  # false until the recorded decision that opens Release B (ADR-44)
    )


@dataclass
class SpendOut:
    evidence_only: bool  # ledger.fleet.evidence_only(now): the daily limit is reached (ADR-55)


@dataclass
class NotifyIn:
    incident_id: str
    notification_id: str  # idempotency key
    kind: str  # opened, summary, approval_needed, handed_over, ...
    audience: list[str]
    detail: Optional[str] = None  # e.g. "not yet reviewed" on the first summary


@dataclass
class EvidenceIn(IncidentIn):
    mode: Literal["baseline", "targeted"] = "baseline"
    requests: list[dict] = field(default_factory=list)  # AgentState.pending_requests, targeted only


@dataclass
class SkillIn(IncidentIn):
    """What a skill activity needs beyond the record. review_diagnosis gets only the diagnosis:
    the critic never sees the diagnoser's reasoning (Artifact 9, CritiqueIn)."""

    evidence_ids: list[str] = field(default_factory=list)
    hypotheses: Optional[HypothesesOut] = None
    diagnosis: Optional[DiagnosisDraft] = None
    diagnosis_id: Optional[str] = None
    critic: Optional[CriticVerdict] = None
    candidate_no: int = (
        1  # draft_patch only: 1 to MAX_PATCH_CANDIDATES, PatchIn.candidate_no (ADR-69)
    )


@dataclass
class EvidenceOut:
    evidence_ids: list[str]
    gap_count: int


@dataclass
class PatchOut:
    action_id: str
    patch_sha256: str
    action_kind: str = "config_revert"
    hold_seconds: int = 600  # on the action: longer per service in the catalogue
    sandbox_available: bool = True  # false in Release A: hand over the untested draft


@dataclass
class TestIn:
    action_id: str
    patch_sha256: str
    test_key: str  # idempotency key


@dataclass
class TestOut:
    test_run_id: str
    reproduced: bool
    outcome: Literal["passed", "failed", "not_reproduced", "inconclusive", "crashed", "timeout"]
    action_digest: Optional[str] = None  # write-once, set when testing ends (ADR-64)
    isolation: Optional[str] = None  # gvisor, or development-runc: the flow, not isolation (ADR-71)


@dataclass
class ReviewIn:
    """open_draft_pull_request: for an action whose test `test_run_id` passed (ADR-72)."""

    action_id: str
    test_run_id: str
    review_key: str  # idempotency key, sent to the review system as Idempotency-Key


@dataclass
class ReviewOut:
    review_ref: str  # repository#number
    review_url: str


@dataclass
class ApprovalRequestIn:
    action_id: str
    action_digest: str
    request_key: str  # idempotency key
    close_reason: Optional[str] = None  # close_approval_request only: expired, withdrawn or voided
    close_detail: Optional[str] = None  # with `voided`: the dispatcher's reason, for the event


@dataclass
class ApprovalRequestOut:
    approval_request_id: str
    approvers_needed: int
    rollback_action_id: Optional[str] = None  # the fix's rollback, created here, status `proposed`
    rollback_digest: Optional[str] = (
        None  # set when the rollback is created, still `proposed`: it is never
    )
    # tested, the one exception to ADR-64's "when testing ends" (ADR-48)
    conditional_approval: bool = False  # on a rollback's request: one was signed with the fix
    action_digest: Optional[str] = None  # the digest sealed when testing ended (ADR-64)


@dataclass
class DispatchIn:
    action_id: str  # the execution key; the dispatcher reloads the rest (ADR-47, ADR-51)


@dataclass
class DispatchOut:
    attempt_id: str
    outcome: Literal["applied", "not_applied", "refused", "unknown"]
    external_ref: Optional[str] = None
    refusal_reason: Optional[str] = None  # set with `refused`
    intent_recorded: bool = True  # false on a refusal, or a reconcile that found no intent
    void_approval: bool = False  # a refusal that voids the approval: ST-01, ST-02, ST-13


@dataclass
class ResolutionIn:
    """POST /v1/actions/{id}/outcome-resolutions, checked by the API, for the dispatcher to write
    (ADR-73: only its role writes attempts and blocks). `auth_time` is ISO 8601, from the token."""

    action_id: str
    subject: str
    outcome: str  # applied | not_applied
    observed_revision: str
    reason: str
    auth_time: str
    mfa: bool
    request_key: str  # idempotency: the API's request


@dataclass
class VerifyIn:
    action_id: str
    attempt_id: str
    hold_seconds: int
    verification_key: str  # idempotency key


@dataclass
class VerifyOut:
    verification_id: str
    result: Literal["verified", "failed", "inconclusive"]
    window_end: Optional[str] = None  # ISO 8601: where the post-change watch starts (T3.17)
    watch_seconds: int = 0  # the post-change watch after a verified fix; 0: none


@dataclass
class WatchIn:
    """One stretch of the post-change watch (11.12, T3.17): from `start` to `end` (ISO 8601),
    returning early on a new flag. `flagged` holds the keys flagged already."""

    action_id: str
    verification_id: str
    start: str
    end: str
    watch_key: str  # idempotency key
    flagged: list[str] = field(default_factory=list)


@dataclass
class WatchFlagOut:
    key: str
    kind: str  # alert_breach | new_error_group
    detail: str


@dataclass
class WatchOut:
    flags: list[WatchFlagOut]
    done: bool  # the watch reached its end


@dataclass
class ReleaseReconciliationIn:
    idempotency_key: str
    since: str  # ISO 8601: the change history from here on


@dataclass
class ReleaseReconciliationOut:
    changes: int
    unmatched: list[str]  # "<deployment> <at> <kind> <revision>" for each unmatched change


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


class StaleVersion(Exception):
    """record_transition's expected_version is behind the record: a person changed the incident
    (a severity confirmation, say) while the workflow ran. Not retried as it stands; the workflow
    reloads the incident and writes again (ADR-69)."""


# ---------------------------------------------------------------------------
# Rules the activity implementations apply. Pure, so they can be tested here.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class QueueRow:
    """One investigation_queue row, with its incident's service. `severity` is the confirmed one,
    else the proposed one, else None, which sorts last as NULL does in the table. `run_id` is the
    workflow run that queued it (ADR-69)."""

    incident_id: str
    service: str
    severity: Optional[str]
    queued_at: datetime
    granted: bool = False
    run_id: str = ""


def may_grant(incident_id: str, rows: list[QueueRow]) -> bool:
    """Inside acquire_investigation_slot's locked transaction: fewer than 5 granted, and this
    incident first in order, severity then oldest. A service holding MAX_SLOTS_PER_SERVICE waits, and
    the next eligible incident takes the slot (NFR-44), the one exception to severity order."""
    if sum(r.granted for r in rows) >= MAX_INVESTIGATIONS:
        return False
    held = Counter(r.service for r in rows if r.granted)
    waiting = sorted(
        (r for r in rows if not r.granted and held[r.service] < MAX_SLOTS_PER_SERVICE),
        key=lambda r: (r.severity is None, r.severity or "", r.queued_at),
    )
    return bool(waiting) and waiting[0].incident_id == incident_id


def precheck_requests(
    gate: ToolGate, requests: list[dict], skill: str
) -> tuple[list[dict], list[dict]]:
    """Runs evidence requests through the ToolGate under the skill that produced them, `hypothesise`
    or `critique`, in that skill's own activity (9.4), so its fixed tool list applies. Returns
    (allowed, refused); a refused request is recorded and dropped, not a SkillFailure."""
    allowed, refused = [], []
    for r in requests:
        try:
            gate.check(r["tool"], r["params"], skill)
            allowed.append(r)
        except ToolRefused as e:
            refused.append({**r, "refused": e.code})
    return allowed, refused


def checked_result(
    skill: str, output, usage: SkillUsage, incident_id: Optional[str] = None
) -> SkillResult:
    """The last check on any model output (ADR-59). A secret is a data risk, so it takes the
    security stop; links and commands are invalid output. Either way, unsafe_result."""
    try:
        check_output(output)
    except UnsafeOutput as e:
        return unsafe_result(skill, e.code, output.model_dump_json(), usage, incident_id)
    return SkillResult(output=output, usage=usage)


def unsafe_result(
    skill: str, kind: str, text: str, usage: SkillUsage, incident_id: Optional[str] = None
) -> SkillResult:
    """Model output that failed the output check, whether here or in the gateway: no retry, and
    the audit copy is masked (ST-23, ST-24). One `aisre.model_output.unsafe` line on the audit
    logger, which the write-once copy keeps (T1.20), with the masked answer and never the text a
    pattern matched; `aisre_model_output_unsafe_total` counts it. For a secret this is the leak
    procedure's start: the security stop hands the incident over at once (ADR-46), and the
    security lead reads the line and rotates what leaked."""
    masked = mask_secrets(text)[:MASKED_COPY_MAX]
    telemetry.model_output_unsafe.labels(task=skill, kind=kind).inc()
    record = {
        "event": UNSAFE_EVENT,
        "time": datetime.now(UTC).isoformat(),
        "incident_id": incident_id,
        "skill": skill,
        "kind": kind,
        "leak_procedure": kind == "secret",
        "masked_output": masked,
    }
    audit.warning(json.dumps(record, sort_keys=True), extra={"aisre": record})
    code = "security_risk" if kind == "secret" else "invalid_output"
    return SkillResult(
        failure=SkillFailure(skill=skill, code=code, detail=f"unsafe model output: {kind}"),
        usage=usage,
    )


def dispatch_out(result, attempt_id: str = "", external_ref: Optional[str] = None) -> DispatchOut:
    """Maps a Layer 11 dispatcher.DispatchResult. dispatcher.ReleaseInProgress is left to
    propagate: it isn't in READ_RETRY's non-retryable list, so its backoff is the wait."""
    outcome = result.outcome.value
    return DispatchOut(
        attempt_id,
        outcome,
        external_ref,
        refusal_reason=result.reason if outcome == "refused" else None,
        intent_recorded=result.intent_recorded,
        void_approval=result.void_approval,
    )


def db_error(sqlstate: str, message: str) -> Exception:
    """What record_transition raises for a database error. InvalidTransition isn't retried;
    anything else is an outage, and DB_RETRY waits it out."""
    return InvalidTransition(message) if sqlstate in DB_REFUSALS else ConnectionError(message)


def reconciliation_findings(
    incidents: dict[str, str],
    running: dict[str, str],
    queue: dict[str, str],
    pending_outbox: set[str],
    live_runs: set[str] = frozenset(),
) -> tuple[dict[str, int], list[str]]:
    """The 8.9 table. `incidents` maps ID to the record's status, `running` maps ID to a live
    workflow's current_state, `queue` maps each investigation_queue row's incident ID to its run
    ID, `pending_outbox` holds the IDs with an undelivered start, `live_runs` the IDs of workflow
    runs still running. Returns (mismatches per state, for aisre_workflows_stalled; queue rows to
    remove). A row goes only when its run has ended: a re-investigation queues while its incident
    is still `handed_over` (ADR-69). Alerts only: the record wins."""
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
    orphans = [
        iid
        for iid in sorted(queue)
        if incidents.get(iid) not in ("open", "investigating") and queue[iid] not in live_runs
    ]
    return stalled, orphans


def reconciliation_schedule() -> Schedule:
    """Every 5 minutes, one ReconciliationWorkflow, one activity on the investigation queue."""
    return Schedule(
        action=ScheduleActionStartWorkflow(
            "ReconciliationWorkflow",
            id=f"{RECONCILIATION_SCHEDULE}-run",
            task_queue=INVESTIGATION_QUEUE,
        ),
        spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=RECONCILIATION_EVERY)]),
    )


def release_reconciliation_schedule() -> Schedule:
    """Daily, one ReleaseReconciliationWorkflow; its activity runs on the dispatcher's queue."""
    return Schedule(
        action=ScheduleActionStartWorkflow(
            "ReleaseReconciliationWorkflow",
            id=f"{RELEASE_RECONCILIATION_SCHEDULE}-run",
            task_queue=INVESTIGATION_QUEUE,
        ),
        spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=RELEASE_RECONCILIATION_EVERY)]),
    )


async def ensure_release_reconciliation_schedule(client: Client) -> None:
    """T3.11: creates the daily schedule unless it exists."""
    try:
        await client.create_schedule(
            RELEASE_RECONCILIATION_SCHEDULE, release_reconciliation_schedule()
        )
    except ScheduleAlreadyRunningError:
        pass


async def ensure_reconciliation_schedule(client: Client) -> None:
    """Creates the schedule unless it exists (the worker calls this at start, T3.10)."""
    try:
        await client.create_schedule(RECONCILIATION_SCHEDULE, reconciliation_schedule())
    except ScheduleAlreadyRunningError:
        pass


# ---------------------------------------------------------------------------
# Activity interfaces. Implementations: platform modules (Artifact 6, section 6.3).
# ---------------------------------------------------------------------------
@activity.defn(name="load_incident")
async def load_incident(inp: IncidentIn) -> Optional[IncidentSnapshot]:
    raise NotImplementedError


@activity.defn(name="acquire_investigation_slot")
async def acquire_investigation_slot(inp: SlotIn) -> None:
    """Queues the incident by severity, under its run ID, and notifies on-call it is queued; beyond
    50 queued the incident lead is alerted. Grants by may_grant, else raises NoSlot. Heartbeat not
    needed: each attempt is one short transaction, and cancellation lands between attempts."""
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
    Idempotent on transition_id. An expected_version behind the record raises StaleVersion;
    other database refusals raise through db_error."""
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
    """Evidence Collector: fixed plan, or the requested tools that passed their producer's
    precheck_requests. Parallel read-only queries, redaction, gaps. Each record is stored with
    flagged_injection set by validation.flags_injection, and a flagged one writes
    aisre.evidence.injection_flagged.v1 (ST-33). Heartbeats, and stops on cancellation."""
    raise NotImplementedError


@activity.defn(name="retrieve_knowledge")
async def retrieve_knowledge(inp: SkillIn) -> SkillResult[KnowledgeOut]:
    raise NotImplementedError


@activity.defn(name="hypothesise")
async def hypothesise(inp: SkillIn) -> SkillResult[HypothesesOut]:
    """Model Gateway: 2-5 hypotheses and evidence requests, which it pre-checks under
    `hypothesise` (precheck_requests). Model rules as for `diagnose`."""
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
    """Code checks, then the independent critic, whose evidence requests it pre-checks under
    `critique` (precheck_requests). Writes the diagnosis's final_outcome and final_confidence,
    once, from ReviewResult.stored_fields (ADR-69). Invalid critic output is ReviewResult(),
    not a failure: the coordinator runs the critique again."""
    raise NotImplementedError


@activity.defn(name="draft_patch")
async def draft_patch(inp: SkillIn) -> SkillResult[PatchOut]:
    """Patch Proposer: candidate `candidate_no` for the diagnosis, as its own action (ADR-69).
    The action's hold_seconds is never below its service's (action.check_hold). No allowed fix is
    a SkillFailure with code `no_allowed_fix`."""
    raise NotImplementedError


@activity.defn(name="run_sandbox_test")
async def run_sandbox_test(inp: TestIn) -> TestOut:
    """Test Runner (TEST_RUNNER_QUEUE, its own process; ADR-71): fresh isolated environment,
    reproduce first, apply, check, destroy. Heartbeats; on cancellation it kills and destroys the
    sandbox before the activity ends. Implementation: aisre.remediation.test_runner."""
    raise NotImplementedError


@activity.defn(name="open_draft_pull_request")
async def open_draft_pull_request(inp: ReviewIn) -> ReviewOut:
    """Release A (ADR-44, FR-18): a draft pull request in the review system for a fix whose
    failure reproduced and whose test passed, with the patch, FR-19's fields and the test result
    with its isolation label. Never merged by the platform. Records `patches.review_ref` once and
    `aisre.action.review_opened.v1`. Implementation: aisre.remediation.review."""
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
    """Close an open request with `close_reason`: expired, withdrawn or voided. Idempotent; a
    request already closed keeps the reason it has, which is write-once. `voided` also writes
    aisre.approval.voided.v1 with `close_detail`: the dispatcher's role can't write that event
    (ADR-51), so it reports void_approval and the workflow asks here."""
    raise NotImplementedError


@activity.defn(name="dispatch_change")
async def dispatch_change(inp: DispatchIn) -> DispatchOut:
    """Trusted Dispatcher (dispatcher task queue): reload the action and signatures, verify
    fingerprint, expiry, service state, the fence and that no stop stands against the incident
    (ADR-69); record the `started` attempt and action `applying`
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


@activity.defn(name="resolve_outcome")
async def resolve_outcome(inp: ResolutionIn) -> DispatchOut:
    """Trusted Dispatcher (11.11, T3.16): record the incident lead's outcome, which clears the
    deployment's block. Re-checks the lead's rights, sign-in and two-factor. Refusals are not
    retried. Implementation: aisre.remediation.dispatcher_process."""
    raise NotImplementedError


@activity.defn(name="verify_recovery")
async def verify_recovery(inp: VerifyIn) -> VerifyOut:
    """Recovery Verifier: reads monitoring directly for the hold period. Heartbeats. The
    implementation counts aisre_verification_outcomes_total with action_kind and category.
    Implementation: aisre.verification.verifier (T3.8)."""
    raise NotImplementedError


@activity.defn(name="watch_after_change")
async def watch_after_change(inp: WatchIn) -> WatchOut:
    """Post-change watch (11.12, T3.17): flags new error groups and breaches of the recovery
    criteria after the hold period, as `aisre.verification.watch_flagged.v1` linked to the
    action. Never rolls back. Heartbeats. Implementation: aisre.verification.verifier."""
    raise NotImplementedError


@activity.defn(name="reconcile_releases")
async def reconcile_releases(inp: ReleaseReconciliationIn) -> ReleaseReconciliationOut:
    """Trusted Dispatcher (T3.11, ST-40): the release process's change history against
    `execution_attempts`. A change with no matching attempt is a security alert. Read-only.
    Implementation: aisre.remediation.dispatcher_process."""
    raise NotImplementedError


@activity.defn(name="hand_over")
async def hand_over(inp: HandOverIn) -> None:
    raise NotImplementedError


@activity.defn(name="reconcile_workflows")
async def reconcile_workflows(inp: ReconciliationIn) -> ReconciliationOut:
    """The 8.9 job: reconciliation_findings over open incidents, running workflows and the
    queue. Sets aisre_workflows_stalled, which raises AisreWorkflowStalled (RB-04), and removes
    queue rows whose run has ended, its one write."""
    raise NotImplementedError


ALL_INVESTIGATION_ACTIVITIES = [
    load_incident,
    acquire_investigation_slot,
    read_spend_state,
    record_transition,
    notify,
    record_notification_failed,
    collect_evidence,
    retrieve_knowledge,
    hypothesise,
    diagnose,
    review_diagnosis,
    draft_patch,
    open_draft_pull_request,
    request_approval,
    escalate_approval,
    close_approval_request,
    verify_recovery,
    watch_after_change,
    hand_over,
    reconcile_workflows,
]
ALL_DISPATCHER_ACTIVITIES = [
    dispatch_change,
    reconcile_execution,
    resolve_outcome,
    reconcile_releases,
]
ALL_TEST_RUNNER_ACTIVITIES = [run_sandbox_test]  # with the reaper's reap_sandboxes (ADR-71)
