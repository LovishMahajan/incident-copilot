# Artifact 4: Requirements Specification

- **Layer:** 4, Functional and Non-Functional Requirements
- **Built from:** Artifacts 1, 2 and 3
- **Status:** Proposed: gaps resolved and reference code aligned 2026-11-02 per [decisions.md](../../decisions.md); pending team sign-off
- **Sources:** [decisions.md](../../decisions.md), [conversation.md](conversation.md), [System Design](../../reference/system-design.md), [Technical Specification](../../reference/technical-specification.md), [Layer 1](../layer-01-problem-discovery/conversation.md), [Layer 2](../layer-02-scope-and-constraints/conversation.md) and [Layer 12](../layer-12-reliability-evaluation/conversation.md) answers where cited

## Required sections

| Required section | Questions this section answers | Done |
| --- | --- | --- |
| 4.1 Requirement ID | Q4.1–Q4.34 | Yes |
| 4.2 Description | Q4.1–Q4.34 | Yes |
| 4.3 Priority | Q4.1–Q4.34 | Yes |
| 4.4 Measurement | Q4.1–Q4.34 | Yes |
| 4.5 Acceptance criterion | Q4.1–Q4.34 | Partial |
| 4.6 Validation method | Q4.1–Q4.34 | Yes |
| 4.7 Related milestone | Q4.1–Q4.34 | Yes |

Every requirement must carry each of these seven fields, so each section is one column of the register below and answers every question.

## Requirements register

97 IDs, in ID order: 96 active requirements and one retired row (NFR-08). Rows are as written in the answers, corrected by [decisions.md](../../decisions.md). FR-39 onward and NFR-40 onward were added from it and from decisions in the answers. The argument for each original row is in [conversation.md](conversation.md).

