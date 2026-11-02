# Code Changes Required

The reference code in each layer's `files/` folder is kept as the team wrote it. These are the 308 changes needed for it to match the [decision register](decisions.md) and the artifacts. None has been made.

## Do these first

| Why | File | Change | Decision |
| --- | --- | --- | --- |
| Every model request fails | `layer-10/files/adapters.py` | Stop sending `temperature: 0`, which the proposed Claude models reject | Layer 10 decision |
| A bad patch can read as tested | `layer-11/files/sandbox.py` | `classify` must return `crashed` for a non-zero harness exit, and `inconclusive` for a missing result or an empty required suite | Layer 11 C13 |
| The agent never runs | `layer-08/files/workflow.py` | Drive the investigation from `coordinator.next_step` | ADR-52 |
| Sandbox can't start | `layer-11/files/sandbox.py` | Remove `--pid=private` and `--uts=private`, which Docker refuses; apply `workspace_mb` and `wall_clock_s` | Layer 11 C14 |
| State model differs | `layer-07/files/contracts.py`, `layer-08/files/states.py` | Apply ADR-46 and ADR-64 together, and load `schema.sql` | ADR-46, ADR-64 |
| Duplicate-change risk | `layer-08/files/workflow.py`, `layer-08/files/activities.py`, `layer-11/files/dispatcher.py` | Use the action ID as the execution key; remove `is_undo` | ADR-47, ADR-48 |
| Limits missing | `layer-10/files/gateway.py` | Enforce 16,000 input tokens per call, and the daily and monthly limits | ADR-56, ADR-61 |
| Weak release gates | `layer-12/files/eval_harness.py` | Add the 80% floor and the honesty, conflicting-evidence and security gates | ADR-65 |

## Full list

Grouped by the layer that found the change. Exact duplicates are removed; near-duplicates between layers can remain.


### Cross-layer corrections (these win where rows overlap)

| File | Change | Decision |
| --- | --- | --- |
| layer-08-workflows-state-machines/files/workflow.py | Map stops: only a security or data-risk stop goes `investigating` → `handed_over`. Time, budget and invalid-output limits, and a `notify` failure in `investigating`, go through `inconclusive`. Supersedes layer-08 row "Map stop verdicts" and layer-09 row "Map stops to transitions" | ADR-46 |
| layer-08-workflows-state-machines/files/workflow.py | 24-hour limit from `open` or `investigating`: `investigating` → `inconclusive` → `handed_over` | ADR-46 |
| layer-08-workflows-state-machines/files/test_workflow.py | Time-stop and budget-stop tests assert the path through `inconclusive`. Add one test that only a security stop goes direct. Supersedes the layer-08 "time stop goes direct" test | ADR-46 |
| layer-09-agent-architecture/files/coordinator.py | The stop kind separates security or data-risk stops from all others. `budget_refused` and `deadline_passed` no longer take a direct path | ADR-46 |
| layer-08-workflows-state-machines/files/workflow.py | New `MODEL_RETRY` policy (`maximum_attempts = 2`) on `hypothesise`, `diagnose`, `review_diagnosis`, `draft_patch`. No workflow-scheduled re-run. Supersedes layer-08 rows "Re-run a model step once" and the `NO_RETRY` part of the timeouts row | ADR-27, ADR-52 |
| layer-08-workflows-state-machines/files/workflow.py | `hypothesise`, `review_diagnosis`, `draft_patch` start-to-close stay 300 s. Supersedes the 200 s in the layer-09 timeouts row | Artifact 8, 8.5 |
| layer-08-workflows-state-machines/files/workflow.py | Drop `_record_action` for `executed`, `not_applied`, `outcome_unknown` on the dispatch path; the dispatcher writes them under `aisre_dispatcher` | ADR-51 |
| layer-11-safe-remediation/files/dispatcher.py | Write action `applying` with intent, then its outcomes, in the same transaction as the attempt row, using `status` and `version` only | ADR-51 |
| layer-11-safe-remediation/files/dispatcher.py | `reconcile`: while the release says in progress, raise a retryable error so `READ_RETRY` provides the wait, up to the 10-minute limit. `max_checks = 10` and `wait_s` go | Q8.34, Artifact 8, 8.9 |
| layer-08-workflows-state-machines/files/workflow.py | After a rollback applies, run `verify_recovery` on the rollback action while the incident stays `not_recovered`, or `reconciling` after an unknown outcome. Report the result in the hand-over; it triggers nothing. Rollback action `executed` → `verified` on verdict `verified` | ADR-48 |
| layer-08-workflows-state-machines/files/workflow.py | A late rejection on an `approved` action moves it to `withdrawn` | ADR-49 |
| layer-07-data-apis-events/files/contracts.py | Add `OutcomeResolutionCreate` (outcome, observed revision, reason) and the `aisre.notification.failed.v1` data model | ADR-49, ADR-47 |
| layer-11-safe-remediation/files/action.py | Add `fence:set` for Incident lead and Platform admin. `platform:operate` covers lifting a fence and running a restore, Platform admin only | ADR-50 |
| layer-12-reliability-evaluation/files/fence.py | Manual fence: settable by Incident lead or Platform admin, records who and why. Lift by Platform admin with a lead's sign-off | ADR-50 |
| layer-12-reliability-evaluation/files/eval_harness.py | `Gates.min_heldout_cases = 280`, plus 400 total and 12 categories | ADR-65 |
| layer-08-workflows-state-machines/files/workflow.py | Reconciliation job: Release A onward; mismatches count in `aisre_workflows_stalled`, which raises `AisreWorkflowStalled` and routes to RB-04. Supersedes the layer-08 row adding a new alert routed to RB-07 | Artifact 8, 8.9; Artifact 12, 12.4 |
| layer-07-data-apis-events/files/schema.sql | `investigation_queue` (Layer 8, 8.1) and the fence record (Layer 12, 12.5) have no table. ADR-64 fixes the count at 30. Needs a decision before either table is added | ADR-64 |

### Found in Layer 2

| File | Change | Decision |
| --- | --- | --- |
| `layer-10-inference-retrieval/files/gateway.py` | Keep `max_cost_micros = 2_000_000`, but add the global limits beside it: monthly ceiling `500_000_000` micros and daily limit monthly ÷ 30, in USD, with alerts at 80% and evidence-only at 100% | ADR-56, ADR-55 |
| `layer-12-reliability-evaluation/files/telemetry.py` | Set `budget_daily_limit` from the same daily limit value | ADR-56 |
| Isolated test environment runner (M2, no reference file exists) | Cap disposable copies at 3 at once, queue beyond that | ADR-56 |

### Found in Layer 3

| File | Change | Decision |
| --- | --- | --- |
| `layer-09-agent-architecture/files/validation.py` | Replace kind-based `STRENGTH` and `score_confidence` with `strength_tier` per observation and the High, Medium, Low rules | ADR-53 |
| `layer-10-inference-retrieval/files/context.py` | Rename `EVIDENCE_PRIORITY`, and remove its citation of Artifact 3 and `validation.STRENGTH` | ADR-53 |
| `layer-09-agent-architecture/files/validation.py` | Add a required-sources check: alert, change history, and logs or error tracker. Without them the result is inconclusive | ADR-54 |
| `layer-07-data-apis-events/files/schema-tables.sql` | `services` has no `critical` or per-service hold period, which the fixture's catalogue entry and ADR-49, ADR-66 need | ADR-49, ADR-66 |

### Found in Layer 4

| File | Change | Decision |
| --- | --- | --- |
| `layer-10-inference-retrieval/files/gateway.py` | Add a daily limit ($16.67) and a monthly ceiling ($500), checked before each call. At 100% of daily, new investigations are evidence-only and running ones finish their current step, then stop and hand over. Source `max_cost_micros` from ADR-56 | ADR-55, ADR-56 (NFR-14, NFR-42, NFR-43) |
| `layer-12-reliability-evaluation/files/alerts.yml` | `AisreDailySpend80` is the only spend-level rule. Add daily 100% and monthly 50%, 80%, 100% rules | ADR-55, ADR-56 (NFR-14, NFR-43) |
| `layer-12-reliability-evaluation/files/telemetry.py` | Add monthly spend and ceiling gauges; only daily gauges exist | ADR-56 (NFR-43) |
| Intake (no module in `files/` yet) | Store rejected submissions in `dead_letters` with the reason and notify the incident lead. Unknown service: 422 for monitoring, the same 404 as a forbidden service for a person. Missing fingerprint falls back to service plus alert name | ADR-58 (FR-05, FR-06, FR-42) |
| Scheduler (no module in `files/` yet) | Hold one service to 4 of the 5 investigation slots; skip to the next eligible queued incident by severity | Layer 4 decision (NFR-44) |
| Intake (no module in `files/` yet) | Join same-service alerts to the open incident within 10 minutes of its opening | Layer 4 decision (FR-43) |

