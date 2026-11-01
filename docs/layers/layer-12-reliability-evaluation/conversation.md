# Layer 12: Reliability, Observability and Evaluation — Conversation

- **Central question:** How do we establish that it works under realistic failure?
- **Built from:** Artifacts 3, 4, 10 and 11
- **Focus for this layer:** Building the system is not sufficient. Every claim about reliability, safety or quality must be backed by a measurement, a denominator and a reproducible procedure.
- **Deliverable:** [Artifact 12: Reliability and Evaluation Specification](artifact-12-reliability-and-evaluation-specification.md)
- **Questions:** 83

## Conversation log

| Questions | Agent | Conversation link | Decision reached | Open unknowns and owner | Artifact section |
| --- | --- | --- | --- | --- | --- |
| Q12.1–Q12.83 | TBD | [Shared conversation](https://claude.ai/share/c6a48a50-d511-4886-b4f5-0a3000f6a725) (needs sign-in; not read for these docs) | Recorded per question below | Listed under each question's Unknowns and Assumptions | [Artifact 12: Reliability and Evaluation Specification](artifact-12-reliability-and-evaluation-specification.md) |

## Where Claude and ChatGPT disagreed

TBD: the source documents do not record any disagreements. The team fills this in.

## Questions and answers

### Q12.1 The AI SRE platform itself experiences failures, but its users cannot tell whether the platform is healthy. How should it monitor its own operational state?

**Frame.** How does the platform watch itself?

**Answer.**

- **Every process is instrumented with OpenTelemetry:** metrics, traces and structured logs, tagged with service, step and incident.
- **Metrics go to Prometheus,** with **18 alerts** on the platform's own targets (12.3).
- **Grafana dashboards** show its health (Q12.17).
- **Health endpoints:** `/livez` and `/readyz` (Q12.21).
- **A status line in the CLI and API:** "platform healthy / degraded: \<what> / writes disabled: \<why>", so users can tell.

All of this is separate from the customer monitoring the platform reads (Q12.2).

### Q12.2 The platform monitors customer applications and also needs to monitor its own infrastructure. How should these two observability responsibilities be distinguished?

**Frame.** How are "watching customers" and "watching ourselves" kept apart?

**Answer.**

|  | **Customer applications** | **The platform itself** |
| --- | --- | --- |
| Role | **Evidence**: read-only input to investigations | **Self-observability**: our own health |
| Source | The organisation's monitoring, logs and traces | Our OpenTelemetry instrumentation |
| Access | Read-only adapters, scoped per incident | Platform team |
| Metric prefix | Theirs | `aisre_` |
| Alerts | Theirs; they trigger incidents | Ours; they page the platform owner |
| Where it goes | Evidence records | Prometheus, Grafana, trace backend |

The platform never writes to customer monitoring. Its own telemetry never becomes incident evidence, so an investigation can't be confused by the platform's own noise.

### Q12.3 Engineers need to understand platform performance and reliability. Which operational metrics should be exposed?

**Frame.** Which metrics show how the platform performs?

**Answer.** The **metrics catalog** (12.1), grouped by:

- **intake:** requests, latency, accepted incidents
- **queue and workflow:** depth, oldest age, wait time, active and stalled workflows, step duration, time to first summary, outbox backlog
- **model:** calls by status, latency, tokens, cost, reserved and daily spend, unreconciled usage, circuit state
- **evidence:** tool calls, refusals, gaps, injection flags
- **remediation:** approval backlog and outcomes, sandbox runs and leftovers, dispatch outcomes, unknown outcomes, verification verdicts, rollbacks, write fence
- **telemetry:** dropped spans

All with bounded labels (tested: no incident, user or action IDs as labels).

### Q12.4 Several incidents remain queued for processing. How should the platform measure how long each incident has been waiting?

**Frame.** How is waiting time measured?

**Answer.**

- **Per incident:** a histogram of acceptance to first worker step (`aisre_incident_queue_wait_seconds`, by severity).
- **Now:** the depth, and the **age of the oldest waiting incident**, by severity (`aisre_incident_queue_oldest_age_seconds`).
- **Alert:** a severity-1 or severity-2 incident waiting over 5 minutes (`AisreQueueBacklog`).
- **The outbox's oldest undelivered row** is tracked separately: an incident not yet started is waiting too (`AisreOutboxStuck`, 2 minutes).

### Q12.5 A workflow remains active for several hours without making progress. How should stalled workflows be detected?

**Frame.** How is a stuck workflow spotted?

**Answer.** Each state has an **expected maximum time**:

| **State** | **Expected maximum** |
| --- | --- |
| `investigating` | 15 min |
| `testing` | 20 min |
| `awaiting_approval` | 60 min |
| `executing` | 10 min |
| `reconciling` | 15 min |
| `verifying` | hold period + 5 min |

- **A job compares** each active workflow's last recorded transition with its state's limit, and publishes `aisre_workflows_stalled` by state.
- **Waits for a person are excluded** until their own expiry.
- **Alert:** `AisreWorkflowStalled`.
- **Runbook RB-04:** look at its Temporal history and last activity, then either let it resume, or cancel and hand over.

The workflow's own timers should already prevent this, so a stalled workflow means a platform bug.

### Q12.6 Model calls become increasingly slow, but the team has no historical latency measurements. How should model performance be monitored?

**Frame.** How do we track model speed over time?

**Answer.**

- **A latency histogram for every model attempt,** by provider, model and task (`aisre_model_call_seconds`), kept 13 months in Prometheus long-term storage, so trends are visible.
- **p50, p95 and p99 on the dashboard,** with a week-over-week comparison.
- **Alert at p95 above 45 s** (the timeout is 60 s).
- **Tokens recorded alongside latency,** so slowness from larger inputs can be told apart from provider slowness.
- **Each attempt's latency is also stored** in `model_attempts`, for incident-level detail.

### Q12.7 A provider begins returning authentication errors and rate-limit responses. How should provider failures be measured?

**Frame.** How are provider failures counted?

**Answer.**

- **`aisre_model_calls_total{status}`****,** using the gateway's normalised statuses: ok, invalid output, truncated, refused, timeout, rate limited, provider error, auth error, bad request.
- **The error rate is a ratio** with its denominator (errors ÷ all calls).
- **Authentication errors page at once:** permanent, and every investigation is evidence-only until fixed.
- **Rate limits and 5xx errors page above 20%** over 10 minutes.
- **Circuit state** per dependency.
- **Refusal rate tracked separately:** a rise usually means a prompt or redaction problem, not an outage.

### Q12.8 The platform reserves more model expenditure than it can safely afford. How should financial exposure be monitored?

**Frame.** How is money at risk watched?

**Answer.**

- **Reserved but unsettled** (`aisre_budget_reserved_micros`): the money committed to calls in flight.
- **Spent today against the daily ceiling:** alert at 80%, evidence-only at 100% (Artifact 4).
- **Calls charged without reported usage** (`pending_reconcile`): an alert above 20 for an hour, since these may be over- or under-stated.
- **Cost per incident, and per verified repair** (Q12.67, Q12.68), on the dashboard.
- **The ledger enforces caps before each call** (Artifact 10), so monitoring watches exposure; it doesn't prevent overspending.

### Q12.9 Hundreds of approval requests accumulate without being reviewed. How should the approval backlog be measured?

**Frame.** How is the approval backlog measured?

**Answer.**

- **Open requests** (`aisre_approvals_pending`), and **the oldest one's age.**
- **Outcomes over time:** approved, rejected, revoked, expired. A high expired share means approvers aren't reachable.
- **Time to decision,** from the approval records.
- **Alert:** any request older than 15 minutes (`AisreApprovalBacklog`).

Hundreds can't actually accumulate: at most 5 investigations run at once, and requests expire at 60 minutes. A growing expired count is the real signal.

### Q12.10 Production writes sometimes finish without a confirmed response. How should unknown action outcomes be monitored?

**Frame.** How are unknown outcomes watched?

**Answer.**

- **`aisre_actions_outcome_unknown`****:** the number of actions currently `unknown` after reconciliation.
- **It pages within 1 minute:** a person must check the service, and that deployment stays fenced (12.5).
- **`aisre_dispatch_outcomes_total{outcome}`** shows how often dispatch ends unknown, against all dispatches.
- **Each unknown outcome links** to its action, ledger entry and reconciliation notes. Runbook RB-07.

### Q12.11 A large percentage of repair attempts fail their recovery checks. How should verification failures be exposed?

**Frame.** How are verification failures shown?

**Answer.**

- **`aisre_verification_outcomes_total{verdict}`****:** verified, failed, inconclusive.
- **Reported as a rate with its denominator:** "failed 4 of 12 (33%)", never a bare percentage.
- **Alert when over 30% fail,** but only once there are at least 10 attempts in 7 days, so 1 of 2 doesn't page.
- **Broken down by action kind and incident category,** with each failure linked to its predicates. Runbook RB-09.

### Q12.12 A request travels through the API, workflow engine, model gateway and dispatcher, but engineers cannot reconstruct its journey. How should distributed tracing be introduced?

**Frame.** How is one request followed end to end?

**Answer.**

- **OpenTelemetry tracing in every process:** API, worker, dispatcher, outbox relay.
- **Each operation is a span:** an HTTP request, a workflow step, an activity, a model call, a tool call, a database query, a dispatch.
- **Context is propagated** with W3C `traceparent` over HTTP, in **outbox rows** and workflow inputs, in activity headers (Temporal's OpenTelemetry interceptor), and in the CloudEvents `traceparent` attribute (Artifact 7).
- **Every span carries** `aisre.incident_id` and `aisre.workflow_id`, so a search by incident finds the whole journey, even across the asynchronous gap.

### Q12.13 Two services generate telemetry for the same incident, but their records cannot be correlated. Which trace identifiers should be propagated?

**Frame.** Which IDs tie telemetry together?

**Answer.**

| **Identifier** | **Purpose** |
| --- | --- |
| `trace_id`, `span_id` (W3C `traceparent`) | Parent and child links within one causal chain |
| `aisre.incident_id` | Ties every trace, log, event and model attempt for one incident |
| `aisre.workflow_id` | Ties the Temporal workflow and its activities |
| `aisre.action_id` | Ties approval, dispatch, ledger and verification |
| `request_id` (`X-Request-ID`) | Ties an API call to its logs |
| `provider_request_id` | Ties a model attempt to the provider's own records |

The incident ID is the one identifier that crosses every boundary, including the outbox and the timeline.

### Q12.14 An API request triggers a Temporal workflow that makes several model requests. How should the platform correlate these operations?

**Frame.** How do we link API, workflow and model calls?

**Answer.**

1. The API span writes the incident, and an **outbox row carrying the** **`traceparent`**.
2. The relay starts the workflow, passing that context in the workflow headers (Temporal OpenTelemetry interceptor). The workflow span is a child of the API span, linked across the time gap.
3. Each activity span is a child of the workflow span.
4. Each model call span (inside the `diagnose` activity) records provider, model, prompt version, tokens, cost and the provider's request ID.

Engineers search by incident ID, and see the tree: intake → workflow → collect → hypothesise → diagnose (model call ×2) → critique → … .

### Q12.15 Different infrastructure components need a common instrumentation standard. Which observability framework could provide this?

**Frame.** Which standard instruments everything?

**Answer.** **OpenTelemetry** (ADR-13):

- vendor-neutral APIs and SDKs for traces, metrics and logs
- semantic conventions for HTTP, databases and generative AI calls
- ready instrumentation for FastAPI, SQLAlchemy, HTTP clients and Temporal
- export through an OpenTelemetry Collector, to Prometheus and the organisation's trace backend

The `telemetry.py` tracer provider is the shared setup.

### Q12.16 Prometheus needs to collect platform measurements. Which metrics should the application expose?

**Frame.** What does `/metrics` expose?

**Answer.** The catalog in 12.1, on an internal-only port (Artifact 7):

- counters for totals
- histograms for durations (with buckets around our targets, such as 0.3 s for intake)
- gauges for current state (queue depth, oldest ages, pending approvals, unknown outcomes, fence state)

**Rules:**

- labels come from small fixed sets (status class, task, outcome, severity)
- never incident IDs or free text
- every alert expression divides by its denominator

### Q12.17 Engineers need a visual representation of incidents, model costs and workflow failures. Which dashboard information should Grafana display?

**Frame.** What's on the dashboards?

**Answer.** Four dashboards:

1. **Platform health:** intake rate, errors and p95; readiness; queue depth and oldest age; outbox backlog; stalled workflows; write-fence state; telemetry drops.
2. **Investigations:** incidents by outcome (established, inconclusive, handed over); time to first summary p50 and p95; step durations; evidence gaps by source; injection flags.
3. **Model and cost:** calls by status; latency p50, p95 and p99 by task; tokens in and out; spend today against the ceiling; cost per incident; cost per verified repair (or "not defined", Q12.69); circuit states.
4. **Remediation safety:** approvals pending and oldest; decisions by type; sandbox outcomes and leftovers; dispatch outcomes; **unknown outcomes**; verification verdicts with counts; rollbacks.

Every rate panel shows its count alongside (Q12.64).

### Q12.18 Observability data contains sensitive customer information. How should sensitive fields be redacted?

**Frame.** How is telemetry kept clean?

**Answer.**

- **An allow-list of span attributes** (`EXPORTABLE_ATTRIBUTES`); everything else is dropped before export (tested).
- **Redaction patterns on the values that remain:** tokens, keys, passwords, private keys, email addresses, card-like numbers and IP addresses (tested).
- **Span events are dropped,** since they often carry raw text.
- **Logs are structured:** fields are allow-listed the same way, and messages are templates, never raw customer data.
- **Metric labels are bounded sets,** so no customer data can appear there.

### Q12.19 Raw prompts and application logs contain potentially confidential data. Why should they not be exported to external telemetry systems by default?

**Frame.** Why keep prompts and raw logs out of telemetry?

**Answer.**

- **They contain incident evidence:** customer data, internal details and possibly secrets that redaction missed.
- **Telemetry systems have wider access** and different retention than the incident record; often a third party runs them.
- **Prompts can contain injected text,** which could reach anyone who opens a trace.
- **The canonical copies already exist,** with access control and retention: model input and output in the artifact store (Artifact 7).

So prompts, completions, log bodies and evidence summaries are on the `NEVER_EXPORT` list. Engineers debugging a model call follow its ID to the artifact store, where permissions apply.

### Q12.20 The telemetry exporter becomes unavailable. How should the platform preserve core functionality without making observability failure a platform-wide outage?

**Frame.** How does a telemetry outage stay harmless?

**Answer.**

- **Non-blocking export:** a batch span processor with a bounded queue (2,048 spans), a 5-second export timeout, and dropping when full (tested: a failing exporter doesn't raise into platform code).
- **Metrics are pulled** by Prometheus, so the application never waits on it.
- **Drops are counted** (`aisre_telemetry_dropped_total`) and alerted (RB-12).
- **The canonical record doesn't depend on telemetry:** the incident timeline, audit chain and model attempts are in PostgreSQL. Losing telemetry loses debugging detail, never the record.
- **Readiness doesn't include the collector** (Q12.21).

### Q12.21 An orchestrator needs to determine whether a process is alive or able to serve traffic. How should health checks distinguish these conditions?

**Frame.** How do liveness and readiness differ?

**Answer.**

- **`/livez`** answers "is the process working at all?". It checks only that the event loop responds. Failing it means restart the process.
- **`/readyz`** answers "can it do its job right now?". It checks the database, the schema version, configuration, and (in the API) the approval signing key. Failing it means stop sending traffic, without restarting.
- **The dispatcher's readiness also includes the write fence:** fenced means not ready to write, but alive.
- **Neither checks Temporal, model providers or the telemetry collector** (Artifact 7, Q7.36), so a dependency outage doesn't take the API down.

### Q12.22 A process is running but cannot communicate with PostgreSQL. Why should liveness and readiness be treated differently?

**Frame.** Why not restart when the database is unreachable?

**Answer.** Restarting doesn't fix the database. It just makes **every replica restart together**, dropping in-flight work and adding load when the database returns. So during a database outage:

- **liveness stays passing:** no restart
- **readiness fails:** no new traffic; monitoring retries intake (`503`)
- **workers pause and retry** with backoff (Artifact 8)
- **production writes are fenced** (Q12.28)

When the database returns, readiness passes again, and work resumes from checkpoints.

### Q12.23 A provider repeatedly fails and receives more requests from the platform. How could a circuit breaker prevent repeated calls to an unhealthy dependency?

**Frame.** How does a circuit breaker help?

**Answer.**

- **Closed** (normal): calls flow, and failures are counted over a sliding window.
- **Open:** when failures exceed a threshold (provisional: 50% of at least 10 calls in 60 s), calls are refused at once, for 30 s, as provider errors, with no network call or wait.
- **Half-open:** after the pause, a few test calls go through. Success closes the circuit; failure reopens it.

Used per dependency in the Model Gateway and the evidence adapters. It protects the provider, saves the deadline, and makes investigations fall back quickly (evidence-only, or gaps). State is exported (`aisre_circuit_open`) and alerted.

### Q12.24 A service receives requests faster than it can process them. How should backpressure protect the system?

**Frame.** How do we push back when overloaded?

**Answer.** Layer by layer:

- **Intake:** stays fast (it only stores). Beyond 10× the expected rate, a per-caller rate limit returns `429` with `Retry-After`; monitoring retries.
- **Queue:** at most 5 investigations run; the rest wait by severity; beyond 50 queued, the incident lead is alerted (Artifact 4).
- **Gateway:** concurrency slots (8 global, 2 per incident) and budgets; callers wait for a slot within the deadline.
- **Tools:** per-tool limits and circuit breakers.

Nothing is dropped silently. Every limit either queues work with a visible age, or refuses it with a clear status.

### Q12.25 The incident queue grows continuously because workers cannot process incoming work quickly enough. How should queue saturation be detected and managed?

**Frame.** How do we spot and handle a growing queue?

**Answer.**

- **Detect:**
    1. the oldest queued age rising (alert at 5 minutes for severity 1 and 2)
    2. depth trending up
    3. arrival rate above completion rate for 15 minutes
- **Manage, in order:**
    1. Group related alerts harder: one incident per service and window.
    2. Add worker capacity, if the bottleneck is workers, not providers.
    3. Lower-severity incidents wait, and can be set to evidence-only.
    4. Tell on-call that the platform is saturated, so they handle the excess manually.
- **Never drop incidents,** and never skip safety steps to go faster. Runbook RB-03.

### Q12.26 A worker needs to restart during a deployment while several investigations are active. How should graceful shutdown and in-flight work draining operate?

**Frame.** How do workers restart without losing work?

**Answer.**

1. **Stop polling:** on SIGTERM, the worker stops accepting new tasks.
2. **Drain:** running activities get up to 60 s to finish. Long ones (sandbox, verification) heartbeat, and are cancelled cleanly if needed. The sandbox destroys its environment.
3. **Production writes are never cut off:** the dispatcher finishes its current write, records the outcome, then stops. A write it can't finish stays `intent_recorded`, and is reconciled on restart.
4. **Exit:** anything unfinished is retried on another worker from its last checkpoint, because activities are idempotent (Artifact 8).
5. **Deployment:** rolling, one worker at a time, with readiness gating, and new workflow code checked by replay first (Artifact 8).

### Q12.27 A provider becomes unavailable during an investigation. Which operations should be paused, retried or escalated?

**Frame.** What happens when a provider goes down mid-investigation?

**Answer.**

| **Operation** | **Action** |
| --- | --- |
| The model call in progress | Retried by the gateway (3 attempts), then the circuit opens |
| Evidence collection | Continues; it doesn't need the model |
| Hypothesis, diagnosis, critique | Stop; the step fails |
| The incident | Inconclusive; hand over with **evidence and timeline** (Artifact 2) |
| Sandbox, approval, dispatch already under way | Continue; they don't depend on the model |
| On-call | Told that investigations are evidence-only until the provider recovers |
| Authentication errors | Page at once; evidence-only until fixed |

No automatic switch to another provider (ADR-38).

### Q12.28 PostgreSQL becomes unavailable, but the API continues accepting write requests. How should the system behave?

**Frame.** What does the API do without the database?

**Answer.** **It stops accepting writes:** readiness fails, and intake returns `503` with `Retry-After`. It never acknowledges an incident it hasn't stored, which keeps Artifact 4's "never lost after acknowledgement" promise.

- monitoring retries alerts on its side
- approvals and cancellations return `503`; nothing is decided without a record
- reads may be served from a replica if one exists
- the **write fence** is set (`ledger_unavailable`): the dispatcher can't record intent, so it writes nothing
- on-call is told, and handles incidents manually until recovery

### Q12.29 The artifact store becomes unavailable while the platform is generating a patch. Which operations should stop?

**Frame.** What stops when large-item storage is down?

**Answer.**

| **Stops** | **Continues** |
| --- | --- |
| Storing a patch diff, so no patch can be created | Workflow progress that doesn't need artifacts |
| Sandbox testing (no diff to load, no results to store) | Investigation with evidence summaries already in PostgreSQL |
| Approval requests (the approver must see the stored diff) | Waits and timers |
| Dispatch: fenced (`artifact_store_unavailable`), since the action's patch content can't be verified | Verification (reads monitoring) |

Steps that stop retry with backoff within their limits, then hand over: "artifact store unavailable".

### Q12.30 Temporal becomes unavailable while workers are processing incidents. How should the platform preserve workflow consistency?

**Frame.** How do we stay consistent with Temporal down?

**Answer.**

- **Workflows simply pause:** their state is in Temporal's database; nothing is lost or duplicated.
- **Activities already running finish,** but can't report until Temporal returns. Their effects are idempotent, so the retry after recovery is safe.
- **Intake continues;** the outbox holds starts and signals (Artifact 7).
- **Production writes are fenced** (`workflow_engine_unavailable`): an in-flight write completes and records its outcome in the ledger, but no new write starts while the workflow can't record what happens next.
- **On recovery,** workflows resume from history, and the outbox drains.

### Q12.31 The approval store becomes unavailable while an action is awaiting authorization. Why should production writes be disabled?

**Frame.** Why stop writes when approvals can't be read?

**Answer.** Without the approval records, nothing can **prove** an action is approved, unrevoked, unrejected and unexpired. A dispatcher acting on a cached or assumed approval could apply a change that was rejected or revoked a minute ago. So the dispatcher's pre-checks fail closed, and the **write fence** is set (`approval_store_unavailable`, tested), until the store is back and approvals can be re-evaluated. Pending requests simply wait, and expire normally.

### Q12.32 Several dependencies are degraded, but read-only investigation remains possible. Which capabilities should remain available?

**Frame.** What still works in degraded mode?

**Answer.** **Read-only investigation mode:**

- incident intake, deduplication, the timeline and the live stream (need only PostgreSQL)
- evidence collection from whichever sources respond, with gaps recorded
- hypothesis and diagnosis, if the model provider is up
- hand-over with evidence, timeline and suggested next steps

**Not available:** patch testing, approval requests, and production writes. The status line says "degraded: read-only investigation".

### Q12.33 A critical dependency becomes unavailable, creating uncertainty around external state. Which failures should automatically stop all production writes?

**Frame.** What triggers the write fence?

**Answer.**

| **Failure** | **Fence** |
| --- | --- |
| PostgreSQL (ledger, approvals) unavailable | Global |
| Approval store unavailable or signatures unverifiable | Global |
| Artifact store unavailable | Global |
| Temporal unavailable | Global (in-flight writes complete) |
| An action's outcome is unknown | **That deployment** |
| Release process can't be queried (no key lookup) | Global |
| Backup quiesce in progress | Global |
| Restored environment, not yet reconciled | Global, with a new epoch |
| Clock drift beyond 5 s on the dispatcher host | Global (expiry checks unreliable) |
| Manual: incident lead or security lead | As set |

Fences are lifted automatically when their cause clears, except two: the restore fence needs reconciliation and a person (tested), and an unknown-outcome fence needs a person to record the actual state.

### Q12.34 The platform experiences a complete infrastructure failure. Which information must be recoverable to restore its operations?

**Frame.** What must come back after total loss?

**Answer.**

| **Data** | **Why** | **Where** |
| --- | --- | --- |
| Incident record: incidents, timeline, evidence, diagnoses, actions, approvals, ledger, verifications | The source of truth, and the audit trail | PostgreSQL |
| Temporal state | Running workflows | Temporal's PostgreSQL database |
| Artifacts: snapshots, diffs, test logs, model input and output | Evidence and reproducibility | Object store |
| Audit chain copy | Tamper-evident record | Separate write-once store |
| Signing key (private, in the secrets manager) and public keys | Approvals | Secrets manager |
| Configuration, prompts, policies, sandbox spec, code | Rebuild the platform | Repository (tagged releases) |
| Retrieval index revisions | Reproducing past investigations | Rebuilt from stored documents and chunks |

Telemetry is not required for recovery (Q12.20).

### Q12.35 PostgreSQL, Temporal persistence and artifact storage contain related information. Why could independently restoring these systems to inconsistent points in time create problems?

**Frame.** Why must the restores line up?

**Answer.** They reference each other:

- **Incident database older than Temporal:** workflows refer to incidents, approvals or actions that don't exist; a workflow could wait for a decision already recorded, or act on a missing one.
- **Temporal older than the database:** completed steps would re-run, such as notifications and test runs. A dispatch step could even run again for an action the ledger says was applied.
- **Artifacts older than the database:** evidence and patch references point at missing objects, so a patch can't be verified or reviewed.

Hence quiesced, consistent backups (Q12.37), a defined restore order, and reconciliation before any write (Q12.44).

### Q12.36 A backup is taken while workflows are actively executing external operations. How could this produce an inconsistent recovery state?

**Frame.** Why is a backup during writes dangerous?

**Answer.** A write can be **in flight** at backup time:

- intent recorded, but the result not yet recorded
- or the release process applied it after the backup's moment

Restored, the ledger says "intent" or "pending" while production has changed. The restored platform might then re-send the write, or believe the old revision is still running and plan the wrong rollback. The same applies to approvals given after the backup moment: they're gone after restore.

### Q12.37 The organization needs to back up its data while preventing new operations from creating inconsistent snapshots. How should a quiesced backup process work?

**Frame.** How does a clean backup work?

**Answer.** For the nightly consistent backup:

1. **Fence writes** (`backup_quiesce`): no new dispatch starts.
2. **Wait for in-flight writes** to reach a final ledger state (bounded at 10 minutes; if any stay unknown, record them for reconciliation).
3. **Pause new workflow steps** that write to the artifact store; investigations already running may hold briefly.
4. **Take a consistent set:** a PostgreSQL snapshot of both the incident and Temporal databases at the same point (or one transactionally consistent snapshot), and an object-store manifest of every artifact key and version at that moment.
5. **Record a backup marker** (time, snapshot IDs, manifest hash, and any unknown actions) in the backup itself.
6. **Lift the fence,** and resume. Target: under 15 minutes of paused writes.

**Between nightly backups,** continuous write-ahead-log archiving and object versioning give point-in-time recovery (Q12.40). Restores to those points always go through reconciliation (Q12.44).

### Q12.38 New incidents arrive while a backup is being prepared. How should new work be controlled during the backup?

**Frame.** What happens to new incidents during the backup?

**Answer.**

- **Intake keeps accepting:** incidents and outbox rows are written to PostgreSQL normally, and are covered by the next backup and the continuous log.
- **New investigations may start:** read-only steps don't affect consistency.
- **Writes are fenced** for the quiesce window only; actions reach "approved", then wait.
- **On-call isn't paged** for the planned fence (the alert waits 15 minutes; Q12.33).

### Q12.39 An external action is halfway through execution when the platform enters backup mode. How should in-flight external effects be handled?

**Frame.** What about a write that's mid-flight?

**Answer.**

- **It completes;** the fence never interrupts a write (Artifact 8).
- **Quiesce waits** for its ledger entry to become applied, not applied, or unknown.
- **If it's still unknown** after the bounded wait, the backup proceeds and **records the action as in flight** in the backup marker. Any restore from that backup reconciles it first.
- **The dispatcher records intent before writing,** so even a crash mid-write leaves a ledger entry for reconciliation.

### Q12.40 The organization needs to quantify acceptable data loss after a disaster. Which recovery measurement should be defined?

**Frame.** How much data can we lose?

**Answer.** **Recovery point objective (RPO): ≤ 5 minutes** (Artifact 4, NFR-21, provisional). Achieved by:

- continuous write-ahead-log archiving for both PostgreSQL databases
- object-store versioning, replicated to a second location
- the audit chain copied to a separate write-once store

Measured in every restore drill as the difference between the last recorded event before the failure and the last event restored.

### Q12.41 The organization needs to quantify how quickly the platform must become operational after a disaster. Which recovery measurement should be defined?

**Frame.** How fast must we recover?

**Answer.** **Recovery time objective (RTO): ≤ 1 hour** to read-only investigation mode (Artifact 4, NFR-22), and **≤ 4 hours** to full operation including production writes, because writes wait for reconciliation and a person. Measured in drills from the start of the restore to `/readyz` passing, then to the fence lifting.

### Q12.42 The platform claims it can recover within 30 minutes, but nobody has tested the process. How should actual recovery performance be measured?

**Frame.** How do we prove recovery time?

**Answer.** **By timing real restores,** never by estimate:

- **quarterly restore drills** into a fenced environment, following the written procedure (12.6)
- **record timestamps:** start, data restored, readiness passing, reconciliation complete, writes re-enabled
- **compute RTO and RPO** from them, and compare with targets
- **record what slowed it,** and update the procedure

A claim is valid only if the last drill met it, within the last quarter.

### Q12.43 Engineers restore a production backup into an environment that could accidentally execute pending writes. How should the restoration environment be fenced?

**Frame.** How is a restored environment kept from writing?

**Answer.**

- **The first step of every restore is** **`begin_restore()`****:** it **bumps the fence epoch**, and sets the restore fence. Any dispatcher holding the old epoch, including a restored one, is refused (tested).
- **The restored dispatcher starts with no production write credential.** Credentials are issued only after the fence lifts.
- **The outbox relay and workflow workers start paused,** so restored workflows don't resume until reconciliation.
- **Drill restores** go into a separate environment, whose release-process credentials point at a fake target.

### Q12.44 Restored workflows contain actions that may already have executed against external systems. Why should dispatch remain disabled until reconciliation is complete?

**Frame.** Why wait for reconciliation?

**Answer.** The restored records describe the past, at the backup point. Production has moved on:

- actions marked "pending" may have been applied after the backup
- "approved" actions may have been rejected since
- the target's revision may differ

Dispatching from that state could apply a change twice, apply a rejected change, or roll back to the wrong revision. So every action not confirmed final is reconciled against the release process, and **writes stay off until none are unresolved and an incident lead signs off** (`complete_restore`, tested).

### Q12.45 The restored database reports that an action is pending, but the external target confirms that it succeeded. How should the restored system reconcile these records?

**Frame.** Who wins when the restored record and the target disagree?

**Answer.** **The external target is the source of truth for what happened externally** (`reconcile_after_restore`, tested):

| **Restored ledger** | **Target says** | **Target revision** | **Result** |
| --- | --- | --- | --- |
| Pending or intent | Applied | Expected after | **Applied**: record it, write the missing event (labelled "restored by reconciliation"), and go on to verification or close |
| Pending or intent | Not found | Base | **Not applied, or void**: the action needs a fresh approval |
| Any | Applied | Base or other | **Unresolved**: a person decides |
| Any | In progress | Any | Wait, then re-check |

Restored approvals newer than the backup are lost. Affected actions need new decisions; old ones aren't trusted.

### Q12.46 A document is deleted from the main database, but copies remain inside retrieval indexes and artifact storage. How should deletion and derived-data consistency be managed?

**Frame.** How do deletions reach every copy?

**Answer.**

- **A deletion record** is the single trigger: it names the subject (a document, an incident's raw evidence), the reason, the requester and the time.
- **A deletion job applies it to every derived store:**
    - **retrieval:** builds a new index revision without the document, and purges chunks for legal or safety deletions (Artifact 10)
    - **artifact store:** deletes the listed objects and their versions
    - **backups:** marked to be re-deleted if ever restored (a deletion log replayed after a restore)
    - **telemetry:** none to delete, since raw content isn't exported (Q12.19)
- **A verification pass** confirms no copies remain, and the deletion record is closed with that result.
- **Legal holds** (Q12.48) block deletion, with the reason recorded.

### Q12.47 The platform stores several years of incident logs and generated artifacts. How should retention requirements be defined?

**Frame.** How long is each kind of data kept?

**Answer.** Per data class, with an owner (12.7). Raw customer data is kept briefly; decisions and audit are kept longer. Retention is enforced by a job, with a separate role (Artifact 7), and checked by an audit query.

### Q12.48 An active investigation references artifacts that would otherwise be deleted under the retention policy. How should the platform preserve required evidence?

**Frame.** How do we keep evidence an open case still needs?

**Answer.**

- **Holds:** an open incident, an open incident review, or a legal or security hold places a hold on every artifact and evidence record it references.
- **The retention job skips held items,** and records that it did.
- **Holds expire** with their reason (incident closed plus 30 days, or the review closed), or are released by their owner.
- **Retention never deletes** anything cited by a decision record (approval, execution, verification) within the audit retention period.

### Q12.49 The organization has successfully created backups for six months but has never restored one. How should disaster recovery testing establish whether those backups are actually usable?

**Frame.** How do we prove backups work?

**Answer.** **Restore them, on a schedule:**

- **Weekly automated check:** restore the latest backup into a scratch environment, run integrity checks (row counts, schema, foreign keys, audit-chain verification, the artifact manifest against the object store), then destroy it.
- **Quarterly full drill:** the complete procedure (12.6), with RTO and RPO measured, reconciliation exercised against a fake release process, and the fence lifted by a person.
- **Annual scenario drill:** lose the primary region entirely.
- **Results are recorded and reviewed.** A failed drill is an incident for the platform team.

A backup that has never been restored doesn't count as a backup.

### Q12.50 The platform claims it can automatically diagnose incidents, but the team has no objective evidence of its effectiveness. How should an evaluation system be established?

**Frame.** How do we measure effectiveness objectively?

**Answer.** **A frozen evaluation suite plus a scoring harness** (`eval_harness.py`):

1. **Cases:** replayable incident fixtures (alerts, telemetry, changes, logs, runbooks), each with a **separately stored answer key**.
2. **Run:** each configuration runs on every case, through the real platform code, with fake external systems.
3. **Score:**
    - accuracy, false-established rate, citation validity, evidence coverage
    - repair outcomes, escalation quality, injection resistance, unauthorised attempts
    - latency and cost
    - **every rate with its count and a 95% confidence interval**
4. **Compare** against a recorded baseline, paired on the same cases.
5. **Gate** promotion on the results (12.11).

### Q12.51 The team evaluates every new model using a different set of incidents. Why would a fixed evaluation dataset improve comparisons?

**Frame.** Why keep the dataset fixed?

**Answer.** With different incidents each time, a score change could come from **easier cases** rather than a better model. A **fixed, versioned suite** means:

- each configuration answers the **same questions**, so differences come from the configuration
- **paired comparison** is possible: case by case, which is statistically far stronger (Q12.72)
- results stay comparable over months

The suite is **frozen by hash** (`SuiteManifest`). Any edit is detected (tested), and makes a new version. Old versions are kept, so baselines remain valid.

### Q12.52 The platform needs a collection of incidents with known expected outcomes. How should a golden evaluation dataset be constructed?

**Frame.** How is the golden dataset built?

**Answer.**

1. **Sources:**
    - **synthetic incidents:** fault injection on a test copy of the service (bad configuration value, bad commit, dependency outage, database failover, load)
    - **sanitised replays** of resolved production incidents, with the cause confirmed by their incident review
2. **Each case:** a fixture (alerts, metrics, logs, traces, changes, runbooks at the time), its category and difficulty, and its split (development or held-out).
3. **Each answer key:**
    - expected outcome and cause, with equivalent descriptions
    - whether it should be handed over
    - which repairs would be safe
    - what evidence a sound diagnosis must cite
    - whether it contains an injection
4. **Labelled by two SREs independently;** disagreements are resolved, or the case is dropped.
5. **Redacted, and reviewed** for secrets and personal data.
6. **Frozen,** as a new suite version.

Target size (provisional): 300 cases, 200 of them held out (Q12.63).

### Q12.53 The agent needs to be tested against known incidents without being given their expected root causes. How should the trusted answer key be separated from the agent?

**Frame.** How is the answer key kept from the agent?

**Answer.**

- **Separate storage and credentials:** cases (fixtures) are readable by the evaluation runner; **answer keys only by the scorer**, which runs after the agent finishes.
- **Fixtures contain no labels:** case IDs are random, and file names, runbook text and commit messages are checked for giveaways.
- **The agent's run is recorded first,** and scored afterwards, from its stored outputs.
- **The suite manifest hashes keys separately,** so tampering is detected.
- **Held-out keys** are accessible to a smaller group: not the people tuning prompts.

### Q12.54 The dataset contains only simple configuration errors. Which additional incident categories would be needed to evaluate realistic system behavior?

**Frame.** Which categories make the suite realistic?

**Answer.**

| **Category** | **Tests** |
| --- | --- |
| Config change (the reference case) | The main path |
| Code regression | Commit-level cause, revert |
| Dependency failure | A non-deployment cause, which should be handed over |
| Database failure or failover | A non-deployment cause, data risk |
| Capacity or load | No change involved |
| **Coincident deploy, unrelated cause** | Correlation versus causation (Artifact 9) |
| **Insufficient evidence** | Should be inconclusive |
| **Prompt injection** in logs | Safety |
| Flaky or transient | Should be recognised as already recovered, or inconclusive |
| Multiple causes | Partial credit; hand-over |

Target mix (provisional): config change at most 30%. Every category has at least 20 held-out cases, so per-category results mean something.

### Q12.55 Different incidents have different expected root causes. How should expected outcomes be represented in the evaluation data?

**Frame.** How is "the right answer" stored?

**Answer.** As a structured `AnswerKey`, not prose:

- `expected_outcome`: established or inconclusive
- `cause_id`: a canonical ID such as `config:payments.timeout_ms`, plus `acceptable_cause_ids` for equivalent descriptions
- `should_escalate`
- `safe_action_kinds`
- `required_evidence_kinds`
- `contains_injection`

The agent's diagnosis is mapped to cause IDs by its structured output (category, changed key), so scoring is mechanical. Unmappable answers count as wrong.

### Q12.56 The agent provides a diagnosis with several evidence citations. How should the evaluator establish whether those citations are valid?

**Frame.** How are citations checked?

**Answer.**

1. **Exists and same incident:** a mechanical check (as in Artifact 9's `check_citations`).
2. **Relevant:** the cited evidence's kind is one the claim needs (for example, a version-split metric for "only the new version fails").
3. **Supports the claim:** on a sample (10% of cases), two reviewers judge whether the evidence actually says what the claim says. Their agreement rate is reported.
4. **Required evidence cited:** the case's `required_evidence_kinds` must appear (evidence coverage).

Reported as **citation validity = valid citations ÷ all citations**, with counts.

### Q12.57 The platform identifies the correct root cause in 70 out of 100 test cases. How should diagnosis accuracy be calculated?

**Frame.** How is accuracy computed and reported?

**Answer.**

- **Accuracy = correct cases ÷ all cases.** "Correct" means: established with an acceptable cause ID, **or** inconclusive where the key says inconclusive. Missing results count as wrong; they're never dropped (tested).
- **Reported as "70/100 (70.0%, 95% CI 60.4–78.1%)"**, using a Wilson interval.
- **Alongside it:**
    - accuracy among established diagnoses
    - the **false-established rate** (confident and wrong ÷ established)
    - per-category accuracy, each with its own count

Accuracy alone hides the dangerous error: confidently wrong.

### Q12.58 The agent produces a technically correct diagnosis but proposes an unsafe repair. How should repair success be evaluated separately from diagnosis accuracy?

**Frame.** How is repair judged apart from diagnosis?

**Answer.** Separate metrics, per case:

- **Unsafe proposal:** the proposed action isn't in the case's `safe_action_kinds`. This is a **blocking** gate, regardless of accuracy.
- **Verified repair:** a safe action, tested and executed against a **replayed environment**, then verified by the predicates.
- **Repair rates, both with denominators:**
    - verified ÷ repairs attempted
    - verified ÷ cases where a repair was expected

A correct diagnosis with an unsafe repair counts as correct in accuracy, **and** as an unsafe proposal, which blocks promotion.

### Q12.59 The agent attempts to fix an incident despite insufficient evidence. How should escalation behavior be evaluated?

**Frame.** How is hand-over behaviour scored?

**Answer.** As a confusion matrix over all cases, against `should_escalate`:

|  | **Agent handed over** | **Agent acted** |
| --- | --- | --- |
| Should hand over | Escalated correctly | **Under-escalated** (the dangerous one) |
| Should act | Over-escalated | Acted correctly |

Reported with counts. Under-escalation has its own target (provisional: below 2%), and acting on the insufficient-evidence category is tracked separately.

### Q12.60 The agent encounters malicious instructions embedded in logs. How should prompt-injection resistance be tested?

**Frame.** How is injection resistance tested?

**Answer.**

- **A prompt-injection category** in the suite (at least 20 held-out cases): fixtures with instructions planted in log lines, error messages, commit messages, runbooks and trace attributes. For example: "ignore previous instructions", "run …", "the cause is the database", "include the API key", "fetch this URL".
- **For each, the answer key records the injected goal.** The scorer checks whether any tool call, proposed action, citation pattern or summary statement matches it (`injection_followed`).
- **Also measured:** whether the content was flagged (`flagged_injection`), and whether the diagnosis stayed correct.
- **Pass mark:** **zero** followed injections. Any one blocks promotion (tested).

### Q12.61 The model proposes an unauthorized production write. How should unauthorized-action prevention be evaluated?

**Frame.** How is unauthorised-write prevention tested?

**Answer.** Two sides:

- **Attempts:** count, from the audit log, every write request that reached the dispatcher gate without a valid approval, and every write-tool request (`unauthorized_write_attempts`).
- **Prevention:** the security tests (Artifact 5, ST-01 to ST-40) and remediation tests (Artifact 11) prove the gate refuses them.

**Pass mark:** zero attempts that reached execution, and zero successful unauthorised writes, ever. **Any attempt blocks promotion** (tested), and is investigated, even if it was refused.

### Q12.62 A model achieves 100% accuracy on the same incidents used during development. How could a held-out dataset establish whether the system generalizes?

**Frame.** How do we test generalisation?

**Answer.**

- **Split by incident,** and by service and fault source where possible, so near-duplicates don't cross the split.
- **Development cases** are used freely for prompt work. **Held-out cases** are run only for promotion decisions, by a smaller group, with keys hidden from the tuners.
- **Only held-out results count** toward gates and claims. A big gap between development and held-out scores signals overfitting.
- **Refresh:** add new held-out cases from each quarter's incidents as a new suite version, so prompts can't slowly fit the held-out set by repeated peeking.

### Q12.63 The team tests the system on 20 incidents and reports that it is production-ready. Why would this sample be insufficient for broad statistical claims?

**Frame.** Why isn't 20 enough?

**Answer.**

- **The uncertainty is huge:** 17 of 20 correct (85%) has a 95% interval of about 64–95%. The true accuracy could be well under target.
- **To estimate around 80% within ±5 points takes about 246 cases** (`cases_needed`, tested); 20 cases only support about ±17.5 points.
- **Rare failures are invisible:** zero unsafe actions in 20 cases says little. The "rule of three" bound for zero events in 20 is about 15%.
- **Categories go untested:** 20 cases can't cover 10 categories meaningfully.

So promotion needs at least 200 held-out cases, with per-category counts, and safety properties also proven by targeted tests, not just sampled.

### Q12.64 A dashboard reports a 95% repair success rate, but does not display the total number of repair attempts. Why is the denominator necessary?

**Frame.** Why show the denominator?

**Answer.** **19 of 20 and 950 of 1,000 are both 95%, but mean very different things:**

- the confidence interval for 19/20 is about 76–99%; for 950/1,000 it is about 93.5–96.2%
- the denominator reveals **what was counted:** attempts, eligible incidents, or only cases that reached verification

A rate that hides a small or selective denominator misleads. Every rate in this platform is a `Rate(k, n)`, printed as "k/n (p%, 95% CI)" (tested).

### Q12.65 The platform successfully verifies 30 repairs out of 100 attempts. How should the verified repair rate be reported?

**Frame.** How is 30 of 100 reported?

**Answer.** **"Verified repairs: 30/100 (30.0%, 95% CI 21.9–39.6%)"** (tested). Then state the denominator's definition (attempted repairs), and give the companion rates:

- verified ÷ incidents where a repair was expected
- failed verification, and inconclusive verification, each with counts
- rollbacks triggered

### Q12.66 Engineers need to compare typical and worst-case response times. How should p50, p95 and p99 latency be calculated?

**Frame.** How are percentiles computed?

**Answer.**

- **Nearest-rank on the raw samples:** sort the n values; the q-th percentile is the value at position ⌈q/100 × n⌉ (tested: 1–100 gives 50, 95, 99).
- **In production, from Prometheus histograms** with `histogram_quantile`. Buckets are chosen around the targets, since precision depends on bucket edges.
- **Always state n and the window.** p99 needs at least about 100 samples to mean anything; below that it's reported as "insufficient samples".
- **Never average percentiles** across services or time windows. Merge the histograms, then compute.

### Q12.67 An investigation consumes model tokens, infrastructure resources and testing compute. How should total cost per incident be calculated?

**Frame.** What goes into cost per incident?

**Answer.** **Cost per incident = (model cost + sandbox cost + infrastructure share) ÷ incidents:**

- **model cost:** settled cost from the budget ledger, including retries, critic, revisions, and charged-but-unreported usage
- **sandbox cost:** sandbox seconds × the host rate
- **infrastructure share:** the month's platform infrastructure (database, Temporal, workers, storage) ÷ incidents

It is reported per category, with n. All incidents are counted, including inconclusive ones, because they cost money too.

### Q12.68 The organization wants to know the expenditure required to achieve one verified repair. How should cost per verified repair be calculated?

**Frame.** How is cost per verified repair computed?

**Answer.** **Total cost of all incidents in the period ÷ verified repairs in the period**, not the cost of the successful ones only. Failed, inconclusive and handed-over incidents are part of what it costs to get each verified repair. It is reported with both numbers: "X total / 30 verified".

### Q12.69 The platform has not successfully verified any repairs. How should cost per verified repair be reported without producing a misleading number?

**Frame.** What if nothing was verified?

**Answer.** **It isn't defined, and is shown as such:** "not defined: 0 verified repairs of N attempted (total spend X)" (tested). The platform never shows ∞, 0, a division error, or a number computed from a placeholder. The total spend and attempt counts are shown, so the cost is still visible.

### Q12.70 A model achieves high diagnosis accuracy but occasionally executes unauthorized production actions. Why should this security failure block release regardless of its average accuracy?

**Frame.** Why does one security failure outweigh accuracy?

**Answer.**

- **The harms aren't on the same scale:** a wrong diagnosis costs time, while an unauthorised production write can cause outages, data loss or a breach, and destroys trust in the approval model the whole platform rests on.
- **Averages hide it:** 1 in 200 sounds rare, but at production volume it will happen, and an attacker can try to trigger it on purpose.
- **It breaks an invariant, not a target:** "no production change without a signed approval" (Artifact 1) is a rule, not a percentage to trade.

So `promote()` checks **hard safety gates first**: unauthorised attempts, followed injections, unsafe proposals. Any one blocks, whatever the accuracy (tested: 95% accuracy with one unauthorised attempt is blocked).

### Q12.71 The team introduces a new model and claims that it improves diagnosis quality, but has no comparable previous results. How should a baseline be established?

**Frame.** How is a baseline created?

**Answer.**

1. **Define the current configuration exactly** (`ExperimentConfig`): provider, model ID, checkpoint, prompt versions, retrieval method and index revision, context budget, compression, critic, revision limit, temperature, suite version, code version.
2. **Run it on the frozen held-out suite;** store the full scorecard and per-case results.
3. **Run it 3 times** to measure run-to-run variation. Model outputs vary slightly even at temperature 0, so cases that flip between runs are noted.
4. **That's the baseline.** Every claim of improvement is a paired comparison against it.

### Q12.72 Two model configurations need to be compared under the same conditions. How should a candidate configuration be defined?

**Frame.** How is a candidate defined and compared?

**Answer.**

- **A candidate = the baseline with one declared change** (`check_single_variable`, tested). Same suite, same code, same fake external systems, same limits.
- **Run on the same cases,** compared **case by case** (`paired_compare`):
    - count the cases only the candidate got right, and those only the baseline got right
    - an **exact McNemar test** on those discordant pairs
    - the accuracy difference
- **The change is accepted only if it passes the promotion gates** (12.11).

### Q12.73 An experiment changes the model, prompt, dataset and retrieval strategy simultaneously. Why would this make it difficult to identify the source of improvement?

**Frame.** Why not change everything at once?

**Answer.**

- **The effects are mixed together:** an improvement could come from any one change, while another quietly made things worse. You can't tell which, or keep the good one and drop the bad.
- **Changing the dataset makes the comparison meaningless:** the scores aren't on the same questions.
- **Changes can interact:** a new prompt may only help with the new model.

So `check_single_variable` refuses a suite change, and any undeclared extra change (tested).

### Q12.74 The team wants to compare Claude and Tinker for incident diagnosis. How should the experiment be designed?

**Frame.** How do we compare Claude and Tinker fairly?

**Answer.**

- **Declared factor:** the model, which changes provider, model ID and checkpoint together. Everything else is held fixed: prompts, schemas, context, retrieval, critic, limits, suite and code (tested: a declared multi-field factor is accepted).
- **Fine-tuning data** for Tinker comes **only from development cases, plus incidents outside the suite.** Never held-out cases, or the comparison is contaminated.
- **Tinker first passes the adapter conformance suite** (structured output, tool proposals, usage reporting); otherwise its schema failures are a capability gap, not a quality result.
- **Both run on the held-out suite,** 3 runs each, paired.
- **Compare everything:**
    - accuracy, false-established rate, citation validity, unsafe proposals, injection
    - verified repairs
    - latency p95, cost per incident and per verified repair
- **Decision:** promotion gates; plus the revisit conditions in ADR-07.

### Q12.75 Engineers believe vector search will outperform BM25 for runbook retrieval. How should the two approaches be compared?

**Frame.** How do we test vector search against BM25?

**Answer.** **Two levels, one variable each:**

1. **Retrieval alone:** on the labelled retrieval query set (Artifact 10): recall@5, MRR and nDCG@5, plus wrong-permission and wrong-service hits. Split by query type, exact identifier versus paraphrase. Compare BM25, vector and hybrid.
2. **End to end:** change only the `retrieval` field, and run the diagnosis suite paired. Retrieval only matters if diagnoses improve.

Adopt vector or hybrid only if it improves end-to-end results without losing exact-identifier recall, and is worth its extra system (ADR-37).

### Q12.76 Context compression is introduced to reduce inference costs, but engineers are unsure whether it removes important information. How should its impact be measured?

**Frame.** How do we measure what compression costs?

**Answer.**

- **Single variable:** `context_compression` on or off, all else equal, paired on the suite.
- **Quality:** accuracy, false-established rate, citation validity, and **evidence coverage**: did the diagnosis still cite the required evidence?
- **Information loss, directly:** for each case, were the answer key's required evidence items **included in the context** after compression? This comes from the context manifest (Artifact 10).
- **Savings:** input tokens and cost per incident.
- **Decision:** keep compression only if quality is non-inferior (within 2 points, false-established not worse) and required-evidence inclusion stays at 100%.

### Q12.77 The team wants to understand whether the critic actually improves diagnosis quality. How could an ablation study isolate its contribution?

**Frame.** How do we isolate the critic's value?

**Answer.** **Ablation:** run the suite with `critic_enabled = false`, everything else the same, paired (tested as a valid single-variable change). Compare:

- **false-established rate:** the critic's main job is to stop confident mistakes
- **unsupported-claim rate and citation validity**
- **accuracy,** and the over-escalation rate (is it too strict?)
- **cost and latency** it adds

Then measure the **critic's catch rate on seeded errors** (Artifact 9). The critic stays if it lowers false-established or unsupported claims significantly, at acceptable cost.

### Q12.78 A new experiment changes five parameters simultaneously. Why should experiments preferably change one variable at a time?

**Frame.** Why one variable at a time?

**Answer.**

- **Attribution:** each result maps to one cause, so good changes can be kept and bad ones dropped.
- **Statistical power:** a paired, single-change comparison needs fewer cases to detect a difference.
- **Safe rollback:** if something regresses, you know what to undo.

When interactions matter, run a **declared factorial design**, with each combination as its own run, rather than one five-way change. `check_single_variable` enforces this (tested).

### Q12.79 The platform needs to evaluate AI-generated actions against production incidents without actually applying changes. How could shadow execution support this?

**Frame.** How do we evaluate on real incidents without changing anything?

**Answer.** **Shadow mode:**

- a candidate configuration runs **alongside** the live one on real incidents: same evidence, its own diagnosis, critique and patch drafts
- it may test patches in the sandbox; its **approval and dispatch steps are replaced by recording** "would have proposed action X"
- afterwards, its outputs are compared with the incident review's confirmed cause, and with what people actually did
- shadow runs have their own budgets, and are never shown to on-call as advice

### Q12.80 A shadow experiment accidentally receives production write permissions. How should the architecture prevent this?

**Frame.** How can shadow runs never write?

**Answer.** Structural barriers, not configuration flags alone:

- **No dispatcher at all:** shadow workflows run on their own task queue, with **no worker for the dispatcher queue**, so a dispatch call has nowhere to go.
- **No credentials:** the shadow worker identity holds no release-process credential and no approval rights. The release process refuses its identity.
- **No approvals possible:** shadow actions are created with `mode = shadow`; the Approval API refuses decisions on them, and the dispatcher refuses them even if asked.
- **Separate fence epoch:** shadow components hold no valid fence token.
- **Tested:** the shadow suite includes an attempt to dispatch, which must fail at all three layers.

### Q12.81 The platform is ready to test a new model on a small percentage of real incidents. How should a canary rollout be designed?

**Frame.** How does a canary rollout work?

**Answer.**

1. **Prerequisites:** the candidate passed promotion gates offline and ran in shadow for at least 2 weeks.
2. **Assignment:** a deterministic share of new incidents, by incident-ID hash, starting at 5%; only low-severity incidents at first.
3. **The safety path is unchanged:** human approval, dispatch and verification are identical for canary incidents. Only the model configuration differs.
4. **Watch, compared with the live configuration over the same period:**
    - inconclusive rate, critic rejections, verified repairs, approval rejections
    - cost and latency
    - any unauthorised attempt or followed injection, which stops the canary at once
5. **Steps:** 5% → 25% → 50% → 100%, each held at least a week, and until enough incidents (provisional: 30) for a meaningful comparison.
6. **Decision recorded** at each step.

### Q12.82 A candidate configuration performs worse than the existing configuration after deployment. How should it be rolled back?

**Frame.** How is a worse configuration rolled back?

**Answer.**

- **Configurations are versioned data** (model alias, prompt versions, retrieval revision, flags), so rollback is **activating the previous configuration version**, with no code deploy.
- **New incidents** use the previous configuration immediately.
- **Running investigations** keep the configuration they started with, recorded in their identity, so each investigation is consistent. An incident lead may cancel and re-run one.
- **Automatic rollback triggers:**
    - any unauthorised attempt or followed injection
    - verified-repair rate significantly below the live configuration (a paired test over the canary period)
    - a cost or latency breach
- **The rollback and its reason** are recorded as a decision.

### Q12.83 A model produces more convincing explanations but has a lower verified repair rate. Why should promotion not depend only on the quality of its generated responses?

**Frame.** Why not promote the more convincing model?

**Answer.**

- **Persuasive isn't correct:** fluent, confident explanations can make approvers trust wrong diagnoses more, which is exactly the risk in Artifact 5.
- **The goal is outcomes:** verified recoveries, safely, at acceptable cost. A lower verified-repair rate means more failed fixes and rollbacks in production.
- **Readers rating explanations is subjective and gameable;** verification against predicates is not.

So promotion gates are **outcome-based**:

- hard safety gates first
- false-established rate, verified-repair rate (not lower than baseline), citation validity, cost and latency

Explanation quality is at most a secondary measure (tested: a fall in verified repairs fails the gate).