| ID | Description | Priority | Measurement | Acceptance criterion | Validation method | Milestone |
| --- | --- | --- | --- | --- | --- | --- |
| FR-01 | Accepting an incident returns an incident ID and starts the workflow asynchronously | Must | Response contains an ID; workflow-start event recorded | 100% of valid submissions return an ID and produce exactly one workflow | Integration test with the scenario fixture | M1 |
| FR-02 | The workflow runs the steps in order and records each one | Must | Timeline entries per step | Every completed incident shows all steps in order, with timestamps | Test against the scenario fixture | M1 |
| FR-03 | Duplicate alerts attach to the existing open incident | Must | Investigations started per unique deduplication key | Sending the same alert 10 times creates 1 investigation and 10 timeline entries | Replay test | M1 |
| FR-04 | Retried submissions with the same request key are idempotent | Must | Incidents created per request key | 1 incident per request key | API test | M1 |
| FR-05 | Submissions without mandatory fields are rejected, naming the fields | Must | Rejection rate and error content | 100% of submissions missing any mandatory field are rejected, stored raw in a dead-letter record with the reason, and the incident lead is notified; 0 investigations start (ADR-58) | API validation tests | M1 |
| FR-06 | The service identifier must match the service catalogue | Must | Unknown-service submissions | Unknown services are rejected and dead-lettered, and the incident lead is notified. A monitoring caller gets 422; a person gets the same 404 as for a forbidden service (ADR-58) | Test with an unknown service | M1 |
| FR-07 | Every evidence item records its source, collection time and a link | Must | Share of items with all three fields | 100% | Schema test | M1 |
| FR-08 | A failed or timed-out source is recorded as a gap, never as empty or healthy | Must | Gap records when a source fails | A source outage in testing produces a gap record, and the diagnosis reports it | Fault-injection test | M1 |
| FR-09 | Evidence collection completes within its time budget | Should | p95 evidence-collection time | ≤ 3 minutes p95 (provisional) | Timing on the test suite | M1 |
| FR-10 | Each hypothesis records its statement, evidence for and against, confidence and status | Must | Field completeness | 100% of hypotheses have all fields | Schema test | M1 |
| FR-11 | At least two hypotheses are considered for every diagnosis | Must | Hypotheses per investigation | ≥ 2 for 100% of diagnosed incidents | Test suite review | M1 |
| FR-12 | Every claim in a diagnosis cites existing evidence | Must | Uncited or invalid citations | 0 in the test suite | Automated citation check | M1 |
| FR-13 | Diagnosis accuracy on change-caused synthetic incidents | Must | Leading hypothesis matches the known cause | ≥ 80% on the synthetic suite (provisional, ADR-65) | Evaluation suite | M1 |
| FR-14 | Conflicting hypotheses are shown side by side with a proposed deciding test | Must | Behaviour on conflict test cases | 100% of conflict cases show both and a deciding test | Test cases with conflicting evidence | M1 |
| FR-15 | A single strong contradiction lowers or rules out a hypothesis | Should | Behaviour on contradiction cases | Hypothesis status changes in 100% of contradiction cases | Test cases based on Artifact 3 | M1 |
| FR-16 | Below the confidence threshold, the diagnosis is marked "Root cause not established" | Must | Behaviour on evidence-withheld cases | Threshold is medium (provisional, ADR-53). 100% of withheld-evidence cases are inconclusive; 0 present a guess as the cause | Honesty test (evidence removed) | M1 |
| FR-17 | No patch is proposed from an inconclusive diagnosis | Must | Patches from inconclusive diagnoses | 0 | Test suite | M2 |
| FR-18 | Patches are proposed through the review system, never committed directly | Must | Direct writes to protected branches | 0 | Permission audit and test | M2 |
| FR-19 | Each patch includes the hypothesis, evidence, diff, expected effect, risk and undo plan | Must | Field completeness | 100% | Schema test | M2 |
| FR-20 | For the scenario, the proposed patch reverts only the changed configuration value | Must | Patch content | The diff changes exactly that value | Scenario test | M2 |
| FR-21 | A patch is tested only after the failure is reproduced in the disposable environment | Must | Tests without reproduction | 0 | Test suite | M2 |
| FR-22 | The disposable environment has no access to production systems, data or credentials | Must | Blocked access attempts | 100% of production access attempts blocked | Security test | M2 |
| FR-23 | Test results are stored as evidence, and the environment is destroyed afterwards | Must | Environments left running | 0 after 60 minutes | Resource audit | M2 |
| FR-24 | No production change runs without a recorded approval from an authorised person | Must | Executions without approval | 0 | Negative test; audit | M3 |
| FR-25 | The approval request contains the diagnosis, evidence, diff, test results, risk and undo plan | Must | Field completeness | 100% | Schema test | M3 |
| FR-26 | The platform's own identity cannot approve | Must | Self-approval attempts | 100% rejected | Security test | M3 |
| FR-27 | Execution re-checks approval validity and service state before applying | Must | Executions with stale approval or changed state | 0 | Test with state changed after approval | M3 |
| FR-28 | The executed change is identical to the approved change | Must | Differences between approved and applied diff | 0 | Hash comparison test | M3 |
| FR-29 | Each approved change executes at most once | Must | Duplicate executions under retry | 0 | Retry and replay test | M3 |
| FR-30 | Recovery criteria are recorded before execution | Must | Executions without recorded criteria | 0 | Test suite | M3 |
| FR-31 | Recovery is verified from monitoring data, independently of the executor | Must | Verification source | 100% verified from monitoring | Code review and test | M3 |
| FR-32 | A failed verification undoes the change and escalates (ADR-48) | Must | Behaviour when the fix doesn't help | 100% escalated in the failed-fix test, and 100% undone when a conditional approval stands. Without one, a fresh approval is requested, and a refusal or expiry leaves the change in place and hands over | Test with an ineffective patch, with and without a conditional approval | M3 |
| FR-33 | Cancellation stops all work within a time limit | Must | Time from cancel to all work stopped | ≤ 30 seconds p95 (provisional) | Cancellation test | M1 |
| FR-34 | Cancellation withdraws open approvals and destroys disposable environments | Must | Open approvals and environments after cancel | 0 | Test | M3 |
| FR-35 | Cancellation never leaves a production change half-applied | Must | Partial changes after cancel mid-execution | 0 | Test cancel during execution | M3 |
| FR-36 | Every platform and human action creates a timeline entry | Must | Actions without an entry | 0 in a full scenario run | Scenario audit | M1 |
| FR-37 | Timeline entries cannot be edited or deleted | Must | Successful edit or delete attempts | 0 | Security test | M1 |
| FR-38 | The timeline is available through the API and the command-line tool | Must | Availability of both | Both return the full timeline | Integration test | M1 |
| FR-39 | Instructions embedded in evidence never cause an unsafe action (ADR-55) | Must | Unsafe actions and followed injections on injection cases | 0 | Injection test suite | M1 |
| FR-40 | Patches come only from the allowed list of change types (ADR-55) | Must | Patches outside the allowed list | 0 | Test suite | M2 |
| FR-41 | Mandatory fields are service identifier, environment, start time or alert time, symptom and source | Must | Submissions accepted with a listed field missing | 0; each rejection names the missing field | API validation tests, one field removed at a time | M1 |
| FR-42 | The deduplication key is the alert's fingerprint, or service plus alert name if there is none (ADR-58) | Must | Stored incidents with no key | 0; repeats of an alert with no fingerprint start 1 investigation | Replay test | M1 |
| FR-43 | Alerts for the same service within the grouping window join the open incident | Must | New investigations for alerts inside the window | 0 within 10 minutes of the incident opening (provisional) | Replay test | M1 |
| FR-44 | Evidence is collected over a fixed window, from a set period before the start up to now | Must | Evidence queries outside the window | 0 outside 60 minutes before the start up to now (provisional) | Adapter test | M1 |
| FR-45 | Each hypothesis is open, supported, ruled out or confirmed; ruled-out ones stay visible with their reason | Must | Invalid statuses; ruled-out hypotheses hidden or without a reason | 0 | Schema test; test suite | M1 |
| FR-46 | Each diagnosis states high, medium or low confidence, set by the ADR-53 rule, with the reasons | Must | Diagnoses with no level, no reasons, or a level the rule doesn't give | 0 | Schema test; test suite | M1 |
| FR-47 | Patches use the smallest reversal: configuration value, then commit, then whole release. Code fixes are out of scope for the first version | Must | Patches larger than the smallest available reversal; code-fix patches | 0 | Test suite | M2 |
| FR-48 | A patch changes only the affected service | Must | Patches touching another service | 0 | Test suite | M2 |
| FR-49 | High-impact changes need two different approvers (ADR-49) | Must | High-impact executions with fewer than two different approvers | 0; unconfirmed severity counts as high-impact | Negative test | M3 |
| FR-50 | Recovery must hold for the hold period: 10 minutes by default, longer per service in the catalogue (ADR-66) | Must | Recoveries declared before the hold period ends | 0 (provisional) | Timer test | M3 |
| NFR-01 | Intake availability | Must | Successful intake requests ÷ total, per month | ≥ 99.9% (provisional) | Production monitoring; load test | M4 |
| NFR-02 | Acknowledged incidents are never lost | Must | Lost incidents after crash tests | 0 | Crash test after acknowledgement | M4 |
| NFR-03 | A crashed investigation resumes from its last checkpoint | Must | Time to resume; steps repeated | Resumes within 2 minutes; completed steps not repeated | Kill-worker test | M4 |
| NFR-04 | No duplicate notifications, approvals or executions after a crash | Must | Duplicates | 0 | Kill-worker test at each step | M4 |
| NFR-05 | No action executes while the incident record can't be written | Must | Actions during a database outage | 0 | Database outage test | M4 |
| NFR-06 | Investigations resume from checkpoints after the database returns | Must | Investigations resumed | 100% resumed within 2 minutes of recovery | Outage test | M4 |
| NFR-07 | Engineers are notified that the platform is paused | Must | Time to notification | ≤ 2 minutes after the outage starts | Outage test | M4 |
| NFR-08 | **Status: Retired**, superseded by NFR-30 (ADR-55). ID kept, never reused. Was: platform acknowledgement latency | — | — | — | — | — |
| NFR-09 | Time to first notification | Must | Alert-time-to-notification, p95 | ≤ 60 s (provisional) | Scenario timing | M1 |
| NFR-10 | Time to first summary | Must | Alert-time-to-summary, p95 | ≤ 5 min (from Artifact 2) | Scenario timing | M1 |
| NFR-11 | The investigation stops at 15 minutes and hands over | Must | Maximum investigation duration | No investigation exceeds 15 min + 30 s | Slow-source test | M1 |
| NFR-12 | Resources are released when a limit is reached | Must | Resources held after stop | 0 workers, environments or model calls active | Test | M4 |
| NFR-13 | The per-incident model-call cap is enforced before each call | Must | Calls beyond the cap | 0 | Runaway-loop test | M1 |
| NFR-14 | Daily spend limit triggers alerts and evidence-only mode | Must | Behaviour at 80% and 100% of the daily limit | Limit $16.67 (provisional, ADR-56). Alert at 80%. At 100%, new investigations are evidence-only; running ones finish their current step, then stop and hand over (ADR-55) | Simulated spend test | M4 |
| NFR-15 | Actual cost is recorded per incident | Must | Incidents with cost recorded | 100% | Report check | M1 |
| NFR-16 | Excess incidents are queued by severity and never dropped | Must | Dropped incidents; queue order | 0 dropped; order matches severity | Burst test with 20 incidents | M4 |
| NFR-17 | Engineers are told when their incident is queued | Should | Notification on queue | 100% of queued incidents notify | Burst test | M4 |
| NFR-18 | Secrets and personal data are redacted before storage and before model calls | Must | Seeded secrets found in stored evidence or prompts | 0 of a seeded corpus of 100 | Redaction test | M1 |
| NFR-19 | Evidence is encrypted at rest and in transit | Must | Encryption configuration | Enabled for all stores and connections | Configuration audit | M4 |
| NFR-20 | Raw evidence is deleted after the retention period | Should | Evidence older than the period | 0 | Retention job test | M4 |
| NFR-21 | Recovery point objective | Must | Data lost in a restore drill | ≤ 5 minutes (provisional) | Quarterly restore drill | M4 |
| NFR-22 | Recovery time objective | Must | Time to read-only investigation mode; time to full operation | ≤ 1 hour to read-only investigation mode; ≤ 4 hours to full operation including writes (provisional, ADR-55) | Quarterly restore drill | M4 |
| NFR-23 | No action resumes after a restore until open incidents are re-checked | Must | Actions before re-check | 0 | Restore drill | M4 |
| NFR-24 | Full-platform recovery time | Must | Time to restore after a total failure | ≤ 1 hour target; ≤ 4 hours maximum (provisional) | Disaster recovery drill | M4 |
| NFR-25 | Platform failure doesn't stop alerts reaching engineers | Must | Alerts delivered while the platform is down | 100% | Outage test | M4 |
| NFR-26 | Every step, source and model call is timed and traced with the incident ID | Must | Untimed steps in a full run | 0 | Scenario trace review | M1 |
| NFR-27 | Percentile dashboards or queries exist for each measurement | Should | Measurements available | All listed measurements queryable | Review | M4 |
| NFR-28 | Degraded dependencies are bounded by timeouts, limited retries and a circuit breaker | Must | Calls to a failing dependency over time | Calls stop after the breaker trips; investigation still completes | Fault-injection test (slow and erroring) | M4 |
| NFR-29 | Evidence from a degraded source is marked partial, and confidence is lowered | Must | Behaviour on degraded sources | 100% marked; confidence lowered | Fault-injection test | M1 |
| NFR-30 | Intake latency | Must | p95 and p99 response time | p95 ≤ 300 ms, p99 ≤ 1 s (provisional) | Load test at 10× expected peak | M4 |
| NFR-31 | Event publication latency | Must | Acceptance-to-available time | p95 ≤ 1 s, p99 ≤ 5 s (provisional); 0 lost events | Load and crash test | M4 |
| NFR-32 | Model-call timeout | Must | Longest model call | No call exceeds 60 s (provisional) | Slow-provider simulation | M1 |
| NFR-33 | Model attempts per step | Must | Attempts before failure | ≤ 3; then inconclusive and handed over | Malformed-output simulation | M1 |
| NFR-34 | Approval request expiry and escalation | Must | Escalation and expiry times | Escalates at 15 min; expires at 60 min (provisional) | Timer test | M3 |
| NFR-35 | A granted approval expires, or is voided by a change | Must | Executions after expiry or a change | 0 | State-change test | M3 |
| NFR-36 | Cumulative token limits per incident | Must | Tokens used per incident | ≤ 200k input, ≤ 20k output (provisional); hand over at cap | Large-evidence test | M1 |
| NFR-37 | Per-call input limit | Must | Largest prompt | ≤ 16k tokens (provisional) | Test | M1 |
| NFR-38 | Maximum concurrent investigations | Must | Concurrent running investigations | Never > 5 (provisional) | Burst test | M4 |
| NFR-39 | Queue overflow alerts the incident lead and still records incidents | Must | Behaviour above 50 queued | Recorded and alerted | Burst test with 60 incidents | M4 |
| NFR-40 | A worker that stops reporting is treated as failed | Must | Time from last report to failure detected | ≤ 60 s (provisional) | Kill-worker test | M4 |
| NFR-41 | The whole workflow stops 24 hours from opening and hands over (ADR-46) | Must | Non-terminal, non-waiting incidents past the limit | 0 past 24 hours (provisional); `recovered` stays until the incident lead closes it | Timer test | M4 |
| NFR-42 | The per-incident spend cap is enforced before each call | Must | Spend beyond the cap | 0 beyond $2.00 (provisional, ADR-56); stop and hand over at the cap | Runaway-loop test | M1 |
| NFR-43 | Monthly spend alerts fire against the budget ceiling | Must | Alerts at 50%, 80% and 100% of the ceiling | All three fire at a ceiling of $500 (provisional, ADR-56) | Simulated spend test | M4 |
| NFR-44 | One service can't take every investigation slot | Should | Running slots held by one service | ≤ 4 of 5 (provisional); the next eligible queued incident by severity takes the slot | Burst test with one noisy service | M4 |
| NFR-45 | Data is kept for its ADR-57 period: raw evidence 30 days, cited evidence, model data, patches, test logs, timeline and audit 1 year | Should | Items past their period; items deleted early | 0 (provisional) | Retention job test | M4 |
| NFR-46 | Backups are continuous plus a daily full backup, stored separately and kept 35 days; restore drills run quarterly | Must | Missed daily backups; drills per quarter | 0 missed; 1 drill per quarter (provisional) | Backup audit; quarterly restore drill | M4 |
| NFR-47 | Calls to a degraded evidence source are retried with backoff | Must | Retries per call | ≤ 2 (provisional) | Fault-injection test | M4 |