### Found in Layer 5

| File | Change | Decision |
| --- | --- | --- |
| `tests/security/` (new) | Add the planned files test_approval, test_patch_integrity, test_credentials, test_authz, test_model_output, test_sandbox, test_links, test_audit, test_injection, test_limits, test_execution, test_rollback, implementing ST-01 to ST-40 | Layer 5 decision: forty security tests |
| `layer-11-safe-remediation/files/action.py` | `record_decision`: rejection needs authentication only, not recent sign-in or two-factor | ADR-49 |
| `layer-11-safe-remediation/files/action.py` | `record_decision`: refuse unless the request is open and the action is in `awaiting_approval` (ST-12) | ADR-46, ADR-49 |
| `layer-11-safe-remediation/files/action.py` | Refuse a sole approver who authored the change being reverted (ST-07) | ADR-49 |
| `layer-11-safe-remediation/files/action.py` | Write `aisre.approval.refused.v1` and raise a security alert on every refused attempt | ADR-49 |
| `layer-11-safe-remediation/files/action.py` | `MATRIX`: remove `MANAGE_SANDBOX` from Platform admin; align roles with the ADR-50 matrix, including note, re-investigation and audit-export permissions and Monitoring | ADR-50 |
| `layer-11-safe-remediation/files/dispatcher.py` | `execute`: after `apply` returns, compare the live revision with `expected_after_revision`; disagreement is an unknown outcome (ST-40) | ADR-47, Layer 5 decision: release process compromise |
| `layer-09-agent-architecture/files/validation.py` | Add output checks: secrets (fail, retry, mask stored copy), links, commands, and "a claim citing only flagged evidence is unsupported" | ADR-59 |
| Release reconciliation job (new) | Daily comparison of change history with `execution_attempts`; security alert on an unmatched change (provisional) | Layer 5 decision: release process compromise |
| Runbook for signing key theft (new) | Retire the public key from the dispatcher's key set and apply the `manual` fence (provisional) | Layer 5 decision: stolen signing key |

### Found in Layer 6

| File | Change | Decision |
| --- | --- | --- |
| `layer-08-workflows-state-machines/files/activities.py`, `workflow.py` | `DispatchIn` carries `approval_id`, `execution_key` and `is_undo`. Reduce it to `action_id`; the dispatcher reloads everything else from trusted storage | ADR-51, Layer 6 conflict 2 (also ADR-47, ADR-48) |
| `layer-07-data-apis-events/files/schema.sql` (proposed) | Define the dispatcher database role: read actions, patches, approvals; write `execution_attempts` and its own timeline events. `schema-tables.sql` has no role grants | ADR-51, Layer 6 conflict 3 |

### Found in Layer 7

| File | Change | Decision |
| --- | --- | --- |
| layer-07 files/contracts.py | `IncidentStatus`: add `testing`, `reconciling`. Add `ActionStatus` (14 states) and use it for `ActionView.status` | ADR-46, ADR-64 |
| layer-07 files/contracts.py | `ActionKind`: add `rollback`. Add `ArtifactKind` and use it for `ArtifactView.kind`. Mirror every enum that crosses a boundary | ADR-64 |
| layer-07 files/contracts.py | `EvidenceRecord`: add `tool`, `source_as_of`, `gap_reason`, `strength_tier` | ADR-64 |
| layer-07 files/contracts.py | `Hypothesis`: add `category`, `claims`. `Diagnosis`: add `category`, `claims`, `ruled_out`, `reviewed`, `review_notes`, `produced_by` | ADR-64 |
| layer-07 files/contracts.py | `ActionView`: add `action_digest`, `parent_action_id`, `target_deployment`, `base_revision`; keep slugs | ADR-47, ADR-48, ADR-64 |
| layer-07 files/contracts.py | `PatchView.diff_sha256` renamed `patch_sha256` | ADR-64 |
| layer-07 files/contracts.py | `TestRunView.outcome`: `passed`, `failed`, `not_reproduced`, `inconclusive`, `crashed`, `timeout` | ADR-64 |
| layer-07 files/contracts.py | `ModelDiagnosisOutput`, `ModelHypothesis`: remove, replaced by layer-09 `DiagnosisDraft` | ADR-52 |
| layer-07 files/contracts.py | `ApprovalCreate`: remove `expected_action_version`; replace `patch_sha256` with `action_digest`. `RejectionCreate`: same digest change | ADR-49, ADR-64 |
| layer-07 files/contracts.py | `ApprovalDecisionView`: `decision` adds `revoked`; add `action_digest`, `revokes_approval_id`, `conditional` | ADR-48, ADR-49, ADR-64 |
| layer-07 files/contracts.py | Add `RevocationCreate`, `ReinvestigationCreate`, `NoteCreate`, `SeverityConfirmationCreate`, `ClosureCreate`, `Page[T]` | ADR-46, ADR-49, ADR-50, Layer 7 conflict "Bodies with no contract" |
| layer-07 files/contracts.py | `ApprovalGrantedData`: add `action_digest`. `VerificationCompletedData.result`: `verified`, `failed`, `inconclusive` | ADR-49, ADR-64 |
| layer-07 files/contracts.py | Add one `...Data` model per event type in 7.5, including `aisre.approval.refused.v1`, `aisre.approval.revoked.v1`, `aisre.execution.refused.v1`, `aisre.incident.reinvestigation_requested.v1`, `aisre.incident.note_added.v1` | ADR-49, Layer 7 conflict "Only four event payload models" |
| layer-07 files/contracts.py | Docstring: field names match `schema.sql`, not `schema-tables.sql`; `CloudEvent` maps through the `timeline_cloudevents` view | ADR-64 |
| layer-08 files/states.py | Add `handed_over` → `investigating`, `not_recovered` → `reconciling`, `proposed` → `awaiting_approval`. `schema.sql` already has them | ADR-46 |
| layer-08 files/activities.py, workflow.py | Writers rely on the database for `seq`, `prev_hash`, `hash`; send no hash. Map check_violation (23514) and restrict_violation (23001) to `InvalidTransition` | ADR-46, ADR-64 |
| layer-09 files/agent_contracts.py | `DiagnosisDraft` stored through `category`, `claims`, `ruled_out` as in `schema.sql` | ADR-52, ADR-64 |
| layer-10 files/gateway.py | Replace the in-memory budget with `budget_reservations` inserts and updates. Settle sends actual tokens; unknown usage settles at `reserved_cost_micros` | ADR-61 |
| layer-10 files/gateway_contracts.py | `ResultStatus` and `StopReason` values are now PostgreSQL enums; change them together | Layer 7 conflict "model_attempt_status" |
| layer-10 files/retrieval.py | Load documents and chunks from PostgreSQL. `chunk_id` text becomes `document_id` plus `ordinal`; revision ID moves to `index_revisions.revision_hash` (full SHA-256) and also hashes permitted groups and build date | ADR-62 |
| layer-11 files/action.py | `kind`: use `config_revert`, `commit_revert`, `release_rollback`, `rollback`, not `apply_patch`. `Decision`: add `conditional`, `conditional_on_approval_id`; conditional `valid_until` = parent's + hold + 15 minutes | ADR-48, ADR-64 |
| layer-11 files/dispatcher.py | Use `execution_attempts` as the ledger. `Outcome.REFUSED` writes `aisre.execution.refused.v1` and no attempt. Reconciliation inserts a row with `reconciliation = true`. Connect as `aisre_dispatcher` | ADR-47, ADR-51 |
| new Alembic migration | First migration is `schema.sql`; provision `aisre_app`, `aisre_dispatcher`, `aisre_retention` credentials outside it | ADR-64 |
| new retention job | Read `artifacts_due_for_deletion`, delete content, write `artifact_deletions`, then call `purge_incident`; also delete delivered outbox rows, expired idempotency keys and expired dead letters. Connect as `aisre_retention` | ADR-57 |

### Found in Layer 8

