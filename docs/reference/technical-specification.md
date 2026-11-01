# AI SRE Platform — Technical specification

The engineering detail behind the System Design tab: invariants, lifecycle, security, data model, pipeline and model contracts, validation, limits, interfaces, reliability, privacy, testing and milestones. Stack: Python, FastAPI and Pydantic, PostgreSQL with SQLAlchemy and Alembic, Temporal, Claude through an internal gateway, gVisor containers, OpenTelemetry, Prometheus and Grafana, Docker Compose and uv.

## 1. Scope and invariants

**In scope:** change-caused failures in Python web applications; one organisation and one environment; alerts from existing monitoring; read-only evidence; diagnosis with citations; fixes from the allowed list, tested in an isolated environment; signed human approval; one-time execution through the release process; independent verification; CLI and HTTP API.

**Out of scope:** other failure types; new code written by the model; data, permission, secret or infrastructure changes; a product dashboard; fine-tuning or self-hosted models; several organisations.

**Invariants, which every milestone must hold**

1. No production change without a signed approval from an authorised person, naming the exact patch fingerprint.
2. Only the trusted dispatcher writes to production, and only through the release process.
3. The model holds no credentials, and its output is never executed or trusted without validation.
4. Every claim in a diagnosis cites evidence that exists; below the confidence threshold, the result is "Root cause not established".
5. A fix is tested only after the failure reproduces in the isolated environment.
6. Recovery is verified from monitoring against criteria recorded before the change, by a component other than the one that applied it.
7. Each approved change executes at most once.
8. Every limit is enforced before the call it limits.
9. Every consequential operation is an append-only, hash-chained event.
10. An accepted incident is never lost, and every incident gets its workflow.

## 2. Incident lifecycle

One Temporal workflow per incident, with ID incident-\<incident_id>, owns every status change. Each change states the version it expects and writes an event in the same transaction.

**Request path (synchronous, p95 ≤ 300 ms):** authenticate → validate → resolve service and environment → check idempotency key → deduplicate → in one transaction write the incident, its first event and an outbox row → return the incident ID. The outbox relay then starts the workflow (p95 ≤ 1 s).

**Allowed transitions**

| From | To | Trigger |
| --- | --- | --- |
| open | investigating | Workflow starts |
| investigating | diagnosed | Reviewed diagnosis, established |
| investigating | inconclusive | Below threshold, limit reached, or invalid output 3 times |
| diagnosed | awaiting_approval | Patch tested and passed after reproduction |
| diagnosed | handed_over | Not reproduced, or test failed |
| awaiting_approval | executing | Signed approval received and re-checked |
| awaiting_approval | handed_over | Rejected, or expired after 60 min |
| executing | verifying | Dispatcher reports "applied" |
| executing | handed_over | "Not applied", "failed", or "unknown" after lookup |
| verifying | recovered | Criteria hold for the hold period |
| verifying | not_recovered | Criteria not met; undo dispatched |
| not_recovered | handed_over | Undo completed; person decides next step |
| inconclusive | handed_over | Findings delivered |
| recovered | closed | Incident lead accepts |
| handed_over | closed | Incident lead closes |
| any active state | cancelled | Cancellation, at the next safe point; never mid-change |

Any other transition is rejected in code, even if the version matches. Waiting for approval doesn't count against the 15-minute investigation limit.

## 3. Security and permissions

Identity comes from the organisation's identity system; permissions are roles per service, checked by the API on every request and again by the dispatcher before any change.

**Authentication:** people sign in with single sign-on and two-factor authentication, and call the API with a short-lived OIDC bearer token (provisional: 1 hour). Monitoring and platform components use their own client credentials, rotated. The platform stores no passwords.

| Permission | Viewer | Responder | Approver | Incident lead | Monitoring |
| --- | --- | --- | --- | --- | --- |
| Read incidents, timeline, artifacts | ✓ | ✓ | ✓ | ✓ | — |
| Create an incident | — | ✓ | ✓ | ✓ | ✓ |
| Approve or reject an action | — | — | ✓ | ✓ | — |
| Cancel an investigation | — | ✓ | ✓ | ✓ | — |
| Confirm severity, close | — | — | — | ✓ | — |

**Trust boundaries**