## 4.1 Requirement ID

*Answers Q4.1–Q4.34.*

A unique, stable reference for one requirement. Later layers cite these IDs.

| Prefix | Meaning | Range | Count | Comes from |
| --- | --- | --- | --- | --- |
| FR | Functional: what the system does | FR-01 to FR-50 | 50 | Q4.1–Q4.15; [decisions.md](../../decisions.md) |
| NFR | Non-functional: how well it must do it | NFR-01 to NFR-47 | 47 | Q4.15–Q4.34; [decisions.md](../../decisions.md) |

Total 97 IDs, 96 active. IDs are continuous and unique, and every question produced at least one. A retired ID stays in the register and is never reused: NFR-08 is the only one (ADR-55).

## 4.2 Description

*Answers Q4.1–Q4.34.*

One statement of what must be true. Every requirement must be measurable; a requirement that cannot be tested is not yet a requirement.

| Area | IDs | Count | Questions |
| --- | --- | --- | --- |
| Intake and deduplication | FR-01 to FR-06 | 6 | Q4.1–Q4.3 |
| Evidence collection | FR-07 to FR-09 | 3 | Q4.4 |
| Hypotheses and diagnosis | FR-10 to FR-17 | 8 | Q4.5–Q4.8 |
| Patch and test | FR-18 to FR-23 | 6 | Q4.9–Q4.10 |
| Approval, execution and verification | FR-24 to FR-32 | 9 | Q4.11–Q4.13 |
| Cancellation and timeline | FR-33 to FR-38 | 6 | Q4.14–Q4.15 |
| Injection and allowed list | FR-39, FR-40 | 2 | ADR-55 |
| Mandatory fields, deduplication key and grouping | FR-41 to FR-43 | 3 | Q4.2–Q4.3 |
| Evidence window, hypothesis statuses and confidence levels | FR-44 to FR-46 | 3 | Q4.4, Q4.5, Q4.7 |
| Patch preference and scope | FR-47, FR-48 | 2 | Q4.9 |
| High-impact approval and hold period | FR-49, FR-50 | 2 | Q4.11, Q4.13 |
| Intake availability, crash and database outage | NFR-01 to NFR-07 | 7 | Q4.16–Q4.18 |
| Notification, summary and investigation duration (NFR-08 retired) | NFR-09 to NFR-12 | 4 | Q4.19–Q4.20 |
| Spend and concurrency handling | NFR-13 to NFR-17 | 5 | Q4.21–Q4.22 |
| Sensitive data, corruption and full-platform failure | NFR-18 to NFR-25 | 8 | Q4.23–Q4.25 |
| Performance measurements and degraded dependencies | NFR-26 to NFR-29 | 4 | Q4.26–Q4.27 |
| Specific limits: latency, model calls, approvals, tokens, concurrency | NFR-30 to NFR-39 | 10 | Q4.28–Q4.34 |
| Crash detection and whole-workflow limit | NFR-40, NFR-41 | 2 | Q4.17, Q4.20 |
| Spend caps and fairness | NFR-42 to NFR-44 | 3 | Q4.21–Q4.22 |
| Retention, backups and dependency retries | NFR-45 to NFR-47 | 3 | Q4.15, Q4.23, Q4.24, Q4.27 |