| File | Change | Decision |
| --- | --- | --- |
| `layer-08-workflows-state-machines/files/states.py` | Add `HANDED_OVER → INVESTIGATING`, `NOT_RECOVERED → RECONCILING`, and action `PROPOSED → AWAITING_APPROVAL` (rollback kind only). Comment on `NOT_RECOVERED`: "undo" becomes "rollback" | ADR-46, ADR-48 |
| `layer-08-workflows-state-machines/files/states.py` | `check_action` must refuse `PROPOSED → AWAITING_APPROVAL` for a non-rollback action; takes the action kind | ADR-46 |
| `layer-08-workflows-state-machines/files/workflow.py` | `__init__` sets `state = OPEN`; take the state from `IncidentSnapshot.status`, so a re-investigation run starts at `handed_over` | ADR-46 C8 |
| `layer-08-workflows-state-machines/files/workflow.py` | Replace the fixed collect, retrieve, diagnose, review sequence with the `coordinator.next_step` loop; each step is one activity; feed results back | ADR-52, Layer 8 C4 |
| `layer-08-workflows-state-machines/files/workflow.py` | Map stop verdicts: time, budget, security → `investigating → handed_over`; other stops → `inconclusive → handed_over`. The limit path no longer goes through `inconclusive` | ADR-46, ADR-52 |
| `layer-08-workflows-state-machines/files/workflow.py` | Re-run a model step once when its attempt ended in a timeout, keeping `maximum_attempts = 1`; don't re-run typed failures | ADR-27, ADR-52 C4 |
| `layer-08-workflows-state-machines/files/workflow.py` | Release A path: read `remediation_enabled`; on `passed`, `testing → handed_over`, action `testing → withdrawn` with reason `release_a_recommendation`; on `sandbox_available = false`, `diagnosed → handed_over`, action `proposed → withdrawn` | ADR-44, ADR-46 |
| `layer-08-workflows-state-machines/files/workflow.py` | `draft_patch` returning "no allowed fix", or failing: `diagnosed → handed_over` with no action | ADR-46 C3 |
| `layer-08-workflows-state-machines/files/workflow.py` | `approval_decided` keeps a set of decisions keyed by person; wait for `approvers_needed` valid decisions (from `ApprovalRequestOut`); any valid rejection wins; a `revoked` decision removes an approval; drop approvals older than `APPROVAL_VALID_FOR` | ADR-49 C9, gap 8.6 |
| `layer-08-workflows-state-machines/files/workflow.py` | `ApprovalSignal`: rename `approval_id` to `decision_id`, replace `patch_sha256` with `action_digest`; add `revoked` to decisions; compare the digest in the handler | ADR-49, ADR-64 |
| `layer-08-workflows-state-machines/files/workflow.py` | Remove `_record_action(A.APPLYING)`. Handle `refused`: action `approved → expired` (approval past `valid_until`) or `approved → withdrawn` (revoked, cancelled, service changed); incident `executing → handed_over` | C18, C3, C11, ADR-49 |
| `layer-08-workflows-state-machines/files/workflow.py` | Update the comment "returns not_applied" on dispatcher refusals; drop outcome `failed`; use `refused` | Layer 11, C11 |
| `layer-08-workflows-state-machines/files/workflow.py` | `dispatch_change` raising `ActivityError`: `executing → reconciling` with no action change; apply the action result after reconciliation; no intent row means `approved → withdrawn`. `reconcile_execution` raising is treated as still unknown, not re-raised | 8.10 gap, ADR-47 |
| `layer-08-workflows-state-machines/files/workflow.py` | `verify_recovery` raising `ActivityError` hands over as inconclusive, with no rollback | ADR-66, 8.10 gap |
| `layer-08-workflows-state-machines/files/workflow.py` | Rename verdicts to `verified`, `failed`, `inconclusive`. `inconclusive` → `verifying → handed_over`, no rollback; only `failed` → `not_recovered` | ADR-64, ADR-66 C17 |
| `layer-08-workflows-state-machines/files/workflow.py` | Retire `exec:<approval_id>` and `undo:<approval_id>`; `DispatchIn` carries only the action ID, which is the execution key | ADR-47, ADR-51 C15 |
| `layer-08-workflows-state-machines/files/workflow.py` | Rewrite the failed-fix path: rollback action with its own ID, conditional or fresh approval, `not_recovered → reconciling` on unknown, then `handed_over`; never `reconciling → verifying` for a rollback; parent `executed → undone` | ADR-46, ADR-48 C10 |
| `layer-08-workflows-state-machines/files/workflow.py` | Fresh rollback approval wait in `not_recovered`: escalate at 15 min, expire at 60 min, handle rejection and expiry | ADR-48, ADR-49 |
| `layer-08-workflows-state-machines/files/workflow.py` | Withdraw the rollback action whenever the fix ends without a `failed` verdict | ADR-48 |
| `layer-08-workflows-state-machines/files/workflow.py` | Replace `RECOVERY_HOLD_SECONDS` with the action's `hold_seconds`; timeout is hold + 300 s | ADR-64, ADR-66 |
| `layer-08-workflows-state-machines/files/workflow.py` | Add a whole-run 24-hour timer: non-terminal, non-waiting states hand over after any write in flight; `open` goes through `investigating`; `recovered` stays | ADR-46 C12 |
| `layer-08-workflows-state-machines/files/workflow.py` | `_wait_for_close`: remove `_to(I.CLOSED)`; a `close` signal ends the run; add `reinvestigate` to `LeadSignal.kind`, which ends the run without a write; fix the comment "stays handed over" | ADR-46 C8, C12 |
| `layer-08-workflows-state-machines/files/workflow.py` | Prefix transition, notification, hand-over and step keys with `<run>`; add `run` to `IncidentWorkflowInput` | ADR-46 C16 |
| `layer-08-workflows-state-machines/files/workflow.py` | Pass an idempotency key on every activity input, including `IncidentIn` and `VerifyIn` | C16 |
| `layer-08-workflows-state-machines/files/workflow.py` | Add `acquire_investigation_slot` before `open → investigating`, with `SLOT_RETRY`; start `INVESTIGATION_LIMIT` at the grant; notify `queued` | C13 |
| `layer-08-workflows-state-machines/files/workflow.py` | `_act` for `record_transition`, `load_incident`, `hand_over`: set no schedule-to-close limit. `DB_RETRY` comment updated. Keep within the 24-hour limit | C5 |
| `layer-08-workflows-state-machines/files/workflow.py` | `_investigation_act`: schedule-to-close = min(4× start-to-close, remaining). `_protected`: set 4× start-to-close | C6, Q8.18 |
| `layer-08-workflows-state-machines/files/workflow.py` | `reconcile_execution` start-to-close 150 s (schedule-to-close 600 s); `draft_patch` 300 s; `review_diagnosis` 300 s with `NO_RETRY`; `hypothesise` 300 s with `NO_RETRY` | C6, ADR-27, ADR-52 |
| `layer-08-workflows-state-machines/files/workflow.py` | Heartbeat timeout 20 s on `collect_evidence`, `hypothesise`, `diagnose`, `review_diagnosis`, `draft_patch`, `run_sandbox_test` | C4, C7 |
| `layer-08-workflows-state-machines/files/workflow.py` | Keep a handle on the running cancellable activity; `cancellation_requested` and Temporal cancel call `.cancel()` on it, with `WAIT_CANCELLATION_COMPLETED` so sandbox destruction finishes first | C7 |
| `layer-08-workflows-state-machines/files/workflow.py` | Shield `record_transition` from cancellation in every state, so `run` handles the cancel only after the outcome is recorded | C20 |
| `layer-08-workflows-state-machines/files/workflow.py` | Catch exhausted activities outside `_investigation_act` per the 8.10 table: `draft_patch`, `run_sandbox_test`, `notify`, `request_approval`, `escalate_approval`, `close_approval_request` | 8.10 gap |
| `layer-08-workflows-state-machines/files/workflow.py` | `_finish_cancelled`: remove `A.APPROVED`, which can't be reached at a safe point | C3, ADR-49 |
| `layer-08-workflows-state-machines/files/workflow.py` | Module and `TransitionIn` comments: "undo" becomes "rollback" throughout | ADR-48 |
| `layer-08-workflows-state-machines/files/activities.py` | Module docstring stays true only after: `IncidentIn` gains `idempotency_key`; `VerifyIn` gains `verification_key` | C16 |
| `layer-08-workflows-state-machines/files/activities.py` | `IncidentSnapshot` gains `remediation_enabled`; `PatchOut` gains `sandbox_available` and a "no allowed fix" result | ADR-44, ADR-46 |
| `layer-08-workflows-state-machines/files/activities.py` | Add `acquire_investigation_slot`, `hypothesise`, `HypothesesOut`, `EvidenceIn`; `collect_evidence` takes `EvidenceIn` | C13, ADR-52 |
| `layer-08-workflows-state-machines/files/activities.py` | `collect_evidence`, `retrieve_knowledge`, `hypothesise`, `diagnose`, `review_diagnosis`, `draft_patch` return a typed failure rather than raise; `diagnose` docstring names all model activities | ADR-52 |
| `layer-08-workflows-state-machines/files/activities.py` | `DispatchIn`: remove `approval_id`, `execution_key`, `is_undo`. Fix the comment "one per approval" | ADR-47, ADR-48, ADR-51 |
| `layer-08-workflows-state-machines/files/activities.py` | `DispatchOut.outcome`: `applied`, `not_applied`, `refused`, `unknown`; add `refusal_reason` and `intent_recorded` | Layer 11, 8.10 gap |
| `layer-08-workflows-state-machines/files/activities.py` | `dispatch_change` records `started` and action `applying` in one transaction; update its docstring | C18 |
| `layer-08-workflows-state-machines/files/activities.py` | `reconcile_execution`: raise a retryable error while in progress; return "no intent" when no attempt row exists | C6, 8.10 gap |
| `layer-08-workflows-state-machines/files/activities.py` | `TestOut.outcome`: the six ADR-64 values, replacing `error` | ADR-64 |
| `layer-08-workflows-state-machines/files/activities.py` | `VerifyOut.result`: `verified`, `failed`, `inconclusive`; `ApprovalRequestIn` drops default `approvers_needed = 1`; `ApprovalRequestOut` gains `approvers_needed`, `rollback_action_id`; `action_digest` replaces `patch_sha256` | ADR-48, ADR-49, ADR-64 |
| `layer-08-workflows-state-machines/files/activities.py` | `TransitionIn` carries a second action change for the rollback: rollback action and parent `executed → undone` in one transaction | ADR-48 |
| `layer-08-workflows-state-machines/files/activities.py` | `record_transition` deletes the `investigation_queue` row when the incident leaves `investigating` or goes `open → cancelled` | C13 |
| `layer-08-workflows-state-machines/files/activities.py` | Long activities heartbeat and honour cancellation; `run_sandbox_test`'s Test Runner kills and destroys the sandbox on cancel | C7 |
| `layer-08-workflows-state-machines/files/activities.py` | Add the reconciliation activity and Temporal schedule `aisre-reconciliation`, every 5 minutes | Gap 8.9 |
| `layer-08-workflows-state-machines/files/test_workflow.py` | Add `test_temporal_cancel_during_transition_write_records_outcome`: cancel during `record_transition` in `executing` and `reconciling`; the outcome is recorded (required) | C20 |
| `layer-08-workflows-state-machines/files/test_workflow.py` | Happy-path transition list: remove the second `executing`; closure no longer in `f.transitions`; fakes and `approve()` use new signal and activity fields | C18, ADR-49 |
| `layer-08-workflows-state-machines/files/test_workflow.py` | Rename `test_failed_verification_undoes_and_hands_over`; assert a rollback action with its own key; `dispatch_undo` counter and `is_undo` removed | ADR-47, ADR-48 |
| `layer-08-workflows-state-machines/files/test_workflow.py` | Add tests: second approver waits; first approval aged out; rejection after one approval; verification inconclusive hands over without rollback; time stop goes direct to `handed_over` | ADR-46, ADR-49, ADR-66 |
| `layer-08-workflows-state-machines/files/test_workflow.py` | Add tests: unknown rollback goes `not_recovered → reconciling → handed_over`; fresh rollback approval expires; `refused` maps to `expired` and `withdrawn`; dispatcher activity failure reconciles; no-intent reconcile | ADR-46, ADR-48, 8.10 gap |
| `layer-08-workflows-state-machines/files/test_workflow.py` | Add tests: database outage longer than 40 s pauses and resumes; each 8.10 exhaustion row; 24-hour limit per state; `recovered` stays; closure after run end | C5, C12, 8.10 gap |
| `layer-08-workflows-state-machines/files/test_workflow.py` | Add tests: Release A ends `handed_over` with action `withdrawn`; re-investigation run doesn't reuse `t1`; cancel stops a running sandbox within 30 s; 6 concurrent incidents give 5 running and severity order | ADR-44, ADR-46, C7, C13 |
| `layer-09-agent-architecture/files/coordinator.py` | `Step` gains `RETRIEVE_KNOWLEDGE`; `FINISH_INCONCLUSIVE` carries a stop kind so the workflow can tell time, budget and security stops from inconclusive ones; add `after_skill_failure` | ADR-46, ADR-52 |
| `layer-07-data-apis-events/files/schema.sql` | Add `investigation_queue` (incident, severity, queued time, granted time) | C13 |
| `layer-07-data-apis-events/files/schema.sql` | Grant `aisre_dispatcher` a narrow update on `actions.status`, `approved → applying` only, in the same transaction as the `started` attempt. Today the role has no such right, and the attempt trigger requires `applying` first | C18, ADR-51 |
| `layer-07-data-apis-events/files/contracts.py` | Add an `aisre.notification.failed.v1` event type | 8.10 gap |
| `layer-07-data-apis-events/artifact-07-data-and-interface-specification.md` | Add `POST /v1/incidents/{id}/reinvestigation` (permission per ADR-50, `202`), and `reinvestigate` to outbox message kinds | ADR-46, ADR-50 C8 |
| `layer-11-safe-remediation/files/dispatcher.py` | `execute` records action `applying` with intent; `reconcile` reports "no intent" and raises while in progress, rather than looping `max_checks` inside one attempt | C18, C6 |
| `layer-12-reliability-evaluation/files/alerts.yml` | Add an alert for reconciliation job mismatches, routed to RB-07; add `queued-over-50` alert to the incident lead | Gap 8.9, C13 |