| Boundary | Rule |
| --- | --- |
| Evidence → platform | Redacted, labelled as data, size-limited, instruction-like text flagged |
| Model output → platform | Strict schema; citations must exist; fixes from the allowed list; secrets scanned |
| Isolated environment → platform | Only scanned test results leave |
| People → API | Authenticated and authorised on every request |
| Approval → dispatcher | Signature, fingerprint, expiry and service state checked |
| Dispatcher → production | Only the approved change, only through the release process |

**Approval signing:** the Approval Service signs each approval with an asymmetric key held in the secrets manager. The dispatcher holds only the public key, so a compromised dispatcher can't forge approvals.

**Never available to the model:** credentials, a shell, free-form database queries, fetching links, or any path to the dispatcher.

## 4. Data model

Twenty-two PostgreSQL tables, each with an application-generated UUIDv7 key and an org_id. The full schema is tested on PostgreSQL 16; audit tables reject updates and deletes through triggers.

| Table | Holds | Key rules |
| --- | --- | --- |
| services, environments | The catalogue | Unique by (org_id, slug) |
| incidents | Status, severity, version, last_event_seq | One open incident per (org_id, dedup_key) |
| incident_alerts | Every alert, duplicates included | Raw payload as JSONB |
| evidence | One record per item, any kind | Append-only; kind-specific attributes in JSONB; fingerprint; snapshot reference |
| hypotheses, hypothesis_evidence | Causes and their supporting or contradicting evidence | Ordinal unique per incident |
| diagnoses | A new row per revision | Leading hypothesis required if established; same-incident foreign key |
| actions | Proposed changes, with recovery criteria | Kind from the allowed list; version |
| patches | Diff reference and diff_sha256 | One per action; append-only |
| test_runs | Reproduction and test result | Append-only |
| approval_requests | Escalation and expiry | One open request per action |
| approval_decisions | One row per person's decision | Append-only; signature required for approvals; reason required for rejections |
| execution_attempts | Each attempt, with execution_key | Final outcome set once; only one "applied" per key |
| verifications | Criteria, measurements, result | Append-only |
| incident_budgets, budget_reservations | Limits, reserved and spent | Database checks reject any overrun |
| model_attempts | Every model call | Append-only; input fingerprint for replay |
| artifacts | References to large items | Write-once; storage key never returned by the API |
| timeline_events | The public history | Append-only; gapless sequence per incident; hash chain |
| outbox | Messages to the workflow engine | Stable message_id and workflow ID |
| idempotency_keys | API write keys | Per caller and operation; 24-hour expiry |

**Columns or JSONB:** anything joined, filtered, constrained or audited is a column. Kind-specific evidence fields, recovery criteria and measurements, event payloads and raw alerts are JSONB, always validated by a Pydantic contract before writing.

**Concurrency:** every update states the expected version and fails if it changed. Sequence numbers and budget reservations take a short row lock.

**Migrations:** Alembic only, expand then contract, tested on a copy of production data, after a point-in-time backup.

## 5. Investigation pipeline and model contracts

Each stage is a Temporal activity with its own timeout, retry limit and saved result. The model is called only in 5.2 and 5.3, through the Model Gateway.

### 5.1 Collect evidence

- One read-only adapter per tool, queried in parallel, scoped to the incident's service, environment and window (provisional: 60 minutes before the start, up to now).
- Change history first. Results are normalised into evidence records, redacted, fingerprinted and snapshotted.
- A failed or timed-out source becomes a gap record. Budget: ≤ 3 minutes p95.

### 5.2 Diagnose

- Code computes the timeline, version split and changed values; up to 16,000 input tokens of labelled extracts go to the model.
- The model must return exactly this structure (ModelDiagnosisOutput); unknown fields are rejected:

```json
{
  "hypotheses": [
    {
      "statement": "The changed config value breaks the order handler",
      "supporting_evidence_ids": ["<uuid>"],
      "contradicting_evidence_ids": [],
      "confirm_test": "Old value passes, new value fails in the test environment",
      "refute_test": "Instances with the old value also fail",
      "proposed_action": "config_revert"
    }
  ],
  "leading_index": 0,
  "summary": "...",
  "missing_evidence": []
}
```

- Two to five hypotheses; leading_index is null when inconclusive; proposed_action only from the allowed list.

### 5.3 Review

- Code checks every cited ID exists and belongs to this incident, the action is allowed and targets the incident's service, and at least two hypotheses were weighed.
- Confidence is scored from evidence strength. Below the threshold, the diagnosis becomes inconclusive and no patch is drafted.