## 4.3 Priority

*Answers Q4.1–Q4.34.*

How firmly the requirement binds. The register uses two values (ADR-55):

- **Must:** its milestone can't pass without it.
- **Should:** expected at that milestone; it can slip only by a recorded decision.

| Value | Count | IDs |
| --- | --- | --- |
| Must | 89 | All others |
| Should | 7 | FR-09, FR-15, NFR-17, NFR-20, NFR-27, NFR-44, NFR-45 |

NFR-08 is retired and carries neither.

## 4.4 Measurement

*Answers Q4.1–Q4.34.*

The quantity observed to judge the requirement.

| Kind | Count | IDs |
| --- | --- | --- |
| Percentile timing (p95, some with p99) | 6 | FR-09, FR-33, NFR-09, NFR-10, NFR-30, NFR-31 |
| Field completeness | 4 | FR-07, FR-10, FR-19, FR-25 |
| Counts of events or observed behaviour | 86 | All others |

The platform's own measurements are set in [Q4.26](conversation.md): queue wait, duration of each step, per evidence source, per model call, human wait time and end-to-end times, all tagged with the incident ID.

## 4.5 Acceptance criterion

*Answers Q4.1–Q4.34.*

The value or behaviour that passes the requirement.

| Kind | Count | IDs |
| --- | --- | --- |
| Target of 0 | 41 | FR-12, FR-17, FR-18, FR-21, FR-23, FR-24, FR-27, FR-28, FR-29, FR-30, FR-34, FR-35, FR-36, FR-37, FR-39 to FR-50, NFR-02, NFR-04, NFR-05, NFR-12, NFR-13, NFR-16, NFR-18, NFR-20, NFR-23, NFR-26, NFR-35, NFR-41, NFR-42, NFR-45, NFR-46 |
| Target of 100% | 17 | FR-01, FR-05, FR-07, FR-10, FR-14, FR-16, FR-19, FR-22, FR-25, FR-26, FR-31, FR-32, NFR-06, NFR-15, NFR-17, NFR-25, NFR-29 |
| Other values or behaviour | 38 | All others |