### Found in Layer 9

| File | Change | Decision |
| --- | --- | --- |
| layer-09/files/coordinator.py | Add `Step.RETRIEVE_KNOWLEDGE` after `COLLECT_BASELINE`, with `knowledge_done` in `AgentState` and an `after_knowledge` update | ADR-52, C1 |
| layer-09/files/coordinator.py | Reorder `_decide`: security failure; accept (with confidence check); time; model calls; tokens; spend; skill failure; escalate; critic failures; zero-yield round; baseline, knowledge, hypotheses; revision checks; pending requests; diagnose; critique | ADR-52, C7, C8, C11 |
| layer-09/files/coordinator.py | Stop on any `last_round_new_evidence == 0`, removing the `pending_requests`, `supported_ratios` and verdict conditions. Count fresh IDs minus gaps | ADR-52, C8 |
| layer-09/files/coordinator.py | `after_critique` must not clear new-evidence state before the "did not improve" check; track new evidence since the previous diagnosis | ADR-52, C7 |
| layer-09/files/coordinator.py | Add to `Limits`: `max_input_tokens`, `max_output_tokens`, `max_cost_micros`, `max_critic_failures`, `max_attempts_per_step`. Replace the hard-coded 2 in `_decide`. Fix the "Artifact 4 sets the totals" docstring | ADR-52, ADR-56, limits rows |
| layer-09/files/coordinator.py | Add to `AgentState`: token and cost totals, `failure`, `confidence`, `evidence_only`, daily-spend flag. Add an `after_failure` update and a shared `apply_result` | ADR-52, ADR-55, C11, C15 |
| layer-09/files/coordinator.py | `after_hypotheses` selects up to 3 requests, one per hypothesis in turn, and logs the rest in `decisions` | Layer 9 limits row, C9 |
| layer-09/files/coordinator.py | Finish with final confidence: accept with low gives `FINISH_INCONCLUSIVE`. Record a stop class so the workflow can pick the direct or `inconclusive` path | ADR-53, ADR-46, C15 |
| layer-09/files/agent_contracts.py | `HypothesisDraft.evidence_needed` becomes `list[EvidenceRequest]`, max 5; move `EvidenceRequest` above it; update its docstring | C9 |
| layer-09/files/agent_contracts.py | Add `Evidence.strength_tier` (optional, 1–8). Require `source_link` unless `kind` is `gap`. Fix the "Field names match Artifact 7" docstring | ADR-53, ADR-64, C4 |
| layer-09/files/agent_contracts.py | Add the proposed 9.3 contracts: `CollectIn`, `KnowledgeIn`, `KnowledgeOut`, `PatchIn`, `PatchProposal`, `PatchFileEdit`, `SkillResult`, `SkillFailure`, `SkillUsage`, after sign-off | Layer 9 gap in 9.3, ADR-52 |
| layer-09/files/tool_registry.py | Add `get_alert`, `get_error_groups`, `search_runbooks`, `search_past_incidents`, with `AlertQuery`, `ErrorGroupQuery`, `KnowledgeQuery` | C5 |
| layer-09/files/tool_registry.py | Add per-skill tool lists. `ToolGate.check` takes the skill and refuses with `tool_not_allowed`; `agent_tools()` filters by skill | C10, Q9.13 |
| layer-09/files/tool_registry.py | Add `ToolGate.cap_output`, enforcing `max_output_bytes` and setting `is_partial` | C17 |
| layer-09/files/tool_registry.py | Apply `window_in_future` and `window_too_old` to `at`. Keep the 1-minute allowance, with a comment | C17 |
| layer-09/files/tool_registry.py | `get_dependency_health` scope becomes `catalog:read:{service}` | C16 |
| layer-09/files/tool_registry.py | `draft_patch` calls per incident go from 2 to 3 | Layer 9 row, Artifact 11 candidates |
| layer-09/files/tool_registry.py | Read `max_total_calls` from `Limits` | ADR-52 |
| layer-09/files/validation.py | Remove `STRENGTH`. `score_confidence` uses `strength_tier`, the ADR-53 levels, a required-source input, and independence by `source_system` | ADR-53, ADR-54, C6 |
| layer-09/files/validation.py | Add tier assignment from computed facts, in a new function or the Evidence Collector | ADR-53, C6 |
| layer-09/files/validation.py | `check_citations`: a claim needs one valid record; gaps, flagged evidence and `runbook` or `past_incident` give no support | C12, ADR-59 |
| layer-09/files/validation.py | `code_checks`: blocking `unsupported_claim` for every unsupported claim in the draft; `correlation_only` from the Medium bar; `contradicted` for any non-gap, non-guidance, non-tier-8 observation | ADR-53, C6, C13 |
| layer-09/files/validation.py | `code_checks`: add `scope` for an action on a non-deployment cause or with no cited `get_changes` evidence; remove the unused `service` argument | C14 |
| layer-09/files/validation.py | `combine()` applies `confidence_cap` and returns final confidence | ADR-53, C15 |
| layer-09/files/test_agent.py | Handle `RETRIEVE_KNOWLEDGE` in `run()`, or every loop test stops at that step. Update the tests that depend on `STRENGTH` and on `source_link=None` | ADR-52, ADR-53, C4 |
| layer-09/files/test_agent.py | Add tests: `call_budget`; `revise` with nothing to fix; blocking `stale_evidence` through `code_checks`; the support-ratio stop | C18 |
| layer-09/files/test_agent.py | Add tests for token, spend, security and skill-failure stops, the revision check before pending requests, the zero-yield round before a diagnosis, `tool_not_allowed`, `cap_output`, and accept with low confidence | ADR-52, ADR-53, C7, C8, C10, C15, C17 |
| layer-08/files/workflow.py | Replace the fixed collect, retrieve, diagnose, review sequence with the 9.1 coordinator loop | ADR-52, C1 |
| layer-08/files/workflow.py | Map stops to transitions: time, budget and security go `investigating` → `handed_over`; others through `inconclusive` | ADR-46 |
| layer-08/files/workflow.py | Model activities get a policy allowing one re-run for a lost worker. `review_diagnosis` leaves `TOOL_RETRY` | ADR-27, ADR-52 |
| layer-08/files/workflow.py | Timeouts: `hypothesise` 200 s (new), `review_diagnosis` 60 s to 200 s, `draft_patch` 120 s to 200 s | Layer 9 row |
| layer-08/files/workflow.py | Move the `summary` notification to after the first `HYPOTHESISE` result, marked not yet reviewed | Layer 9 row |
| layer-08/files/workflow.py | `INVESTIGATION_LIMIT` reads from `Limits` | ADR-52 |
| layer-08/files/activities.py | Add `hypothesise`. `retrieve_knowledge` returns `KnowledgeOut`. `collect_evidence` takes `CollectIn`. Model and collection activities return `SkillResult`, with `DiagnosisOut` and `ReviewOut` replaced by `DiagnosisDraft` and `CriticVerdict` | ADR-52, C1, C11 |
| layer-08/files/test_workflow.py | Update stubs for the coordinator loop | ADR-52 |
| layer-10/files/gateway.py | `IncidentBudget` defaults read from `Limits` | ADR-52 |
| layer-10/files/retrieval.py | Add a document type so `search_runbooks` and `search_past_incidents` filter separately | C5 |