### 5.4 Patch and test

- The smallest reversal is drafted as a diff against the failing revision, stored write-once, and fingerprinted (diff_sha256).
- A fresh gVisor container reproduces the failure at the failing version. No reproduction, no test.
- Then: apply the patch, check the original symptom is gone, run the existing tests, and check for new errors. Results are scanned and stored; the container is destroyed.

### 5.5 Approve, execute, verify

- **Approve:** the request carries the diagnosis, evidence, diff, test result, risk and undo plan. The approval is signed and valid for 15 minutes.
- **Execute:** the dispatcher verifies signature, fingerprint, expiry and current service state, records its intent, then applies the change with the approval's execution_key.
- **Verify:** the Recovery Verifier reads monitoring directly against the recorded criteria for the hold period (provisional: 10 minutes). On failure, it dispatches the pre-approved undo and hands over.

## 6. Validation

Every object crossing a boundary is a Pydantic model that forbids unknown fields. Invalid input is rejected whole, never repaired by guessing.

| Where | Checked | On failure |
| --- | --- | --- |
| Intake | Service, environment, start time, title and source present; service and environment in the catalogue; caller allowed | 422 naming the fields, or 403; no investigation starts |
| Evidence | Source, collection time, link, fingerprint present; redaction applied; size limit | Item dropped and a gap recorded |
| Model output | Schema; 2–5 hypotheses; cited IDs exist in this incident; leading_index in range; action on the allowed list; no secrets, links or commands | Retry with the error, up to 3 attempts; then inconclusive |
| Patch | Within the target service; change type allowed; no new secret reads, prints or hard-coded values | Rejected; no test |
| Approval | Approver's role; sign-in within 15 minutes; not self-approval; fingerprint and action version match; request open | 401, 403 or 409; nothing recorded |
| Execution | Signature, fingerprint, expiry, unchanged service state | Stop, void approval, alert |
| Events | Envelope and payload contract; next sequence number | Transaction rolls back |
| Migrations | Models and migrations agree | Build fails |

## 7. Limits, idempotency and metering

Limits are in the System Design tab, section 9; this section is how they and duplicate protection are enforced.

**Budget reservation, before every model call:**

1. Lock the incident's incident_budgets row.
2. Compute the call's maximum cost from its input size and output limit.
3. If calls, tokens and cost all fit, insert a budget_reservations row and add it to reserved; otherwise refuse the call and record refused_by_budget.
4. After the call, settle with the actual cost and tokens; an unsettled reservation is released when it expires.

Money is stored in whole millionths of the currency unit. Database checks reject any total above the limit.

**Idempotency**

| Layer | Mechanism | A duplicate gets |
| --- | --- | --- |
| API writes | Idempotency-Key per caller and operation; canonical request fingerprint; 24 h | The stored response; a different body gets 409 idempotency_key_reused |
| Incident creation | One open incident per (org_id, dedup_key) | The alert attached to the existing incident, deduplicated: true |
| Workflow start | Workflow ID incident-\<incident_id> | "Already started", treated as success |
| Workflow signals | Each carries its decision or request ID | Ignored if seen |
| Production change | One execution_key per approval; one "applied" attempt per key | Refused; after a crash, a lookup by key |
| Event consumers | Per-incident cursor stored with the effect | Skipped |

**Metering:** every model attempt records provider, model, instruction version, tokens, cost, latency and status. Cost per incident is reported daily and checked against the monthly budget.

## 8. APIs, events and integrations

All endpoints sit under /v1, take an OIDC bearer token, return RFC 9457 problem details on error, and require Idempotency-Key on writes. The OpenAPI document is generated from the contracts and checked in the build.

| Endpoint | Purpose | Success |
| --- | --- | --- |
| POST /v1/incidents | Submit an incident | 201, or 200 when deduplicated |
| GET /v1/incidents/{id} | Current state, with ETag | 200 |
| GET /v1/incidents/{id}/timeline?after_seq= | History, paged | 200 |
| GET /v1/incidents/{id}/timeline/stream | Live history over SSE, resumes from Last-Event-ID | 200 |
| GET /v1/incidents/{id}/actions | Proposed actions with patch and test | 200 |
| POST /v1/actions/{id}/approvals | Approve one exact patch | 201 |
| POST /v1/actions/{id}/rejections | Reject, with a reason | 201 |
| POST /v1/incidents/{id}/cancellation | Stop the investigation | 202 |
| POST /v1/incidents/{id}/severity-confirmation, /closure | Incident lead decisions | 200 |
| GET /v1/artifacts/{id}, /content | Metadata; content as a plain-text attachment | 200 |
| GET /livez, /readyz, /metrics | Liveness, readiness, Prometheus (internal port) | 200 |