28 criteria are labelled provisional in the register: FR-09, FR-13, FR-16, FR-33, FR-43, FR-44, FR-50, NFR-01, NFR-09, NFR-14, NFR-21, NFR-22, NFR-24, NFR-30, NFR-31, NFR-32, NFR-34, NFR-36, NFR-37, NFR-38, NFR-40, NFR-41, NFR-42, NFR-43, NFR-44, NFR-45, NFR-46, NFR-47. Everything in a provisional table in the sources stays provisional too, whether or not its row is labelled (ADR-55).

Two values are set by decision, both provisional:

- **FR-16:** the threshold is medium. High needs a tier 1–3 observation, medium needs tier 4 or two independent tier 5–7 observations, each with no unresolved contradiction. Anything lower is inconclusive with no patch (ADR-53). The SRE lead recalibrates it on the frozen suite before Release A.
- **NFR-14:** the daily limit is $16.67, the $500 monthly ceiling ÷ 30 (ADR-56). The money values are placeholders so that limits can be built and tested.

**Needs the team:** the actual monthly budget behind NFR-14 and NFR-43, Owner: Engineering manager, settled when: finance confirms it (ADR-56).

**Needs measurement:** real alert samples behind FR-42, and the peak concurrency behind NFR-38 and NFR-44, Owner: Platform owner and SRE lead, settled when: samples and past incident records are inspected (System Design section 10).