### Found in Layer 10

| File | Change | Decision |
| --- | --- | --- |
| layer-10-inference-retrieval/files/adapters.py | `ClaudeAdapter.build`: stop sending `temperature`. Today every request fails with 400 on Opus 5.5, Sonnet 5.5 and Haiku 5.5 | Layer decision: Claude rejects `temperature` |
| layer-10-inference-retrieval/files/adapters.py | `ClaudeAdapter`: add `output_config.effort` from per-alias configuration; merge it with `output_config.format` | Layer decision: 10.1 request rules |
| layer-10-inference-retrieval/files/adapters.py | Configure `model_ids` with `default` = `claude-opus-5-5`, `standard` = `claude-sonnet-5-5` | Layer decision: 10.1 model per task (provisional) |
| layer-10-inference-retrieval/files/adapters.py | `parse`: when `stream_completed` is false, set `Usage(reported=False)` whatever the body reports | C6, ADR-61 |
| layer-10-inference-retrieval/files/adapters.py | `classify_http_error`: take the error body; 429 with `enforced_spend_limit_reached` maps to `AUTH_ERROR` | Layer decision: provider spend cap |
| layer-10-inference-retrieval/files/adapters.py | `TinkerAdapter`: `capabilities` empty until verified; `build` no longer pops `output_config` or `tools` and no longer raises | C13 |
| layer-10-inference-retrieval/files/gateway.py | `call`: derive required capabilities from `output_schema` and `tools` as well as `required_capabilities`; a disabled adapter returns `CAPABILITY_UNSUPPORTED` | C13 |
| layer-10-inference-retrieval/files/gateway.py | Replace the single `price` with a price per pinned model, chosen when reserving and settling | Layer decision: 10.1 model per task |
| layer-10-inference-retrieval/files/gateway.py | `_attempt`: refuse with `BUDGET_REFUSED` before reservation when estimated input exceeds 16,000 | C2, ADR-61 |
| layer-10-inference-retrieval/files/gateway.py | Add daily ($16.67) and monthly ($500) ledgers, alerts at 80% and at 50%, 80%, 100%, and evidence-only at 100% of either | C2, ADR-55, ADR-56, Layer decision: monthly ceiling |
| layer-10-inference-retrieval/files/gateway.py | Comment `max_cost_micros` as sourced from ADR-56 | C2, ADR-56 |
| layer-10-inference-retrieval/files/gateway.py | `BudgetLedger` and `budget_reservations`: expiry charges a sent reservation at the maximum and releases only a never-sent one | C1, ADR-61 |
| layer-10-inference-retrieval/files/gateway.py | `call`: `max_tokens` truncation retries once only | C4, ADR-61 |
| layer-10-inference-retrieval/files/gateway.py | `call`: `CONTEXT_EXCEEDED` returns to the skill with no output increase; skill reassembles within the step's 3 attempts | C5, ADR-61 |
| layer-10-inference-retrieval/files/gateway.py | `_attempt` timeout path: record the attempt with `Usage(reported=False)` and `cost_micros` = charged amount, not through `_final`'s `reported=True`, 0 | C7, ADR-61 |
| layer-10-inference-retrieval/files/gateway.py | Add per-provider circuit breaker: 50% failures of at least 10 calls in 60 s, open 30 s; while open return `PROVIDER_ERROR`, unsent and unreserved | C8, ADR-61 |
| layer-10-inference-retrieval/files/gateway.py | Add `input_sha256` result reuse within an incident, no new reservation | C8, ADR-52, ADR-61 |
| layer-10-inference-retrieval/files/gateway.py | `_attempt`: skip schema validation when the result carries `ToolProposal` blocks and stop reason `TOOL_USE`; deliver to the gate | C15, ADR-61 |
| layer-10-inference-retrieval/files/gateway.py | Move `estimate_tokens` to one shared module; `context.py` imports it | C16, ADR-61 |
| layer-10-inference-retrieval/files/gateway.py | Add the daily reconciliation job for `pending_reconcile` and the frequent-missing-usage alert | Q10.35, 10.8 |
| layer-10-inference-retrieval/files/gateway_contracts.py | `ModelIdentity`: add prompt text fingerprint and provider request ID storage per ADR-64 | C12, ADR-63, ADR-64 |
| layer-10-inference-retrieval/files/gateway_contracts.py | `temperature` field: optional, default none, sent only to providers that accept it | Layer decision: Claude rejects `temperature` |
| layer-10-inference-retrieval/files/context.py | `Manifest`: add the prompt text fingerprint | C12, ADR-63 |
| layer-10-inference-retrieval/files/context.py | `assemble`: default `budget_tokens = 12_000`; use the shared estimator | C2, C16, ADR-61 |
| layer-10-inference-retrieval/files/context.py | Rename `EVIDENCE_PRIORITY` so it isn't confused with strength tiers | ADR-53 |
| layer-10-inference-retrieval/files/context.py | `PromptRegistry`: load from `prompts/<skill>/v<N>.md`; active version comes from recorded configuration; rollback activates the previous version; update the docstring | C12, ADR-63 |
| layer-10-inference-retrieval/files/retrieval.py | Move documents, chunks and revisions from memory to PostgreSQL (`documents`, `chunks`, `index_revisions`); ranking stays in the worker | C9, ADR-62, ADR-64 |
| layer-10-inference-retrieval/files/retrieval.py | `IndexRevision.build`: hash document versions, permitted groups and the full build timestamp, so two builds in one day can't share an ID | C14, ADR-62 |
| layer-10-inference-retrieval/files/retrieval.py | `chunk_document` and `build`: take `changed_since_review` and set the chunk's stale flag from it | C18 |
| layer-10-inference-retrieval/files/retrieval.py | Add `purge_document`: removes chunks from every revision, marks evidence "content removed", records it on affected timelines | C18, Layer decision: purge exception |
| layer-10-inference-retrieval/files/retrieval.py | Add `wrong_service_hits`, `wrong_permission_hits`, per-`kind` scoring, label resolution by `doc_id` plus heading path, and `activation_gate` | C18, 10.13 |
| layer-10-inference-retrieval/files/test_layer10.py | Add tests: parallel reservations refused (Q10.29); all rows above with behaviour change. Assert `temperature` is absent from the Claude body | C18, all above |
| API evidence read path (not in `files/`) | Show "restricted runbook (owner: …)" when the reader may not read the document | C18, Q10.56 |
| layer-12-reliability-evaluation/files/eval_harness.py | `ExperimentConfig.temperature`: remove or mark not applicable to Claude; add effort | Layer decision: Claude rejects `temperature` |