**Events:** CloudEvents 1.0 in structured JSON, types such as aisre.approval.granted.v1, with extensions incidentid, sequence, orgid, actor, correlationid, causationid and traceparent. Order comes from the gapless per-incident sequence, never from timestamps. Temporal's own history is never exposed as the timeline.

**Outbox:** the API never calls Temporal directly. It writes an outbox row in the same transaction as the change; a relay in the worker process claims rows with FOR UPDATE SKIP LOCKED, starts or signals the workflow, and marks them delivered. Delivery is at least once; stable IDs make repeats harmless. Alert if the oldest pending row is older than 2 minutes.

| Integration | Direction | Access |
| --- | --- | --- |
| Monitoring | In: alerts; out: read-only queries | Client credentials; read-only |
| Logs, traces, error tracker, change history | Out: read-only queries | One adapter each; read-only, scoped |
| Runbooks | Out: read and index | Read-only |
| Model provider | Out: redacted extracts | Gateway only |
| Review system | Out: proposed change (pull request) | No direct commits |
| Release process | Out: apply, undo, look up by key | Dispatcher only |
| Messaging | Out: notifications | Notifier only |
| Identity system | In: tokens and groups | OIDC |

## 9. Reliability and failure modes

The platform must never make an incident worse by failing itself: alerts keep reaching engineers directly, and no action runs that can't be recorded.

| Target | Value (provisional) |
| --- | --- |
| Intake availability | ≥ 99.9% per month |
| Intake latency | p95 ≤ 300 ms, p99 ≤ 1 s |
| Event available to workers | p95 ≤ 1 s, p99 ≤ 5 s |
| Time to first notification | p95 ≤ 60 s from alert time |
| Resume after a worker crash | ≤ 2 min, completed steps not repeated |
| Cancellation | All work stopped ≤ 30 s p95 |
| Recovery point / recovery time | ≤ 5 min data loss / ≤ 1 h restore |
| Maximum tolerable full outage (pilot) | 4 h |

| Failure | Behaviour |
| --- | --- |
| Worker crash | Heartbeat lost after 60 s; another worker resumes from the last checkpoint; unique keys stop repeated side effects |
| Crash after a change, before confirmation | Intent recorded first; lookup by execution_key, then a service state check; if still unclear, stop and ask a person |
| PostgreSQL down | Workers pause and retry with backoff; no approvals or executions; intake returns 503 so monitoring retries; on-call told within 2 min |
| Temporal down | Intake still works; outbox rows wait; relay delivers on recovery |
| Evidence source slow or erroring | Per-call timeout, 2 retries with backoff, circuit breaker; evidence marked partial |
| Model provider slow or down | 60 s timeout, 3 attempts; then evidence and timeline only |
| Database corruption | Point-in-time restore; open incidents re-checked against monitoring and release history before any action resumes |

Restore drills run quarterly against the recovery targets.

## 10. Privacy and data handling

Application logs may contain customer data and credentials, so evidence is redacted when collected, before it is stored or sent to the model.

- **Redaction:** secret and personal-data patterns are masked at collection. Model output, patches and test results are scanned again before they are stored or shown.
- **Least data:** only the incident's service, environment and time window are queried; prompts carry short extracts, not whole logs.
- **No secrets to the model,** and none in the isolated environment, which uses fake test values.
- **Encryption:** at rest in PostgreSQL and the object store, and in transit everywhere.
- **Retention (provisional):** raw evidence and model input and output 30 days; timeline and audit 1 year. A separate retention role deletes expired items; the application's role can't.
- **Access:** an incident's evidence is visible only to roles on its service; every read of content is logged.
- **Model provider:** only redacted extracts are sent; terms must confirm requests aren't kept or used for training.
- **A leaked secret** is treated as a security incident: masked, traced to its source, the redaction rule fixed, and the secret rotated. The event is recorded without the secret itself.

## 11. Testing

The platform is tested against incidents whose cause is known, before it sees a real one. Synthetic cases come first, then sanitised replays of past incidents.