## 4.6 Validation method

*Answers Q4.1–Q4.34.*

How the criterion is shown to be met. The test suites are defined in [Technical Specification, section 11](../../reference/technical-specification.md).

| Kind | Count | IDs |
| --- | --- | --- |
| Executed test, including timing and simulations | 80 | All others |
| Test plus audit, review or production signal | 4 | FR-18, FR-24, FR-31, NFR-01 |
| Audit or review alone | 7 | FR-11, FR-23, FR-36, NFR-15, NFR-19, NFR-26, NFR-27 |
| Restore or disaster recovery drill | 5 | NFR-21, NFR-22, NFR-23, NFR-24, NFR-46 |

## 4.7 Related milestone

*Answers Q4.1–Q4.34.*

The milestone by which the requirement must pass.

**Milestones used below (provisional; finalised in Layer 13):**

| **Milestone** | **Scope** |
| --- | --- |
| **M1** | Intake, evidence collection and diagnosis on synthetic test cases (advisory only) |
| **M2** | Patch generation and testing in a disposable environment |
| **M3** | Human approval, controlled execution and verified recovery |
| **M4** | Reliability, limits and recovery hardening for the pilot |

| Milestone | FR | NFR | Total |
| --- | --- | --- | --- |
| M1 | 27 | 13 | 40 |
| M2 | 10 | 0 | 10 |
| M3 | 13 | 2 | 15 |
| M4 | 0 | 31 | 31 |

M1 and M2 run on synthetic incidents, and M3 and M4 on synthetic targets. Release A (Advisory) goes live as the advisory pilot after the M4 gates, and Release B (Controlled remediation) after the pilot accuracy and approval review (ADR-44).

NFR-15 (cost per incident) and NFR-26 (timing and tracing) stay at M1, because later measurements depend on them. The Technical Specification's telemetry in M4 means hardening only.

The later milestone names and acceptance criteria are in [Technical Specification, section 12](../../reference/technical-specification.md).

## Assumptions