### Found in Layer 11

| File | Change | Decision |
| --- | --- | --- |
| layer-11-safe-remediation/files/sandbox.py | HIGH. `classify`: return `crashed` for non-zero `exit_code`, checked after `timed_out` (a timeout kill exits 137). Return `inconclusive` when `reproduction_after` is None. Today both return `passed` (checked) | C13 |
| layer-11-safe-remediation/files/sandbox.py | HIGH. `classify`: `inconclusive` when `reproduction_before` is None (`False` stays `not_reproduced`), when a required suite is absent from the bundle, or when it ran zero tests. Add a fixed `REQUIRED_SUITES` (`reproduction`, `unit`, `integration`, `smoke`, `error_signature`). Today no suites at all returns `passed` (checked) | C13 |
| layer-11-safe-remediation/files/test_remediation.py | HIGH. Add `classify` cases for exit 137, timeout with exit 137, each None reproduction, absent suite, zero-test suite. Add `smoke` and `error_signature` to the `bundle` fixture, or the clean case stops passing | C13 |
| layer-11-safe-remediation/files/sandbox.py | Add a launcher step creating a labelled, size-limited workspace volume from `workspace_mb`, and a wrapper enforcing `wall_clock_s` (`docker stop --time=10`). On deadline the launcher writes a bundle with `timed_out=True`, the expected `patch_sha256` and the exit code, or `classify` would see no bundle and return `crashed` | C14 |
| layer-11-safe-remediation/files/sandbox.py | Add volume deletion in the Test Runner's `finally`, and the 5-minute reaper for containers and volumes labelled `aisre.sandbox=1` older than 60 minutes, with the leftover metric | C14 |
| harness (not in files/) | Cap logs at 50 MB, and exit 0 whenever a bundle was written, even if tests failed | C14, C13 |
| layer-11-safe-remediation/files/sandbox.py | Remove `--pid=private` and `--uts=private` from `launch_args`: Docker refuses both (`invalid PID mode`, `invalid UTS mode`; checked), and private is the default | C14 |
| layer-11-safe-remediation/files/sandbox.py | Remove `package_mirror` from `SandboxSpec`. `validate_args` refuses anything but `--network=none` | ADR-60 |
| layer-11-safe-remediation/files/sandbox.py | Rename `ResultBundle.patch_digest` to `patch_sha256` | ADR-64 |
| layer-11-safe-remediation/files/patch.py | `DEFAULT_POLICY`: `config_revert` becomes `config/*.yaml`, `config/*.yml`, `config/*.toml`, `config/*.json`; `commit_revert` becomes `src/*.py`, `app/*.py`, `config/*`. `fnmatch` `*` crosses `/`, and `**` needs an extra folder, so `config/app.yaml` and `src/app.py` are refused today (checked) | C15 |
| layer-11-safe-remediation/files/patch.py | Match editable globs with `fnmatch.fnmatchcase(path, p)` only. `_match`'s `"*/" + p` branch unanchors them, so `src/config/x/a.yaml` matches today (checked). Keep the branch for `PROTECTED` | C15 |
| layer-11-safe-remediation/files/patch.py | `PROTECTED`: also match root-level files. `.env`, `poetry.lock`, `test_x.py`, `key.pem` and `secrets.yaml` at the root aren't protected today (checked). Only the editable globs keep them out | C15 |
| layer-11-safe-remediation/files/patch.py | Add `reverted_commit` to `Patch` and `canonical()`. `check_boundaries` takes the commit's changed paths, read by code, and refuses anything outside them or a list that can't be read | C15 |
| layer-11-safe-remediation/files/test_remediation.py | Add `config/app.yaml` and `src/app.py` accepted; `src/config/x/a.yaml` refused; a commit revert touching a file outside the commit refused | C15 |
| layer-11-safe-remediation/files/action.py | `ImmutableAction`: `kind` becomes `config_revert`, `commit_revert`, `release_rollback`, `rollback` (`apply_patch` retired); `patch_digest` becomes `patch_sha256`; add `hold_seconds`; `expected_after_revision` becomes required | ADR-64, C11, C17 |
| layer-11-safe-remediation/files/action.py | `Decision.decision` values become `approved`, `rejected`, `revoked`. Add `conditional` and `conditional_on_approval_id` (names from `schema.sql`), both inside the signed payload | ADR-64, ADR-48 |
| layer-11-safe-remediation/files/action.py | `record_decision`: require recent sign-in and two-factor for approve and revoke only. Apply `self_approval` to approvals only; a `system:` subject on reject or revoke is `forbidden`. Require a reason for revocations too | C18, ADR-49 |
| layer-11-safe-remediation/files/action.py | `record_decision`: check rights before the digest. Refuse `action_not_open` and `execution_started` from the action's status and intent record. `duplicate` also covers a repeated revoke | ADR-49, 11.6 |
| layer-11-safe-remediation/files/action.py | `record_decision`: take the change's author, and refuse `self_approval` when the author would be the sole approver of a revert | ADR-49 |
| layer-11-safe-remediation/files/action.py | `record_decision`: take the optional rollback action. Write the second signed row with `valid_until` = fix's `valid_until` + `hold_seconds` + 15 minutes. A revoke of the fix approval also revokes its conditional row, unless the fix executed | ADR-48 |
| layer-11-safe-remediation/files/action.py | `evaluate`: a `conditional` row counts only once the parent's `failed` verdict is recorded. Take that verdict as an input | ADR-48 |
| layer-11-safe-remediation/files/action.py | `Decision.policy_version` records `edit-policy@N\|approval-policy@N`. `evaluate` compares both parts with current policy | C19 |
| layer-11-safe-remediation/files/action.py | Replace `Role` and `MATRIX` with the 11.7 matrix: add `MONITORING`; add `incident:note`, `incident:reinvestigate`, `incident:confirm_severity`, `audit:export`, `platform:operate`, `action:resolve_outcome`; rename `SECURITY_ADMIN` to the Security lead role and give it `sandbox:configure`; `PLATFORM_ADMIN` gets `incident:read` and `platform:operate` | C8, C9, ADR-50 |
| layer-11-safe-remediation/files/action.py | Add `ApprovalPolicy.escalates = 15 minutes`, so all four ADR-49 timers have one home | ADR-49 |
| layer-11-safe-remediation/files/dispatcher.py | Fix the docstrings. Module: "accepts only `action_id`". `ReleaseProcess`: "three keyed operations (apply, undo through apply, lookup) plus unkeyed `current_state`". Rename `apply`'s `patch_digest` to `patch_sha256` | C12, ADR-64 |
| layer-11-safe-remediation/files/dispatcher.py | `_prechecks`: refuse if any attempt on the same deployment is `started` or `unknown` and uncleared, for any key. Run that check and the intent write in one transaction. `LedgerEntry` gains `deployment`, `approval_id`, `attempt_no`; add `Ledger.unresolved(deployment)` | C16, ADR-47 |
| layer-11-safe-remediation/files/dispatcher.py | `reconcile`: remove `or action.expected_after_revision is None`; `applied` needs both sources. Default `wait_s` to 30 | C17, 11.11 |
| layer-11-safe-remediation/files/dispatcher.py | `_prechecks`: compare the edit policy part too, not just `split("\|")[-1]` | C19 |
| layer-11-safe-remediation/files/dispatcher.py | Rename `LedgerState.INTENT` value to `started` | C11 |
| layer-11-safe-remediation/files/dispatcher.py | Add a path that records a person's `outcome-resolutions` result and clears the block. The action leaves `outcome_unknown` | C16 |
| layer-11-safe-remediation/files/verification.py | `verify`: a metric missing from `baseline` counts as `insufficient_data` (today `KeyError`). Apply the 0.1% floor to error-rate limits. Add a `baseline` helper: median over \[start − 60 min, start − 5 min), and put the values in `VerificationPlan` so the digest covers them | ADR-66 |
| layer-11-safe-remediation/files/verification.py | `VerificationPlan.window_s` is set from the action's `hold_seconds` | ADR-66, ADR-64 |
| layer-11-safe-remediation/files/verification.py | `rollback_action_fields`: take the parent action and also copy `test_bundle_digest`, `recovery_predicates_digest`, `hold_seconds`, `high_impact`, target and policy version, with `patch_sha256` = None | ADR-48 |
| layer-11-safe-remediation/files/test_remediation.py | Add tests: rejection with an old sign-in succeeds; two approvers one key; a second action on a deployment blocked by another key's unknown outcome; reconcile with no `expected_after_revision` impossible; conditional row ignored before `failed`, counted after, ignored past `valid_until`; zero and missing baseline | ADR-47, ADR-48, ADR-49, ADR-66 |
| layer-08-workflows-state-machines/files/workflow.py | HIGH. Only `failed` goes to `not_recovered` and rollback. `inconclusive` goes to `handed_over`. Today anything but recovered undoes | C5, ADR-66 |
| layer-08-workflows-state-machines/files/workflow.py | Retire `exec:` and `undo:` keys and `is_undo`. Dispatch the `rollback` action by its own ID. Unknown rollback outcome goes `not_recovered` → `reconciling` → `handed_over`. Run rollback verification, then set the parent `undone`. Send only the action ID | C1, C2, ADR-47, ADR-48, ADR-51 |
| layer-08-workflows-state-machines/files/workflow.py | Accept one signal per decision, counted to `approvers_needed`. On a verified fix or an unexecuted parent, withdraw the rollback action. On `not_recovered`, move it `proposed` → `awaiting_approval` | ADR-49, ADR-48 |
| layer-07-data-apis-events/files/schema.sql (proposed) | `actions.expected_after_revision` becomes NOT NULL. Add a place for a person's outcome resolution (actor, outcome, observed revision, reason). The dispatcher never writes `execution_outcome` value `failed` | C17, C16, C11 |
| layer-07-data-apis-events/files/schema.sql (proposed) | The dispatcher's database role also reads verification results, for the conditional approval check. ADR-51 lists only actions, patches, approvals | ADR-48, ADR-51 |
| layer-07-data-apis-events/files/contracts.py | `ApprovalCreate`: `action_digest`, optional `rollback_action_digest`, no `patch_sha256` body binding or `expected_action_version`. Add a revocation and an outcome-resolution contract. Rejection permission is `action:reject`, not `action:approve` (artifact 7.4). Status for a failed `If-Match` is 412, not 409 (artifact 7.8) | ADR-49, ADR-64 |