| Suite | What it proves | Pass mark |
| --- | --- | --- |
| Scenario | The reference case runs end to end: alert, evidence, diagnosis, revert of the one changed value, approval, execution, verification | Every step in the timeline, in order |
| Diagnosis accuracy | Leading cause matches the known cause on change-caused synthetic incidents | ≥ 80% |
| Honesty | Evidence withheld → "Root cause not established", no patch | 100% |
| Conflicting evidence | Both hypotheses shown, with a deciding test | 100% |
| Injection | Logs containing instructions produce no action outside the allowed list | 0 unsafe actions |
| Security (40 cases) | Approval forgery, self-approval, patch change after approval, sandbox escape, credential reach, link fetching, audit tampering | All pass |
| Validation | Missing and unknown fields rejected; invalid model output rejected whole | All pass |
| Fault injection | Slow and failing sources, provider outage, worker kill at every step, database and Temporal outage | Behaviour as in section 9 |
| Idempotency | Repeated submissions, approvals and executions; key reuse with a different body | No duplicate effects |
| Load and burst | 20 and 60 simultaneous incidents; intake at 10× expected peak | Latency targets met; nothing dropped |
| Limits | Runaway loop; oversized evidence | Stops at the cap; hands over |
| Migrations | Upgrade from the previous version on a data copy | No data lost |
| Restore drill | Point-in-time restore, quarterly | ≤ 5 min loss, ≤ 1 h restore |

Every suite except load and restore runs on each change, using a fake model adapter and fake release process; accuracy runs nightly against the real model.

## 12. Milestones and acceptance criteria

Each milestone ships only when every criterion in its row passes; M1 and M2 run on synthetic incidents with no production access at all.

| Milestone | Delivers | Acceptance criteria |
| --- | --- | --- |
| M1 — Investigate (advisory) | Intake, deduplication, evidence collection, diagnosis, review, timeline and stream, limits, CLI | Scenario passes; accuracy ≥ 80%; honesty 100%; injection 0 unsafe; first summary ≤ 5 min p95; investigation stops at 15 min; every claim cited; budget caps hold |
| M2 — Patch and test | Patch drafting, isolated test environment, reproduction-first testing | No test without reproduction; no production access from the environment; no environment left running after 60 min; patches only from the allowed list |
| M3 — Approve, apply, verify | Approval Service, trusted dispatcher, Recovery Verifier, undo | 0 executions without approval; self-approval refused; fingerprint mismatch stops execution; each change at most once; failed fix undone and escalated; recovery criteria recorded before execution |
| M4 — Harden for pilot | Crash recovery, outage handling, backups, telemetry, burst handling | Intake ≥ 99.9%; resume ≤ 2 min; no duplicates after crashes; restore drill within targets; burst of 60 queued by severity with none dropped; all 40 security tests pass |

After M4, the pilot runs in advisory mode first. Applying approved fixes is switched on only after a recorded decision reviewing pilot accuracy and approvals.

## 13. Risks

| Risk | Likelihood | Impact | Mitigation | Owner (role) |
| --- | --- | --- | --- | --- |
| Accuracy below 80% on real incidents | Medium | Advisory output ignored | Measure on replays before pilot; tighten the inconclusive threshold; revisit fine-tuning only with evidence | SRE lead |
| Approvers trust a convincing but wrong summary | Medium | Wrong fix applied, then undone | Contradicting evidence and test result shown with every request; approval review in the pilot | SRE lead |
| Injected text steers the written summary | Medium | Misleading advice | Flagged text shown to approvers; injection suite on every change | Security lead |
| Redaction misses an unusual secret | Low | Secret sent to provider or stored | Output scanning as a second layer; rotate on any leak; extend rules | Security lead |
| Release process can't apply or look up one change by key | Medium | M3 blocked | Confirm before M3; an adapter if needed | Platform owner |
| Running Temporal is too heavy for the team | Low | Slower delivery | Fallback to a PostgreSQL job queue behind the same interface | Platform owner |
| Alerts lack stable service labels | Medium | Duplicates; failed intake | Check alert samples before M1; mapping in Intake | Platform owner |
| Approvers unreachable at night | Medium | Expired requests | Escalation at 15 min; rota coverage confirmed | Engineering manager |
| Provider outages | Low | Evidence-only mode | Accepted; second provider only if frequent | SRE lead |
| Isolation escape | Low | Test host compromised | Separate host, no secrets inside, kept up to date; virtual machines if escape tests fail | Security lead |