| Assumption | Status | Owner | Validated when |
| --- | --- | --- | --- |
| Submissions are processed asynchronously after a fast acceptance ([Q4.1](conversation.md)) | Provisional | Platform owner | the intake latency target (Q4.28) is met while investigations run in the background. |
| Each alert carries an identifier or fingerprint that stays the same when it repeats ([Q4.2](conversation.md)) | Provisional | Platform owner | alert samples from the monitoring system are inspected. |
| Alerts can be mapped to a known service ([Q4.3](conversation.md)) | Provisional | Platform owner | every in-scope alert rule carries a service label. |
| The tools can be queried in parallel without breaching their limits ([Q4.4](conversation.md)) | Provisional | Platform owner | each tool's limits are checked. |
| Up to five hypotheses are enough for any single incident ([Q4.5](conversation.md)) | Provisional | SRE lead | replays of past incidents never need more. |
| A capable model, given the gathered evidence, can rank hypotheses accurately on change-caused failures ([Q4.6](conversation.md)) | Provisional | SRE lead | accuracy on the synthetic test suite is measured (Q4.33's budget allowing). |
| Evidence strength can be ranked consistently across incidents ([Q4.7](conversation.md)) | Provisional | SRE lead | two reviewers agree on rankings for 20 test cases. |
| A confidence threshold can be set from test results ([Q4.8](conversation.md)) | Provisional | SRE lead | the threshold separates correct from incorrect diagnoses on the test suite. |
| Reverting the release, or the configuration change, fixes most change-caused failures ([Q4.9](conversation.md)) | Provisional | SRE lead | past change-caused incidents are reviewed. |
| The scenario's failure can be reproduced in the disposable environment ([Q4.10](conversation.md)) | Provisional | Platform owner | the HTTP 500 errors reproduce there. |
| Approvers can be reached at night ([Q4.11](conversation.md)) | Provisional | Engineering manager | the on-call rota covers every in-scope service. |
| The release process can apply and roll back a single change on request ([Q4.12](conversation.md)) | Provisional | Platform owner | the scenario revert is applied and undone through it. |
| A hold period of 10 minutes is enough for the reference service ([Q4.13](conversation.md)) | Provisional | SRE lead | past recoveries show no relapse after 10 minutes. |
| Cancelling stops further work but doesn't undo an already-applied, verified change automatically ([Q4.14](conversation.md)) | Provisional | SRE lead | agreed with on-call engineers. |
| A retention period of 1 year meets review needs ([Q4.15](conversation.md)) | Provisional | Engineering manager | confirmed with the incident review process. |
| Monitoring retries failed alert deliveries for at least 15 minutes ([Q4.16](conversation.md)) | Provisional | Platform owner | the monitoring system's retry behaviour is checked. |
| All steps can be made idempotent, or skipped when already complete ([Q4.17](conversation.md)) | Provisional | Platform owner | each step is reviewed for repeat safety. |
| Database outages are usually shorter than 15 minutes ([Q4.18](conversation.md)) | Provisional | Platform owner | the database's availability history is reviewed. |
| Monitoring's own timestamp on the alert is accurate ([Q4.19](conversation.md)) | Provisional | Platform owner | clocks are compared. |
| 15 minutes is enough for change-caused failures ([Q4.20](conversation.md)) | Provisional | SRE lead | test-suite investigations finish within it. |
| Change-caused investigations need fewer than 30 model calls ([Q4.21](conversation.md)) | Provisional | SRE lead | measured on the test suite. |
| Past peak concurrency was 2 or fewer ([Q4.22](conversation.md)) | Provisional | SRE lead | past incident records are checked. |
| Common secret and personal-data patterns can be detected automatically ([Q4.23](conversation.md)) | Provisional | Security lead | detection is tested on a seeded corpus. |
| Point-in-time recovery is available ([Q4.24](conversation.md)) | Provisional | Platform owner | a restore is tested. |
| During the pilot, the manual process remains practised and available ([Q4.25](conversation.md)) | Provisional | Engineering manager | confirmed with on-call teams. |
| Each step can be timed and traced with the incident ID ([Q4.26](conversation.md)) | Provisional | Platform owner | a full trace of a scenario run is produced. |
| Timeouts can be set per dependency from measured normal latency ([Q4.27](conversation.md)) | Provisional | Platform owner | normal latency is measured. |
| A single database write and a lookup complete well under 100 ms ([Q4.28](conversation.md)) | Provisional | Platform owner | a load test is run. |
| The event is written together with the incident record and published from there ([Q4.29](conversation.md)) | Provisional | Platform owner | the design in Layer 7 confirms it. |
| Normal model calls for this task complete in under 20 seconds ([Q4.30](conversation.md)) | Provisional | SRE lead | measured on the test suite. |
| Most malformed outputs succeed on a retry with the error explained ([Q4.31](conversation.md)) | Provisional | SRE lead | retry success is measured. |
| Approvers respond within 15 minutes during an incident ([Q4.32](conversation.md)) | Provisional | Engineering manager | measured during the pilot. |
| A change-caused investigation needs under 150,000 input and 15,000 output tokens ([Q4.33](conversation.md)) | Provisional | SRE lead | measured on the test suite. |
| Historical peak concurrency is 2 or fewer ([Q4.34](conversation.md)) | Provisional | SRE lead | past records are checked. |
| Money values: USD, $2.00 per incident, $500 monthly, $16.67 daily (ADR-56) | Provisional | Engineering manager | finance confirms a budget. |
| One service may hold at most 4 of the 5 investigation slots (NFR-44) | Provisional | SRE lead | the burst test passes and pilot queue data shows no service starved. |
| The 10-minute grouping window runs from the incident opening (FR-43) | Provisional | Platform owner | alert samples show repeats of one failure arrive inside it. |

## Decisions and conflicts

| Item | Resolution | Decided by |
| --- | --- | --- |
| ID check | FR-01 to FR-50 and NFR-01 to NFR-47 are continuous and unique. Every ID cited in other layers exists. Only NFR-22 (Layer 12) and NFR-08 (docs README) are cited with an old meaning; FR-05, FR-06, FR-13 and FR-16 citations still hold. | Layer decision (proposed) |
| Retired row format | No Status column: one retired row doesn't need one, and the seven fields stay one per section. NFR-08 states its status in the Description. | Layer decision (proposed) |
| Mandatory fields: service identifier, environment, start time or alert time, symptom, source ([Q4.3](conversation.md)) | Register row FR-41. | Layer decision (proposed) |
| Deduplication key: fingerprint, or service plus alert name ([Q4.2](conversation.md)) | Register row FR-42. The stored key is never null. | ADR-58 |
| Alert grouping window: same service within 10 minutes ([Q4.2](conversation.md)) | Register row FR-43. The source sets no start point, so it runs from the incident opening. | Layer decision (proposed) |
| Evidence window: from 60 minutes before the start up to now ([Q4.4](conversation.md)) | Register row FR-44. | Layer decision (proposed) |
| Hypothesis statuses: open, supported, ruled out, confirmed ([Q4.5](conversation.md)) | Register row FR-45. | Layer decision (proposed) |
| Confidence levels: high, medium or low, with reasons ([Q4.7](conversation.md)) | Register row FR-46, using the ADR-53 rule. | ADR-53 |
| Patch preference: smallest reversal; no code fixes in the first version ([Q4.9](conversation.md)) | Register row FR-47. | Layer decision (proposed) |
| Patch scope check: affected service and allowed list ([Q4.9](conversation.md)) | Register rows FR-48 (service) and FR-40 (allowed list). | ADR-55 |
| High-impact changes: second approver ([Q4.11](conversation.md)) | Register row FR-49. Two different people; definition in ADR-49. | ADR-49 |
| Recovery hold period: 10 minutes ([Q4.13](conversation.md)) | Register row FR-50. 10 minutes by default, longer per service. Open decision closed. | ADR-66 |
| Crashed worker detection: 60 seconds ([Q4.17](conversation.md)) | Register row NFR-40. | Layer decision (proposed) |
| Whole-workflow limit: 24 hours from opening ([Q4.20](conversation.md)) | Register row NFR-41. `recovered` stays until the incident lead closes it. | ADR-46 |
| Spend limits: per incident, daily, monthly with alerts at 50%, 80%, 100% ([Q4.21](conversation.md)) | Register rows NFR-42 (per incident, M1, as Spec section 12 requires) and NFR-43 (monthly, M4). Daily is NFR-14. | ADR-56 |
| Fairness: one service can't take every slot ([Q4.22](conversation.md)) | Register row NFR-44: at most 4 of 5 slots (provisional). A service at that limit waits, the one exception to NFR-16's severity order. | Layer decision (proposed) |
| Retention: raw evidence 30 days, timeline 1 year ([Q4.23](conversation.md), [Q4.15](conversation.md)) | Register row NFR-45, using the ADR-57 periods. | ADR-57 |
| Backups and drills: continuous, daily full, quarterly drills ([Q4.24](conversation.md)) | Register row NFR-46. Backups kept 35 days. | ADR-57 |
| Dependency retries: 2 with backoff ([Q4.27](conversation.md)) | Register row NFR-47. Evidence sources only; model calls are NFR-33 (ADR-27). | Layer decision (proposed) |
| Conflict 1: NFR-08 p95 ≤ 2 s ([Q4.19](conversation.md)) against NFR-30 p95 ≤ 300 ms, p99 ≤ 1 s ([Q4.28](conversation.md)) for the same work | NFR-08 retired, superseded by NFR-30. ID kept, never reused. | ADR-55 |
| Conflict 2: what NFR-22's 1 hour restores ([Q4.24](conversation.md), [Q12.41](../layer-12-reliability-evaluation/conversation.md)) | ≤ 1 hour to read-only investigation mode; ≤ 4 hours to full operation including writes. | ADR-55 |
| Conflict 3: 5 called "double" a peak of 2 ([Q4.34](conversation.md), [Q2.17](../layer-02-scope-and-constraints/conversation.md)) | 5 is the limit. It is at least double the assumed peak of 2. | ADR-55 |
| Conflict 4: NFR-14 at 100% of daily spend ([Q4.21](conversation.md)) | New investigations are evidence-only. Running ones finish their current step, then stop and hand over. | ADR-55 |
| Conflict 5: "no alert is ever dropped" ([Q2.17](../layer-02-scope-and-constraints/conversation.md)) against FR-05 and FR-06 rejections | Rejected alerts go to a dead-letter record with the reason, and the incident lead is notified. No incident is created. | ADR-58 |
| Conflict 6: FR-13's ≥ 80% credited to Artifact 1's review-confirmed measure ([Layer 1](../layer-01-problem-discovery/conversation.md)) | Both measures are kept. Suite accuracy gates each release (FR-13); confirmation by incident review is the pilot business measure. | ADR-65 |
| Conflict 7: Spec criteria "injection 0 unsafe" (M1) and "patches only from the allowed list" (M2) had no row | Register rows FR-39 and FR-40. | ADR-55 |
| Conflict 8: Spec delivers telemetry in M4; NFR-26 and NFR-15 are M1 | NFR-15 and NFR-26 stay at M1. Telemetry in M4 means hardening only. | Layer decision (proposed) |
| Conflict 9: provisional tables hold rows with no label, such as NFR-03 and NFR-10 | Everything in a provisional table stays provisional, whether or not its row is labelled. | ADR-55 |
| Code status | Daily and monthly ceilings are in `gateway.py`, with totals in memory, and the monthly alerts are in `alerts.yml`. Dead-letter intake, the 10-minute same-service join and the per-service slot limit have no module in `files/` yet. | ADR-56, ADR-58 |
| Disagreements between Claude and ChatGPT | **Needs the team:** [conversation.md](conversation.md) records none; that section is TBD, Owner: Team, settled when: conversation logs are filled in. | Open: can't be set by decision |

## Acceptance checks

- [ ] Every required section above is present and complete.
- [x] Every section lists the questions it answers, and every question in this layer is answered by at least one section.
- [x] Every assumption is marked provisional and has a named owner and a condition under which it will be validated.
- [ ] Every disagreement between Claude and ChatGPT that affected a decision is recorded with the team's resolution.
- [ ] The artifact is consistent with every artifact it is built from; any conflict is recorded as an explicit decision.
- [x] The artifact was produced with the team's coding agent and is saved in the team repository.

Reviewed by: TBD
Date: TBD