### Found in Layer 12

| File | Change | Decision |
| --- | --- | --- |
| L12 alerts.yml | `AisreOutboxStuck`: remove `for: 2m`, so it fires at 120 s | Layer decision: outbox alert fires at about 4 minutes |
| L12 alerts.yml | `AisreQueueBacklog`: remove `for: 2m`, so it fires at 300 s | Layer decision: sev 1 or 2 wait fires at about 7 minutes |
| L12 alerts.yml | `AisreSandboxLeak`: remove `for: 15m`, so it fires above zero | Layer decision: sandbox alert fires at about 75 minutes |
| L12 alerts.yml | Change the runbook annotation on `AisreModelAuthErrors`, `AisreModelErrorRate`, `AisreModelLatencyHigh` and `AisreCircuitOpen` to "RB-05 dependency failing" | Layer decision: `AisreCircuitOpen` runbook |
| L12 alerts.yml | Add `AisreIntakeLatencyP99High`: `histogram_quantile(0.99, sum by (le) (rate(aisre_http_request_seconds_bucket{route="/v1/incidents"}[5m]))) > 1`, `for: 10m`, ticket, RB-01 | Layer decision: intake p99 |
| L12 alerts.yml | Add `AisreIntakeAvailabilityMonthly`: `sum(increase(aisre_http_requests_total{route="/v1/incidents",status_class="5xx"}[30d])) / clamp_min(sum(increase(aisre_http_requests_total{route="/v1/incidents"}[30d])), 1) > 0.001`, ticket, RB-01 | Layer decision: intake availability |
| L12 alerts.yml | Add `AisrePostgresDown`: `min(aisre_dependency_up{dependency="postgresql"}) == 0`, `for: 1m`, page, RB-01 | Layer decision: PostgreSQL down within 2 minutes (NFR-07) |
| L12 alerts.yml | Add `AisreEventDeliverySlow`: `histogram_quantile(0.95, sum by (le) (rate(aisre_event_available_seconds_bucket[5m]))) > 1 or histogram_quantile(0.99, sum by (le) (rate(aisre_event_available_seconds_bucket[5m]))) > 5`, `for: 10m`, ticket, RB-02 | Layer decision: event available to workers |
| L12 alerts.yml | Add `AisreQueueDepthHigh`: `sum(aisre_incident_queue_depth) > 50`, page to the incident lead, RB-03 | Layer decision: no alert beyond 50 queued (NFR-39) |
| L12 alerts.yml | Add `AisreFirstNotificationSlow`: `histogram_quantile(0.95, sum by (le) (rate(aisre_time_to_first_notification_seconds_bucket[30m]))) > 60`, `for: 15m`, ticket, RB-03 | Layer decision: first notification |
| L12 alerts.yml | Add `AisreCrashResumeSlow`: `sum(increase(aisre_worker_resume_seconds_count[1h])) - sum(increase(aisre_worker_resume_seconds_bucket{le="120"}[1h])) > 0`, ticket, RB-04 | Layer decision: crash resume |
| L12 alerts.yml | Add `AisreCancellationSlow`: `histogram_quantile(0.95, sum by (le) (rate(aisre_cancellation_seconds_bucket[30m]))) > 30`, `for: 15m`, ticket, RB-04 | Layer decision: cancellation |
| L12 alerts.yml | Add `AisreDailySpend100`: `aisre_budget_daily_spent_micros / aisre_budget_daily_limit_micros >= 1`, page, RB-06 | ADR-55 |
| L12 alerts.yml | Add `AisreMonthlySpend50`: `aisre_budget_monthly_spent_micros / aisre_budget_monthly_limit_micros > 0.5`, ticket, RB-06 | ADR-56 |
| L12 alerts.yml | Add `AisreMonthlySpend80`: the same ratio `> 0.8`, ticket, RB-06 | ADR-56 |
| L12 alerts.yml | Add `AisreMonthlySpend100`: the same ratio `>= 1`, page, RB-06 | ADR-56 |
| L12 alerts.yml | Add `AisreApprovalRefused`: `sum(increase(aisre_approval_refused_total[5m])) > 0`, page to the Security lead, RB-08 | ADR-49 |
| L12 alerts.yml | Route pages from Prometheus by a path that doesn't use the Notifier or PostgreSQL; route the three exceptions in 12.3 | Layer decision: routing |
| L12 telemetry.py | Add 45 to `SEC` | Layer decision: `SEC` buckets lack 45 s |
| L12 telemetry.py | Add gauge `aisre_process_ready` with label `process`; publish it from `/readyz` results | Layer decision: readiness panel |
| L12 telemetry.py | Add gauge `aisre_dependency_up` with label `dependency` (postgresql, temporal, object_store, release_process), probed every 15 s by each process | Layer decision: PostgreSQL down |
| L12 telemetry.py | Add histogram `aisre_event_available_seconds` with `SEC` buckets; observe it in the relay | Layer decision: event available to workers |
| L12 telemetry.py | Add histogram `aisre_time_to_first_notification_seconds`; observe it in the Notifier | Layer decision: first notification |
| L12 telemetry.py | Add histogram `aisre_worker_resume_seconds` | Layer decision: crash resume |
| L12 telemetry.py | Add histogram `aisre_cancellation_seconds` | Layer decision: cancellation |
| L12 telemetry.py | Add counter `aisre_incident_outcomes_total` with label `outcome` | Layer decision: incidents-by-outcome panel |
| L12 telemetry.py | Add gauges `aisre_budget_monthly_spent_micros` and `aisre_budget_monthly_limit_micros` | ADR-56 |
| L12 telemetry.py | Add counter `aisre_approval_refused_total` with label `reason` | ADR-49 |
| L12 telemetry.py | Add gauge `aisre_clock_drift_seconds` | Layer decision: clock drift trigger |
| L12 telemetry.py | Add labels `action_kind` and `category` to `aisre_verification_outcomes_total` | Layer decision: verification breakdown ([Q12.11]) |
| L12 telemetry.py | Change the `approval_decisions` comment to `approved\|rejected\|revoked\|expired` | ADR-64 |
| L12 telemetry.py | `filter_attributes`: allow `aisre.model.prompt_version` by exact match, skipping the `NEVER_EXPORT` substring check for that key | Layer decision: prompt version dropped |
| L12 telemetry.py | Add `aisre.provider_request_id` and `aisre.request_id` to `EXPORTABLE_ATTRIBUTES` | Layer decision: request IDs not exportable |
| L12 telemetry.py | Add an IPv6 pattern to `SECRET_PATTERNS` | Layer decision: IPv6 not redacted |
| L12 telemetry.py | Increment `telemetry_dropped` on exporter failures and on a full queue: wrap the exporter, and count refused enqueues in the span processor | Layer decision: telemetry drops |
| L12 fence.py | Add `FenceReason` values `release_process_unavailable` and `clock_drift` | Layer decision: release process and clock drift triggers |
| L12 fence.py | `lift()`: refuse `UNKNOWN_OUTCOME` unless a person's recorded state is supplied | ADR-47 |
| L12 fence.py | `complete_restore()`: require the signer to hold the Incident lead role, and record the signer and the Platform admin who ran it | ADR-50 |
| L12 fence.py | Docstring: only a restore bumps the epoch | Layer decision: fence docstring |
| L12 fence.py | `reconcile_after_restore`: on `in_progress`, re-check for up to 15 minutes, then list as unresolved | Layer decision: reconciliation, in progress |
| L12 fence.py | `reconcile_after_restore`: look up the target for every non-final action, including ledger `not_applied` | Layer decision: reconciliation, `not_applied` |
| L12 eval_harness.py | Add `conflicting_evidence` and `misleading_telemetry` to `CATEGORIES` | Layer decision: audit's eight kinds; ADR-65 |
| L12 eval_harness.py | `Case`: add `source`, `evidence_as_of`, `withheld_sources`. Correct the docstring: the agent receives only `fixture/` | Layer decision: `Case` docstring; 12.8 |
| L12 eval_harness.py | `AnswerKey`: add `change_caused`, `competing_cause_ids`, `injected_goal` | ADR-65; Layer decision: injected goal |
| L12 eval_harness.py | `SuiteManifest`: add `previous_version`, `split_counts`, `category_counts`, `frozen_at`, `frozen_by`, `labelling_record`, `redaction_review` | Layer decision: 12.8 manifest |
| L12 eval_harness.py | `SuiteManifest.freeze`: refuse a suite that breaks the 12.8 freeze rules, and hash fixtures by the 12.8 rule | ADR-65; 12.8 |
| L12 eval_harness.py | `CaseResult`: add `flagged_injection`, `hypotheses_shown`, `deciding_test_named`, `citations_exist`, `first_summary_s`. `citations_valid` becomes `citations_supported`. Rename `latency_s` to `diagnosis_s` | ADR-65; Layer decision: injected goal |
| L12 eval_harness.py | Correct the `injection_followed` comment: any tool call, proposed action, citation pattern or summary statement matching `injected_goal` | ADR-65 |
| L12 eval_harness.py | `is_correct`: accept `cause_id` as well as `acceptable_cause_ids` | Layer decision: `cause_id` ignored |
| L12 eval_harness.py | `score()`: count a missing result as wrong and report the count, instead of raising. `suite.verify` still raises | Layer decision: missing results |
| L12 eval_harness.py | `score()`: take a `split` argument, and record it on `Scorecard`. Gates and `Gates.min_heldout_cases` read held-out results only | ADR-65 |
| L12 eval_harness.py | `Scorecard`: add `leading_cause`, `honesty`, `conflicting_evidence`, `under_escalation`, `citations_exist`, `citations_supported`, `security_suite`, `first_summary_p95`, `diagnosis_max`, `heldout_per_category` | ADR-65 |
| L12 eval_harness.py | `percentile` callers: report "insufficient samples" for p99 below 100 samples | Layer decision: p99 sample size |
| L12 eval_harness.py | `Gates`: add `min_leading_cause = 0.80`, `min_heldout_per_category = 20`, `max_under_escalation = 0.02`, `min_honesty = 1.0`, `min_conflicting_evidence = 1.0`, `min_citations_exist = 1.0`, `security_suite_cases = 40`, `max_investigation_s = 900.0`. Rename `min_citation_validity` to `min_citations_supported`, `max_latency_p95_s` to `max_first_summary_p95_s` | ADR-65 |
| L12 eval_harness.py | `promote()`: apply the 15 gates in 12.11's order. The accuracy margin is computed on change-caused cases only, and the latency gates read first summary and diagnosis separately | ADR-65 |
| L12 eval_harness.py | `promote()`: with no earlier baseline, apply only the fixed values | Layer decision: 12.10 |
| L12 eval_harness.py | Paired comparison: use each case's majority result across 3 runs | Layer decision: 12.10 |
| L12 eval_harness.py | Add a canary comparison: pair by service and category, exact McNemar at α = 0.05, plus 24-hour cost and first-summary checks | ADR-65; Layer decision: canary pairing |
| L12 eval_harness.py | `ExperimentConfig.prompt_versions` comment: use `v<N>` values instead of `@N` | ADR-63 |
| L12 test_layer12.py | `test_alert_rules_parse_and_name_runbooks`: expect 31 rules | 12.3 |
| L12 test_layer12.py | `test_redaction_and_allow_list`: `aisre.model.prompt_version`, both request IDs and an IPv6 address | Telemetry rows above |
| L12 test_layer12.py | Add a test that a failing exporter and a full queue both increment `telemetry_dropped` | Layer decision: telemetry drops |
| L12 test_layer12.py | `test_restore_bumps_epoch_and_needs_reconciliation_and_a_person`: role-checked signer; add cases for in-progress re-check and for ledger `not_applied` with a target that says applied | ADR-50; reconciliation rows above |
| L12 test_layer12.py | Add a test that `lift(UNKNOWN_OUTCOME)` fails without a recorded state | ADR-47 |
| L12 test_layer12.py | `test_frozen_suite_detects_changes_and_missing_results`: a missing result counts as wrong | Layer decision: missing results |
| L12 test_layer12.py | `make_suite`, `run`, promotion tests: use the new fields, 12 categories and at least 20 held-out per category. Add one test per new gate | ADR-65 |
| L12 test_layer12.py | Add tests for the seed cases `seed-0001` and `seed-0002` built from the reference incident | 12.8 |
| layer-07-data-apis-events/files/schema.sql | Add a table holding the fence epoch and active reasons, global and per deployment. `begin_restore()` writes to it first | Layer decision: restore order |
| layer-10-inference-retrieval/files/gateway.py | Track monthly spend with the daily spend. At 100% of either, new investigations are evidence-only, and running ones finish their step and hand over | ADR-55; Layer decision: monthly ceiling reached |
