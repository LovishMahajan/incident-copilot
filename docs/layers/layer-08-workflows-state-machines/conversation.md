# Layer 8: Distributed Workflows and State Machines — Conversation

- **Central question:** How does long-running work survive failure?
- **Built from:** Artifacts 6 and 7
- **Focus for this layer:** Establish why ordinary application code is insufficient for long-running, failure-prone processes before designing the workflow. Every state and transition must be explicit.
- **Deliverable:** [Artifact 8: Workflow Specification](artifact-08-workflow-specification.md)
- **Questions:** 48

## Conversation log

| Questions | Agent | Conversation link | Decision reached | Open unknowns and owner | Artifact section |
| --- | --- | --- | --- | --- | --- |
| Q8.1–Q8.48 | TBD | [Shared conversation](https://claude.ai/share/c6a48a50-d511-4886-b4f5-0a3000f6a725) (needs sign-in; not read for these docs) | Recorded per question below | Listed under each question's Unknowns and Assumptions | [Artifact 8: Workflow Specification](artifact-08-workflow-specification.md) |

## Where Claude and ChatGPT disagreed

TBD: the source documents do not record any disagreements. The team fills this in.

## Questions and answers

### Q8.1 An incident investigation requires several operations that must happen in a particular order. How would you represent and coordinate this sequence?

**Frame.** How do we express and run the ordered steps of an investigation?

**Knowns.** Artifact 6: a Workflow Orchestrator owns step order, checkpoints, timers and cancellation; Temporal is the engine (ADR-06).

**Unknowns.** None.

**Assumptions (provisional).** None needed.

**Answer.** As **one workflow per incident**, written as ordinary sequential code that calls **activities** for each step. The workflow holds the order and the decisions (if inconclusive, hand over; if approved, dispatch). Activities do the work. The order is visible in one place, `IncidentWorkflow._lifecycle` (8.1), and every business step is also an explicit state transition (8.4).

### Q8.2 A normal Python function executes from beginning to end inside a process. How would its behavior need to change to support operations that continue independently of that process?

**Frame.** What must change for code to outlive the process running it?

**Knowns.** Artifact 4: resume after a crash within 2 minutes; workflows can last 24 hours.

**Unknowns.** None.

**Assumptions (provisional).** None needed.

**Answer.** Its progress must live **outside the process**:

- **Each step's result is recorded durably** before the next step starts.
- **Any process can continue it:** a new process rebuilds where the function was from the record, and carries on.
- **Waiting doesn't hold a process:** timers and waits for people are stored, not slept.
- **Steps that touch the world are separate and idempotent,** so repeating one after a crash is safe.
- **The coordinating code is deterministic,** so rebuilding it from the record gives the same result.

That is what a durable workflow engine provides. Built by hand, it is exactly the background threads and database flags that lose progress (Q8.12).

### Q8.3 Incident investigations can take several minutes or hours. What problems arise when the entire investigation is tied to one HTTP request?

**Frame.** Why not run the investigation inside the request?

**Answer.**

- **Timeouts:** proxies and clients close requests after seconds or minutes. The investigation dies, or continues with no one listening.
- **Duplicates:** monitoring retries the timed-out request, and starts a second investigation.
- **No survival:** an API restart or deployment kills every investigation in progress.
- **No waiting:** a request can't wait an hour for an approver.
- **Wasted capacity:** each investigation holds a connection and a worker slot for its whole life, so intake slows down.

Hence intake returns in milliseconds, and the workflow runs separately (Artifact 6, Q6.1).

### Q8.4 A worker crashes halfway through an investigation. What happens to its local variables and in-memory execution state?

**Frame.** What's lost when a worker dies?

**Answer.** **Everything in memory is gone:** local variables, the call stack, open connections, in-progress activity results that weren't reported, and timers.

What survives is what Temporal recorded: the workflow's input, each completed activity's result, each signal received, and each timer. Another worker **replays** that history through the workflow code to rebuild the same variables (Q8.8). An activity that was running when the worker died is retried after its timeout, which is why activities must be idempotent.

### Q8.5 The platform restarts after a deployment, but several investigations are still active. Which information must survive the restart?

**Frame.** What must outlive a restart?

**Answer.**

| **Information** | **Where it survives** |
| --- | --- |
| Each workflow's history: input, completed steps, signals, timers | Temporal (PostgreSQL store) |
| Incident and action state, version, timeline | PostgreSQL incident record |
| Evidence, diagnoses, patches, approvals, attempts, verifications | PostgreSQL + artifact store |
| Pending outbox messages (starts, signals) | PostgreSQL outbox |
| Budget used and reserved | PostgreSQL |
| Approval deadlines, escalation times | Temporal timers, plus the approval request row |
| Execution intent and key, before any production write | PostgreSQL execution attempt |

Nothing that matters lives only in a worker's memory.

### Q8.6 An investigation needs to continue from its last completed step after a crash. Which execution guarantees would make this possible?

**Frame.** What guarantees let work resume where it stopped?

**Answer.**

- **Durable history:** every completed step is recorded before the next starts.
- **Deterministic replay:** the same history through the same code gives the same decisions.
- **At-least-once activities:** an interrupted activity is retried, so it must be idempotent.
- **Exactly-once workflow decisions:** each decision is recorded once in the history, even if the code replays many times.
- **One live run per workflow ID:** no duplicate investigation.
- **Durable timers and signals:** waits and human decisions survive restarts.

### Q8.7 Engineers need to understand which operations were completed before a worker crashed. How should execution history be preserved?

**Frame.** How do engineers see what was done before a crash?

**Answer.** Two records, for two audiences:

- **The incident timeline** (product): every business step is written by the `record_transition` activity as an event, in the same transaction as the state change. Engineers read it through the API.
- **Temporal's history** (engineering): every activity attempt, failure, retry and timer. It's for debugging the platform, viewed in Temporal's own tools by platform engineers only.

After a crash, the timeline shows the last completed business step, and Temporal's history shows the interrupted attempt.

### Q8.8 A workflow restarts after a failure and must reconstruct its previous progress. How could historical execution records be used to reproduce that progress?

**Frame.** How does history rebuild progress?

**Answer.** **Replay:**

1. A worker picks up the workflow, and receives its history.
2. It runs the workflow code from the start. When the code asks for an activity that the history says already completed, the recorded result is returned **without running the activity again**.
3. Signals and timers are delivered at the same points they occurred.
4. When the code reaches the end of the history, it is in exactly the state it was in before the crash, and continues live.

Tested: `test_completed_history_replays_deterministically` replays a full run against the current code.

### Q8.9 The workflow engine re-executes workflow code to reconstruct state. Which restrictions must be imposed on workflow code to ensure repeatable behavior?

**Frame.** What may workflow code not do?

**Answer.** Workflow code must give the same decisions every time it runs over the same history:

- **No I/O:** no database, network, files or model calls; only activities do these.
- **No system clock:** use `workflow.now()`.
- **No randomness, and no new UUIDs:** IDs come from activities, or are derived from history, such as `incident-<id>:t<step>`.
- **No threads or blocking sleeps:** use `workflow.sleep` and `wait_condition`.
- **No reading environment or global mutable state.**
- **No iteration over unordered collections** that affects decisions.
- **No changing the order or type of activity calls** for running workflows without versioning (Q8.23).

Temporal's Python sandbox enforces most of these. The rest are checked in code review and by replay tests.

### Q8.10 Workflow code needs to coordinate operations, while external operations need to communicate with databases and model providers. How should these responsibilities be separated?

**Frame.** What belongs in the workflow, and what in activities?

**Answer.**

| **Workflow (deterministic)** | **Activities (do I/O)** |
| --- | --- |
| Step order and branching | Database reads and writes |
| Waiting for signals and timers | Evidence tool queries |
| Choosing which activity to call next | Model calls through the gateway |
| Tracking incident and action state in memory | Sandbox tests |
| Checking a transition is allowed before requesting it | Approval requests, notifications |
| Cancellation safe points | Production changes (dispatcher queue) |
| Limits that depend on time | Recovery verification |

Each activity is small, idempotent on a key, and has its own timeout and retry policy (8.5).

### Q8.11 An external model call succeeds, but the workflow process crashes before recording the result. How should the external operation be retried and reconciled?

**Frame.** What happens when the model answered but the result wasn't recorded?

**Answer.**

- **The** **`diagnose`** **activity didn't report completion**, so Temporal retries it after its timeout. The model is called again.
- **This is safe,** because a model call has no external side effect: it reads and returns text.
- **Its cost is counted:** the first call's budget reservation either settled (if the activity got that far) or expires and is released. The second call takes its own reservation. Both calls are recorded in `model_attempts`.
- **To avoid paying twice:** the gateway checks for a recorded successful attempt with the same `input_sha256` for this incident and step, and reuses it if found.

Model calls are therefore at least once, with reuse of a recorded result, within the budget.

### Q8.12 The engineering team currently manages long-running tasks using background threads and custom database flags. These tasks frequently lose progress after crashes. Which durable execution system could replace this approach?

**Frame.** What replaces hand-built background work?

**Knowns.** Artifact 6, ADR-06: Temporal, single node, storing its state in PostgreSQL.

**Answer.** **Temporal.** It replaces the threads and flags with:

- recorded history and replay
- durable timers
- signals for human decisions
- retries with limits
- cancellation
- one run per workflow ID

The fallback, a PostgreSQL job queue behind the same Workflow Orchestrator interface, stays recorded in ADR-06.

### Q8.13 Several investigations need to survive worker restarts and resume from their previous state. How should the platform preserve workflow progress?

**Frame.** Where is workflow progress kept?

**Answer.** In **Temporal's history**, stored durably by the Temporal server (in its own PostgreSQL database), one history per workflow ID. Business state is kept in parallel in the **incident record** (Q8.39). Workers keep nothing important locally; any worker can replay any workflow.

### Q8.14 A workflow exists in the system, but no process is available to execute its next operation. Which component should poll for and execute pending work?

**Frame.** Who picks up waiting work?

**Answer.** **Temporal workers**, which long-poll a **task queue**:

- the **worker process** polls `aisre-investigation`, for workflow tasks and investigation activities
- the **dispatcher process** polls `aisre-dispatcher`, for production writes and reconciliation only

If no worker is running, tasks wait in the queue; nothing is lost. An alert fires if a queue's oldest task waits longer than 60 seconds.

### Q8.15 Multiple workflows need to be distributed across different worker processes. How should work be routed to the appropriate workers?

**Frame.** How does work reach the right process?

**Answer.** By **task queue**, chosen per activity:

| **Task queue** | **Polled by** | **Carries** | **Why** |
| --- | --- | --- | --- |
| `aisre-investigation` | Worker process (any number) | Workflow tasks; evidence, knowledge, diagnosis, review, patch, sandbox control, approval, verification, notification activities | No production credentials here |
| `aisre-dispatcher` | Dispatcher process only | `dispatch_change`, `reconcile_execution` | Only this process holds production write access (Artifact 5) |

Concurrency is set per worker: at most 5 workflows at once, plus a limit on concurrent model activities. Temporal balances tasks across all workers on a queue.

### Q8.16 A workflow needs to call an external API or database. Which execution unit should perform this operation?

**Frame.** What runs external calls?

**Answer.** **An activity.** Workflow code never calls out (Q8.9). Each activity:

- has one job
- takes a small input, including its idempotency key
- returns a small output (large items travel as artifact IDs)
- reports heartbeats if it runs long
- is listed in the activity inventory (8.2)

### Q8.17 A model provider becomes unavailable temporarily. How should the platform retry a failed external operation?

**Frame.** How are model calls retried?

**Knowns.** Artifact 4: 3 attempts per step, 60 s per call; provider outage → evidence only.

**Answer.**

- **Inside the Model Gateway only:** up to 3 attempts per step, backoff 2 s → 4 s, each call limited to 60 s, each attempt reserving budget first.
- **The** **`diagnose`** **activity has** **`maximum_attempts = 1`** in Temporal, so Temporal doesn't multiply the gateway's retries (Q8.44).
- **After 3 failures,** the activity returns, and the workflow treats the step as inconclusive: it hands over with the evidence and timeline.
- **A circuit breaker** in the gateway stops calling a failing provider for a short period, so other incidents fail fast instead of waiting.

### Q8.18 A model request takes too long and blocks workflow progress. Which time limits should be enforced around external operations?

**Frame.** Which timeouts apply?

**Answer.** Four layers of limit, from smallest to largest:

| **Limit** | **Applies to** | **Value (provisional)** |
| --- | --- | --- |
| Per call | One model request, inside the gateway | 60 s |
| Start-to-close | One activity attempt | Per activity (8.5); `diagnose` 300 s |
| Schedule-to-close | All attempts of one activity | 4× start-to-close, capped by the investigation's remaining time |
| Investigation | Evidence to diagnosis | 15 min, from `workflow.now()` |

Long activities (evidence, sandbox, verification) also send **heartbeats**; a missing heartbeat ends the attempt early instead of waiting for the full timeout.

### Q8.19 An investigation needs to pause until an engineer approves a patch. How can an external human decision resume the suspended workflow?

**Frame.** How does a person's decision wake the workflow?

**Answer.** With a **signal**:

1. The API records the decision, and writes an outbox row, in one transaction (Artifact 7).
2. The outbox relay sends the `approval_decided` signal, with the approval's ID, action ID and patch fingerprint.
3. The workflow's signal handler ignores duplicates, stale actions and a different fingerprint. Otherwise it stores the decision.
4. The `wait_condition` sees the decision, and the workflow continues.

Tested: duplicate delivery of the same approval is ignored, and the change runs once.

### Q8.20 An approval request remains unanswered for 24 hours. How could the workflow wait without occupying a Python process?

**Frame.** How do we wait a long time without holding a process?

**Answer.** `workflow.wait_condition(..., timeout=...)` records a **timer in Temporal's history**. The worker drops the workflow from memory, and holds nothing. When the timer fires, or a signal arrives, Temporal hands the workflow to any worker, which replays to the waiting point, and continues.

In our design the wait is never 24 hours: approval escalates at **15 minutes** and expires at **60** (Artifact 4). The same mechanism holds the whole workflow open for up to 24 hours, waiting for the incident lead to close it. Tested with time-skipping: a 61-minute wait runs in under a second.

### Q8.21 An approval expires while the workflow is waiting. What should happen to the workflow?

**Frame.** What happens at expiry?

**Answer.**

1. At **15 minutes**, call `escalate_approval`: the next approver is notified. The workflow keeps waiting.
2. At **60 minutes**, call `close_approval_request` (reason: expired), so a late approval is refused by the API.
3. Transition: the incident goes from `awaiting_approval` to `handed_over`; the action goes to `expired`.
4. Hand over to on-call: "approval expired; no action taken".
5. Wait for the incident lead to close.

**Nothing is executed by default.** Tested: `test_unanswered_approval_escalates_then_expires`.

### Q8.22 An engineer requests cancellation while the workflow is running. How should the workflow respond?

**Frame.** How does the workflow react to cancellation?

**Answer.**

- **The request arrives as a signal,** `cancellation_requested`, through the outbox, carrying its own ID for deduplication.
- **The workflow stops at the next safe point:** between steps, or while waiting for approval.
- **In-flight production writes aren't interrupted** (`executing`, `reconciling`, `not_recovered`); see Q8.37.
- **On stopping:**
    - close any open approval request
    - withdraw the action, if it wasn't executed
    - transition to `cancelled`, recording who asked and why
- **Running read-only activities are cancelled;** the sandbox activity destroys its environment.
- **Temporal-level cancellation** (an administrator's emergency stop) is handled the same way.

Tested: cancellation while awaiting approval ends `cancelled`, with the action withdrawn.

### Q8.23 A new version of the workflow code is deployed while existing investigations are still running. How should the system preserve compatibility with their previous execution histories?

**Frame.** How do running workflows survive a code change?

**Answer.**

- **Safe changes** need nothing: changing an activity's implementation, or a timeout value read at call time.
- **Changes to the workflow's decisions** (adding, removing or reordering activity calls or waits) use **`workflow.patched("<change-id>")`**. Running workflows take the old branch; new ones take the new branch. Once no old workflow is running, `workflow.deprecate_patch` and remove the old branch.
- **Large redesigns** use a new workflow type (`IncidentWorkflowV2`). Old workflows finish on the old type.
- **Before every deploy,** CI replays a sample of recent production histories against the new code with Temporal's `Replayer` (as in the replay test). Any nondeterminism fails the build.
- **Workflows are short-lived** (at most 24 hours), so old branches are removed within a day of a change.

### Q8.24 Temporal stores detailed internal execution history, while the product needs an auditable business timeline. How should these two representations remain separate?

**Frame.** How do we keep Temporal's history and the business timeline apart?

**Answer.**

|  | **Temporal history** | **Business timeline** |
| --- | --- | --- |
| Written by | Temporal, automatically | The `record_transition` activity and the API |
| Contains | Activity attempts, retries, timers, signals, serialised inputs | Business events: status changes, approvals, executions, verifications |
| Format | Temporal's own | Our CloudEvents contracts (Artifact 7) |
| Retention | Short (provisional: 7 days after close) | 1 year, hash-chained |
| Readers | Platform engineers, for debugging | Engineers, reviewers, auditors, through the API |
| Permissions | Temporal's own | Per-service roles |

The workflow **never** exposes its history as the timeline. Every business step it takes is an explicit activity call that writes a business event (Artifact 7, Q7.53).

### Q8.25 An incident can be received, investigated, diagnosed, repaired and resolved. Which states should the incident be allowed to occupy?

**Frame.** What states can an incident be in?

**Answer.** Fourteen. Each one names a situation that needs different handling.

| **State** | **Meaning** |
| --- | --- |
| `open` | Accepted; workflow not yet running |
| `investigating` | Evidence, retrieval, diagnosis, review |
| `diagnosed` | Reviewed diagnosis, established |
| `inconclusive` | Root cause not established |
| `testing` | Patch being tested in the isolated environment |
| `awaiting_approval` | Waiting for a person |
| `executing` | Dispatcher applying the change |
| `reconciling` | Outcome unknown; establishing the real state |
| `verifying` | Recovery being checked over the hold period |
| `recovered` | Recovery held; waiting for the lead to accept |
| `not_recovered` | Fix didn't hold; undo in progress |
| `handed_over` | A person owns the next step |
| `cancelled` | Stopped before any production change |
| `closed` | Accepted and closed by the incident lead |

"Repaired" is deliberately not a state. A change being applied doesn't mean the service has recovered, so `executing` and `verifying` are kept separate.

### Q8.26 An incident arrives for the first time and has not yet been investigated. Which initial state should represent this condition?

**Frame.** What's the starting state?

**Answer.** **`open`****.** It is set by the API in the same transaction that writes the incident and its outbox row. It means "accepted and recorded, workflow not yet running". An incident stuck in `open` for over 2 minutes raises the alert from Artifact 7 (outbox not delivered). The workflow's first act is open → investigating.

### Q8.27 An investigation has collected evidence, but no root cause has been validated. What conditions should be satisfied before moving it from investigating to diagnosed?

**Frame.** What must be true to enter `diagnosed`?

**Answer.** All of these:

1. Evidence collection finished, with gaps recorded.
2. At least two hypotheses were considered, including "not the deployment".
3. The leading hypothesis cites existing evidence for every claim.
4. Confidence, scored from evidence strength, is at or above the threshold.
5. The **Diagnosis Reviewer accepted** it: citations valid, action on the allowed list, within the incident's service.
6. The investigation is still within its 15-minute limit.

If any one fails, the incident goes to `inconclusive`, then `handed_over`.

### Q8.28 A generated patch passes some tests but fails others. Under which conditions should it be allowed to move from testing to review?

**Frame.** When may a partly passing patch go forward?

**Answer.** **It may not.** A patch moves from `testing` to `awaiting_approval` only when **all** of these hold:

- the failure was **reproduced** before the patch was applied
- the original symptom is gone after the patch
- **all** existing tests that passed before the incident still pass
- no new error types appear

A partial pass is `test_failed`: the incident hands over, with the results attached. A person may still choose to fix it by hand, but the platform never asks someone to approve a patch it couldn't show works. Tested: "not reproduced" never reaches approval.

### Q8.29 An action is awaiting human authorization. Which conditions must be satisfied before it can transition into applying?

**Frame.** What must hold before a change is applied?

**Answer.** All of these, checked by the workflow, and again by the dispatcher:

1. A recorded `approved` decision from an authorised person, for this action.
2. A second approver, if the change is high-impact.
3. The approval's **patch fingerprint matches** the action's patch.
4. The approval's **signature** is valid.
5. The approval is **unexpired** (within 15 minutes).
6. The **service's version and configuration are unchanged** since approval.
7. No cancellation was requested.
8. Recovery criteria are recorded.

Then: action awaiting_approval → approved → applying, and incident awaiting_approval → executing.

### Q8.30 An approver rejects a proposed action. Which state should the action enter, and what should happen to the workflow?

**Frame.** What happens on rejection?

**Answer.**

- **Action** → `rejected`, which is final. The rejection and its reason are permanent records.
- **Incident** → `handed_over`, with the rejection reason.
- **The workflow** hands over to on-call, then waits for the incident lead to close. If the approver asked for more evidence, a person starts a recorded re-investigation (a new workflow run, `incident-<id>-r2`).

The platform never proposes another fix on its own after a rejection. Tested: no dispatch after rejection.

### Q8.31 An approval request remains unanswered beyond its expiry time. How should the action and incident state change?

**Frame.** What states follow expiry?

**Answer.**

- **Action:** awaiting_approval → expired (final).
- **Approval request:** closed, reason "expired".
- **Incident:** awaiting_approval → handed_over, reason "approval expired; no action taken".

Tested (Q8.21).

### Q8.32 An application crashes while a production operation is being executed. How should the platform represent the incomplete execution?

**Frame.** How is a half-finished production operation shown?

**Answer.**

- **Before calling the release process,** the dispatcher records the attempt with outcome `started`, its `execution_key`, and the action state `applying`. This is the record of intent.
- **If the dispatcher crashes,** the attempt stays `started`, and the action stays `applying`. Nothing claims success or failure.
- **The incident stays** **`executing`****.** Cancellation can't act on it.
- **When the activity is retried, or the workflow resumes,** the dispatcher sees the `started` attempt for this key, and **reconciles first** (Q8.34). It never applies the change again blindly.

### Q8.33 The dispatcher times out, but the external system may already have applied the change. How should this uncertain outcome be represented?

**Frame.** How do we record "it may or may not have happened"?

**Answer.** As its own state, never as success or failure:

- **Attempt outcome:** `unknown`.
- **Action:** applying → outcome_unknown.
- **Incident:** executing → reconciling.
- **Event:** `aisre.execution.completed.v1` with outcome `unknown`.

Only reconciliation can leave `outcome_unknown` (8.4 enforces it). Tested: an unknown outcome is reconciled, not retried.

### Q8.34 The platform cannot determine whether an external operation succeeded. Which reconciliation process should establish the actual state?

**Frame.** How do we find out what really happened?

**Answer.** The **`reconcile_execution`** activity, on the dispatcher queue, read-only:

1. **Ask the release process** for the change with this `execution_key`: not found, in progress, succeeded, or failed.
2. **If it's in progress,** wait and ask again, with backoff, for up to 10 minutes.
3. **Check the service itself:** compare the running version and configuration value with the target (in the reference case, is the configuration value reverted?).
4. **Decide:**
    - **Both say applied** → `executed`, then verify.
    - **Both say not applied** → `not_applied`. The incident hands over; whether to try again is a person's decision with a fresh approval.
    - **They disagree, or still unclear** → stay unknown; hand over with "production state uncertain; do not retry until a person checks".
5. **Record the result** as a new attempt row, and an event.

The platform **never** decides to apply again on its own while the outcome is unknown. Tested: still unknown after reconciliation → handed over, no verification.

### Q8.35 A repair is successfully applied, but independent verification detects continuing failures. How should the incident transition?

**Frame.** What happens when the fix doesn't hold?

**Answer.**

1. Verification result `not_recovered` → incident verifying → not_recovered.
2. The dispatcher applies the **pre-approved undo**, with its own key (`undo:<approval_id>`). An unknown undo outcome is reconciled the same way.
3. Action executed → undone (if the undo applied).
4. Incident not_recovered → handed_over: "fix did not hold; person decides next step".
5. The approver and on-call are notified.

Tested: the undo runs once, and the incident ends handed over.

### Q8.36 An engineer cancels an investigation before any external changes have occurred. Which state transitions should be permitted?

**Frame.** Where can cancellation go before any change?

**Answer.** To `cancelled`, from any of: `open`, `investigating`, `diagnosed`, `inconclusive`, `testing`, `awaiting_approval`. At the same time:

- an open approval request is closed
- the action becomes `withdrawn`, from proposed, testing, awaiting approval or approved
- the sandbox is destroyed

`cancelled` is final. A later problem is a new incident.

### Q8.37 An engineer requests cancellation after a production write has started. How should the system handle cancellation without pretending that the external operation has been reversed?

**Frame.** How do we handle a cancel that arrives mid-change?

**Answer.**

- **`cancelled`** **is not an allowed transition** from `executing`, `reconciling` or `not_recovered` (8.4). The record can't say "cancelled" when a change may have happened.
- **The write runs to its outcome:** the dispatcher activity is shielded from cancellation, and the workflow waits for it.
- **The request is recorded** as an event (`cancellation_requested`), with who and when, so it's visible.
- **What happens next depends on the outcome:**
    - **applied** → verification still runs, because the service must be checked either way; the incident ends `recovered` or `handed_over`
    - **not applied or unknown** → hand over
- **Undoing a change is a separate, explicit, approved action,** never a side effect of "cancel".

Tested: cancellation during a write leaves exactly one write, and the incident ends `closed` after verification, not `cancelled`.

### Q8.38 Two workers attempt to transition an incident into incompatible states. How should invalid transitions be prevented?

**Frame.** How do we stop conflicting state changes?

**Answer.** Four layers:

1. **One writer:** only the incident's workflow changes its state, and Temporal runs one workflow per ID.
2. **The transition table:** `check_incident` and `check_action` refuse any transition not in 8.4, in the workflow before it asks, and in `record_transition` before it writes.
3. **Optimistic concurrency:** `record_transition` writes only if `version = expected_version` (Artifact 7). A second, stale writer gets a conflict, and reloads.
4. **Idempotent transition IDs:** `<incident_id>:t<step>` is derived from history, so a retried transition is recognised, not applied twice.

### Q8.39 Several systems maintain information about incident progress. Which system should own the canonical business state?

**Frame.** Which system holds the truth about an incident?

**Answer.** **The PostgreSQL incident record** owns business state: status, versions, approvals, attempts, verifications, the timeline.

- **Temporal owns execution state:** which step the workflow is on, its timers and retries. That is internal and short-lived.
- **The workflow changes business state only through** **`record_transition`****,** so the two can't silently drift.
- **If they disagree** (for example after a database restore), the incident record wins. A reconciliation job compares open incidents with running workflows: it alerts on an incident with no workflow, or a workflow whose incident is closed (8.9).

### Q8.40 A workflow retries an operation after a worker crash, and the operation may be delivered more than once. What delivery guarantee does this represent?

**Frame.** What guarantee is "maybe more than once"?

**Answer.** **At-least-once.** The operation is never lost, but it may run more than once. That's what Temporal gives activities, and what the outbox gives messages. It's made safe by idempotency keys on every operation (Artifact 7, 7.8).

### Q8.41 An application discards an operation if it cannot confirm delivery, even if that means the operation may never execute. What delivery guarantee does this represent?

**Frame.** What guarantee is "maybe never"?

**Answer.** **At-most-once.** The operation runs once or not at all; it is never repeated. It suits things where a duplicate is worse than a loss, and loss is tolerable, such as a metric sample. We don't use it for incidents, approvals or changes. Production writes use at-least-once delivery with an execution key, and reconciliation when the outcome is unknown, which gives **at most one effect** without losing the decision.

### Q8.42 An external API may successfully execute a write even when the caller receives a timeout. Why is guaranteeing exactly-once execution of arbitrary external side effects difficult?

**Frame.** Why can't we promise exactly once?

**Answer.**

- **A timeout doesn't say what happened:** the request may never have arrived, may have been applied, or may still be running. The caller can't tell these apart.
- **Retrying risks doing it twice; not retrying risks doing it never.**
- **Exactly-once needs the receiver's cooperation:** it must recognise a repeated request (an idempotency key) or let us ask what happened (a lookup). An arbitrary external system may offer neither.
- **No shared transaction** spans our database and an external system (Artifact 7, Q7.56).

So the platform aims for **effectively once**: an idempotency key, a lookup by key, a check of the real state, and a person when all of those fail.

### Q8.43 An incident triggers an API retry, a workflow retry and an SDK retry simultaneously. How could these independent retries multiply the number of requests?

**Frame.** How do stacked retries multiply?

**Answer.** Retries at different layers **multiply, not add**. With 3 attempts each:

- the SDK makes 3 calls per activity attempt
- Temporal makes 3 activity attempts
- the client retries the API request 3 times

That is up to **3 × 3 × 3 = 27 calls** for one logical request. Under a provider outage, that load arrives just when the provider is weakest. It also spends budget 27 times, and stretches the delay to the sum of every layer's backoff.

### Q8.44 Three components independently retry the same failing model request. How should retry ownership be centralized?

**Frame.** Who should own model retries?

**Answer.** **The Model Gateway, and only it.**

- **Provider SDK:** its built-in retries are turned off (`max_retries = 0`).
- **Model Gateway:** 3 attempts per step, backoff, circuit breaker, budget check before each attempt.
- **Temporal:** the `diagnose` activity has `maximum_attempts = 1`. A crashed worker leads to one replay of the activity, which reuses a recorded result (Q8.11).
- **API clients:** they never retry model work; they only submit incidents (which are idempotent).

The same rule applies everywhere: **one layer owns retries for each operation** (8.5 lists the owner).

### Q8.45 A remote operation times out, but the external process may still be running. How should the system handle the uncertain execution?

**Frame.** What do we do while an operation may still be running?

**Answer.**

- **Record it as unknown,** not failed (Q8.33).
- **Don't start a competing operation** on the same target.
- **Reconcile:** ask by key, with backoff, until the external system reports a final status, or the reconciliation limit (10 minutes) is reached.
- **Check the actual state** of the target.
- **Then act on facts:** verify, hand over, or (with a person's approval) try again.

For read-only operations (evidence, verification) the rule is simpler: discard the late result, and retry or record a gap.

### Q8.46 PostgreSQL becomes unavailable while a workflow is collecting evidence. Which operations can safely continue, and which must stop?

**Frame.** What can carry on without the database?

**Answer.**

| **Continues** | **Stops** |
| --- | --- |
| Read-only evidence queries already running (results held in the activity, and retried for storage) | Recording any transition (`record_transition` retries with backoff) |
| Model calls already reserved | New model calls (budget reservation needs the database) |
| Temporal timers and waits | Approvals (they must be recorded first) |
| Workflow state in Temporal (separate database) | Any production write (no intent record, no write) |
| Health checks | Intake (`/readyz` fails, returns `503`, so monitoring retries) |

Rule (Artifact 4): **nothing that changes production, or can't be recorded, runs while the record can't be written.** When the database returns, activities complete, and the workflow continues from history.

### Q8.47 Temporal becomes unavailable while the API is receiving new incidents. How should the platform preserve incoming work?

**Frame.** How do we keep new incidents when Temporal is down?

**Answer.** **Through the outbox** (Artifact 7, 7.7):

- Intake doesn't depend on Temporal. It writes the incident, its event and an outbox row to PostgreSQL, and returns `201` as normal.
- The outbox relay can't deliver, so it retries with backoff. Rows wait in the outbox.
- The alert fires when the oldest pending row is older than 2 minutes, and on-call is told to handle new incidents manually for now.
- When Temporal returns, the relay starts the waiting workflows, oldest first. Stable workflow IDs prevent duplicates.

Running workflows simply pause, and resume from their history.

### Q8.48 A production write has an unknown outcome after a timeout. How should the platform establish the external state before deciding whether another operation is safe?

**Frame.** How do we know the real state before doing anything else?

**Answer.** The **reconciliation process** (Q8.34, 8.9):

1. **Look up by execution key** in the release process.
2. **Read the target's actual state:** its version and the configuration value.
3. **Wait out anything in progress,** with backoff, up to 10 minutes.
4. **Only on a definite answer** move to verification (applied) or hand-over (not applied).
5. **If still unknown,** hand over with an explicit warning, and allow no further production operation on that service until a person records the actual state.
