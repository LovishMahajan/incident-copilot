# Layer 4: Functional and Non-Functional Requirements — Conversation

- **Central question:** What must the system do, and how well must it do it?
- **Built from:** Artifacts 1, 2 and 3
- **Focus for this layer:** Distinguish what the system does from how well it must do it. Every requirement must be measurable; a requirement that cannot be tested is not yet a requirement.
- **Deliverable:** [Artifact 4: Requirements Specification](artifact-04-requirements-specification.md)
- **Questions:** 34

## Conversation log

| Questions | Agent | Conversation link | Decision reached | Open unknowns and owner | Artifact section |
| --- | --- | --- | --- | --- | --- |
| Q4.1–Q4.34 | TBD | [Shared conversation](https://claude.ai/share/c6a48a50-d511-4886-b4f5-0a3000f6a725) (needs sign-in; not read for these docs) | Recorded per question below | Listed under each question's Unknowns and Assumptions | [Artifact 4: Requirements Specification](artifact-04-requirements-specification.md) |

## Where Claude and ChatGPT disagreed

TBD: the source documents do not record any disagreements. The team fills this in.

## Questions and answers

### Q4.1 An engineer submits an incident to the platform. What sequence of operations should happen after submission?

**Frame.** What should the platform do, in order, once an incident is submitted?

**Knowns.**

- In the reference scenario, a deployment changes a configuration value, the Python web application returns HTTP 500 errors, and monitoring detects it.
- Artifact 1 set the journey: open one record, notify people, gather evidence, rank causes, propose a fix, test it, get approval, apply it, verify recovery, close.
- Artifact 2 set the first summary within 5 minutes, and the full investigation within 15 minutes.

**Unknowns.** Whether engineers submit manually as well as through monitoring alerts; what the caller needs back immediately.

**Assumptions (provisional).**

- Submissions are processed asynchronously after a fast acceptance. *Owner: Platform owner. Validated when: the intake latency target (Q4.28) is met while investigations run in the background.*

**Answer.**

1. **Validate** the submission and check its mandatory fields (Q4.3).
2. **Check for duplicates** (Q4.2).
3. **Create the incident record** and return its ID immediately.
4. **Publish an event** for workers to pick up (Q4.29).
5. **Notify** the owner, the on-call engineer and the author of the last change.
6. **Collect evidence** (Q4.4).
7. **Build hypotheses and a diagnosis** (Q4.5–Q4.8).
8. **Send the first summary** within 5 minutes.
9. **Propose a patch, test it, request approval, execute it and verify recovery** (Q4.9–Q4.13).
10. **Record every step** in the timeline (Q4.15).

**Requirements.**

| **ID** | **Description** | **Priority** | **Measurement** | **Acceptance criterion** | **Validation method** | **Milestone** |
| --- | --- | --- | --- | --- | --- | --- |
| FR-01 | Accepting an incident returns an incident ID and starts the workflow asynchronously | Must | Response contains an ID; workflow-start event recorded | 100% of valid submissions return an ID and produce exactly one workflow | Integration test with the scenario fixture | M1 |
| FR-02 | The workflow runs the steps in order and records each one | Must | Timeline entries per step | Every completed incident shows all steps in order, with timestamps | Test against the scenario fixture | M1 |

### Q4.2 Monitoring systems send the same alert multiple times. How should the platform prevent duplicate investigations?

**Frame.** How do we stop repeated alerts from starting repeated investigations?

**Knowns.**

- Alerts come from the organisation's existing monitoring.
- Artifact 2 decided that related alerts are grouped into one incident, and that no alert is ever dropped.

**Unknowns.** Whether monitoring sends a stable alert identifier; how often it repeats alerts; how far apart repeats arrive.

**Assumptions (provisional).**

- Each alert carries an identifier or fingerprint that stays the same when it repeats. *Owner: Platform owner. Validated when: alert samples from the monitoring system are inspected.*

**Answer.**

- **Build a deduplication key** from the alert's fingerprint, or from service plus alert name if there is none.
- **If an open incident has the same key**, attach the new alert to it as evidence, and don't start a new investigation.
- **Group related alerts:** alerts for the same service within a set window (provisional: 10 minutes) join the open incident.
- **Make submission idempotent:** a client-supplied request key returns the same incident when the same request is retried.
- **Record every alert**, including duplicates, in the incident's timeline.

**Requirements.**

| **ID** | **Description** | **Priority** | **Measurement** | **Acceptance criterion** | **Validation method** | **Milestone** |
| --- | --- | --- | --- | --- | --- | --- |
| FR-03 | Duplicate alerts attach to the existing open incident | Must | Investigations started per unique deduplication key | Sending the same alert 10 times creates 1 investigation and 10 timeline entries | Replay test | M1 |
| FR-04 | Retried submissions with the same request key are idempotent | Must | Incidents created per request key | 1 incident per request key | API test | M1 |

### Q4.3 An investigation is created without sufficient context to identify the affected application. Which information should be mandatory?

**Frame.** What must every incident include so the affected application can be identified?

**Knowns.**

- Artifact 3 lists the alert, the service and the time window as the first evidence needed.
- Artifact 2 limits the first version to one organisation and to Python applications.

**Unknowns.** Which fields the monitoring system always sends.

**Assumptions (provisional).**

- Alerts can be mapped to a known service. *Owner: Platform owner. Validated when: every in-scope alert rule carries a service label.*

**Answer.** Mandatory fields:

| **Field** | **Why it is required** |
| --- | --- |
| **Service identifier** (matching the service catalogue) | Finds the owner, evidence sources and change history |
| **Environment** | Avoids investigating the wrong copy of the service |
| **Start time** (or alert time) | Sets the evidence window |
| **Symptom:** alert name or description | States what is wrong |
| **Source:** monitoring system or engineer | Traceability |

Optional but valuable: severity, alert threshold and value, affected paths, links.

If a mandatory field is missing, the submission is **rejected with a clear error** naming the missing fields. The platform never guesses the service.

**Requirements.**

| **ID** | **Description** | **Priority** | **Measurement** | **Acceptance criterion** | **Validation method** | **Milestone** |
| --- | --- | --- | --- | --- | --- | --- |
| FR-05 | Submissions without mandatory fields are rejected, naming the fields | Must | Rejection rate and error content | 100% of submissions missing any mandatory field are rejected; 0 investigations start | API validation tests | M1 |
| FR-06 | The service identifier must match the service catalogue | Must | Unknown-service submissions | Unknown services are rejected and the incident lead is notified | Test with an unknown service | M1 |

### Q4.4 An investigation needs information from several external systems. How should the evidence collection process work?

**Frame.** How should evidence be gathered from several external tools?

**Knowns.**

- Artifact 2: evidence is read from existing tools with read-only access.
- Artifact 3: evidence is collected in a set order (alert, impact, service context, timeline, changes, telemetry, dependencies, history). Each tool is reached through its own adapter, and an unavailable tool is reported as a gap, never as "healthy".

**Unknowns.** Each tool's response time and rate limits.

**Assumptions (provisional).**

- The tools can be queried in parallel without breaching their limits. *Owner: Platform owner. Validated when: each tool's limits are checked.*

**Answer.**

1. **Query in parallel** through one adapter per tool, each with its own timeout.
2. **Use a fixed time window:** from a set period before the start (provisional: 60 minutes) up to now.
3. **Normalise everything** into common evidence records, each with its source, collection time and a link to the original.
4. **Record gaps explicitly** for any tool that times out or fails.
5. **Collect changes first**, since change-caused failures are the first version's focus.
6. **Respect the time budget:** evidence collection finishes within its share of the 5-minute first summary.

**Requirements.**

| **ID** | **Description** | **Priority** | **Measurement** | **Acceptance criterion** | **Validation method** | **Milestone** |
| --- | --- | --- | --- | --- | --- | --- |
| FR-07 | Every evidence item records its source, collection time and a link | Must | Share of items with all three fields | 100% | Schema test | M1 |
| FR-08 | A failed or timed-out source is recorded as a gap, never as empty or healthy | Must | Gap records when a source fails | A source outage in testing produces a gap record, and the diagnosis reports it | Fault-injection test | M1 |
| FR-09 | Evidence collection completes within its time budget | Should | p95 evidence-collection time | ≤ 3 minutes p95 (provisional) | Timing on the test suite | M1 |

### Q4.5 The agent identifies three possible causes for an incident. How should these competing hypotheses be represented?

**Frame.** How do we record several possible causes side by side?

**Knowns.** Artifact 3 requires listing at least two hypotheses, including "not the deployment", with evidence for and against each.

**Unknowns.** How many hypotheses are useful before it becomes noise.

**Assumptions (provisional).**

- Up to five hypotheses are enough for any single incident. *Owner: SRE lead. Validated when: replays of past incidents never need more.*

**Answer.** Each hypothesis is a record with:

- a **statement** of the cause, for example "the changed configuration value breaks the request handler"
- **supporting evidence:** links to evidence items
- **contradicting evidence:** links to evidence items
- **what would confirm it**, and **what would rule it out**
- a **confidence level** (Q4.7)
- a **status:** open, supported, ruled out, or confirmed
- the **proposed fix**, if any

All hypotheses stay visible; ruled-out ones keep the reason they were ruled out.

**Requirements.**

| **ID** | **Description** | **Priority** | **Measurement** | **Acceptance criterion** | **Validation method** | **Milestone** |
| --- | --- | --- | --- | --- | --- | --- |
| FR-10 | Each hypothesis records its statement, evidence for and against, confidence and status | Must | Field completeness | 100% of hypotheses have all fields | Schema test | M1 |
| FR-11 | At least two hypotheses are considered for every diagnosis | Must | Hypotheses per investigation | ≥ 2 for 100% of diagnosed incidents | Test suite review | M1 |

### Q4.6 The system has collected evidence and needs to determine the most likely root cause. How should diagnoses be generated?

**Frame.** How does the platform turn evidence into a most-likely cause?

**Knowns.**

- Artifact 3 defines the evidence that strengthens a deployment hypothesis (reproduction, version split, configuration match, code match, new error type) and the evidence that contradicts it.
- Artifact 1 requires every claim to show its evidence.

**Unknowns.** The accuracy of the model on our test cases.

**Assumptions (provisional).**

- A capable model, given the gathered evidence, can rank hypotheses accurately on change-caused failures. *Owner: SRE lead. Validated when: accuracy on the synthetic test suite is measured (Q4.33's budget allowing).*

**Answer.**

1. **Pre-compute facts in code:** the timeline, version split and changed values.
2. **Ask the model** to propose and rank hypotheses using only the supplied evidence. Every claim must cite an evidence ID.
3. **Validate the output:** a strict structure; cited evidence must exist; no fixes outside the allowed list.
4. **Score confidence** (Q4.7).
5. **Produce the diagnosis:** the leading hypothesis with its confidence, the alternatives, and the missing evidence. Or produce an inconclusive result (Q4.8).

**Requirements.**

| **ID** | **Description** | **Priority** | **Measurement** | **Acceptance criterion** | **Validation method** | **Milestone** |
| --- | --- | --- | --- | --- | --- | --- |
| FR-12 | Every claim in a diagnosis cites existing evidence | Must | Uncited or invalid citations | 0 in the test suite | Automated citation check | M1 |
| FR-13 | Diagnosis accuracy on change-caused synthetic incidents | Must | Leading hypothesis matches the known cause | ≥ 80% (provisional, from Artifact 1) | Evaluation suite | M1 |

### Q4.7 Two hypotheses have conflicting supporting evidence. How should the system evaluate and compare them?

**Frame.** How do we weigh two hypotheses whose evidence conflicts?

**Knowns.** Artifact 3 ranks evidence by strength (reproduction strongest, timing alone weakest) and requires keeping both explanations open until the evidence separates them.

**Unknowns.** How to calibrate confidence numbers.

**Assumptions (provisional).**

- Evidence strength can be ranked consistently across incidents. *Owner: SRE lead. Validated when: two reviewers agree on rankings for 20 test cases.*

**Answer.**

- **Weigh by strength, not count:** one reproduction outweighs many timing matches.
- **Contradicting evidence counts more than supporting evidence:** one strong contradiction can rule a hypothesis out.
- **Find a deciding test:** identify the observation that would separate the two (for example, do old-version instances fail too?) and collect it.
- **If they can't be separated:** report both side by side, with equal standing, and hand over to a person (Q4.8).
- **Confidence levels:** high, medium or low, with the reasons stated.

**Requirements.**

| **ID** | **Description** | **Priority** | **Measurement** | **Acceptance criterion** | **Validation method** | **Milestone** |
| --- | --- | --- | --- | --- | --- | --- |
| FR-14 | Conflicting hypotheses are shown side by side with a proposed deciding test | Must | Behaviour on conflict test cases | 100% of conflict cases show both and a deciding test | Test cases with conflicting evidence | M1 |
| FR-15 | A single strong contradiction lowers or rules out a hypothesis | Should | Behaviour on contradiction cases | Hypothesis status changes in 100% of contradiction cases | Test cases based on Artifact 3 | M1 |

### Q4.8 The AI cannot confidently identify a root cause. How should the system represent an inconclusive diagnosis?

**Frame.** How is "we don't know" recorded and shown?

**Knowns.** Artifact 1 and Artifact 3: when evidence is too thin, the platform says so, never presents its best guess as a conclusion, and hands over to a person.

**Unknowns.** The confidence threshold below which a diagnosis is inconclusive.

**Assumptions (provisional).**

- A confidence threshold can be set from test results. *Owner: SRE lead. Validated when: the threshold separates correct from incorrect diagnoses on the test suite.*

**Answer.** An inconclusive diagnosis is its own status, not a low-confidence answer. It contains:

- the label **"Root cause not established"**
- the remaining hypotheses with their evidence
- the missing evidence, and how to get it
- the reason it stopped (time limit, sources exhausted, conflicting evidence)
- suggested next steps, limited to safe information gathering
- the person it was handed to

No patch is proposed from an inconclusive diagnosis.

**Requirements.**

| **ID** | **Description** | **Priority** | **Measurement** | **Acceptance criterion** | **Validation method** | **Milestone** |
| --- | --- | --- | --- | --- | --- | --- |
| FR-16 | Below the confidence threshold, the diagnosis is marked "Root cause not established" | Must | Behaviour on evidence-withheld cases | 100% of withheld-evidence cases are inconclusive; 0 present a guess as the cause | Honesty test (evidence removed) | M1 |
| FR-17 | No patch is proposed from an inconclusive diagnosis | Must | Patches from inconclusive diagnoses | 0 | Test suite | M2 |

### Q4.9 A diagnosis indicates that a code change is required. How should the system convert the diagnosis into a proposed patch?

**Frame.** How does a diagnosis become a concrete proposed change?

**Knowns.**

- In the scenario, the fix is reverting a configuration value.
- Artifact 2: changes go through the organisation's existing release process; the platform never edits history directly.

**Unknowns.** Which kinds of patch are allowed in the first version.

**Assumptions (provisional).**

- Reverting the release, or the configuration change, fixes most change-caused failures. *Owner: SRE lead. Validated when: past change-caused incidents are reviewed.*

**Answer.**

1. **Prefer the smallest reversal:** revert the configuration value, then the commit, then the whole release.
2. **Generate a code fix only** when reverting isn't possible. This is out of scope for the first version.
3. **Generate the patch as a proposed change** in the normal review system (for example, a pull request), never as a direct commit.
4. **The patch describes:**
    - the hypothesis it addresses, and the evidence
    - the exact change (a diff)
    - the expected effect
    - the risks
    - how to undo it
5. **Check the patch** stays within the affected service and the allowed list of change types.

**Requirements.**

| **ID** | **Description** | **Priority** | **Measurement** | **Acceptance criterion** | **Validation method** | **Milestone** |
| --- | --- | --- | --- | --- | --- | --- |
| FR-18 | Patches are proposed through the review system, never committed directly | Must | Direct writes to protected branches | 0 | Permission audit and test | M2 |
| FR-19 | Each patch includes the hypothesis, evidence, diff, expected effect, risk and undo plan | Must | Field completeness | 100% | Schema test | M2 |
| FR-20 | For the scenario, the proposed patch reverts only the changed configuration value | Must | Patch content | The diff changes exactly that value | Scenario test | M2 |

### Q4.10 A patch has been generated, but its correctness is unknown. How should the platform test it?

**Frame.** How do we check a patch works before anyone approves it?

**Knowns.** Artifact 2: patches are tested in a fresh, isolated, short-lived environment with no production access, destroyed afterwards.

**Unknowns.** How faithfully the disposable environment reproduces production failures.

**Assumptions (provisional).**

- The scenario's failure can be reproduced in the disposable environment. *Owner: Platform owner. Validated when: the HTTP 500 errors reproduce there.*

**Answer.**

1. **Reproduce first:** create the environment at the failing version and confirm the failure appears. If it doesn't, the test is invalid.
2. **Apply the patch.**
3. **Run the checks:**
    - the original symptom is gone (no HTTP 500 errors on the failing path)
    - the existing automated tests pass
    - no new errors appear
4. **Record the results** as evidence, and destroy the environment.

**Requirements.**

| **ID** | **Description** | **Priority** | **Measurement** | **Acceptance criterion** | **Validation method** | **Milestone** |
| --- | --- | --- | --- | --- | --- | --- |
| FR-21 | A patch is tested only after the failure is reproduced in the disposable environment | Must | Tests without reproduction | 0 | Test suite | M2 |
| FR-22 | The disposable environment has no access to production systems, data or credentials | Must | Blocked access attempts | 100% of production access attempts blocked | Security test | M2 |
| FR-23 | Test results are stored as evidence, and the environment is destroyed afterwards | Must | Environments left running | 0 after 60 minutes | Resource audit | M2 |

### Q4.11 A patch passes automated tests but could affect production. How should a human review and authorize it?

**Frame.** How does a person review and approve a tested patch?

**Knowns.** Artifact 1: any production change needs approval from a named, accountable person (the service owner or on-call engineer; plus the incident lead for high-impact changes). The platform never approves its own changes.

**Unknowns.** The organisation's approval rules by service and severity.

**Assumptions (provisional).**

- Approvers can be reached at night. *Owner: Engineering manager. Validated when: the on-call rota covers every in-scope service.*

**Answer.**

1. **Send an approval request** to the authorised approver, containing:
    - the diagnosis and evidence
    - the patch diff
    - the test results
    - the risk and undo plan
2. **The approver chooses** approve, reject, or ask for more evidence.
3. **The decision is recorded** with who, when, what was approved, and why.
4. **High-impact changes** need a second approver.
5. **Approval expires** after a set time, or if the service changes (Q4.32).

**Requirements.**

| **ID** | **Description** | **Priority** | **Measurement** | **Acceptance criterion** | **Validation method** | **Milestone** |
| --- | --- | --- | --- | --- | --- | --- |
| FR-24 | No production change runs without a recorded approval from an authorised person | Must | Executions without approval | 0 | Negative test; audit | M3 |
| FR-25 | The approval request contains the diagnosis, evidence, diff, test results, risk and undo plan | Must | Field completeness | 100% | Schema test | M3 |
| FR-26 | The platform's own identity cannot approve | Must | Self-approval attempts | 100% rejected | Security test | M3 |

### Q4.12 A patch has been approved. How should the platform execute the authorized operation?

**Frame.** How is an approved patch applied safely?

**Knowns.**

- Artifact 2: execution goes through the existing release process, with write access limited to the specific approved change.
- Artifact 1: every change is reversible.

**Unknowns.** The release process's interface for applying a change.

**Assumptions (provisional).**

- The release process can apply and roll back a single change on request. *Owner: Platform owner. Validated when: the scenario revert is applied and undone through it.*

**Answer.**

1. **Re-check before acting:** the approval is valid; the service's version and configuration are unchanged since approval; the patch is identical to what was approved.
2. **Apply only the approved change** through the release process.
3. **Execute once:** a unique execution key prevents repeating it.
4. **Record** the start, finish and outcome.
5. **Move straight to verification** (Q4.13).

**Requirements.**

| **ID** | **Description** | **Priority** | **Measurement** | **Acceptance criterion** | **Validation method** | **Milestone** |
| --- | --- | --- | --- | --- | --- | --- |
| FR-27 | Execution re-checks approval validity and service state before applying | Must | Executions with stale approval or changed state | 0 | Test with state changed after approval | M3 |
| FR-28 | The executed change is identical to the approved change | Must | Differences between approved and applied diff | 0 | Hash comparison test | M3 |
| FR-29 | Each approved change executes at most once | Must | Duplicate executions under retry | 0 | Retry and replay test | M3 |

### Q4.13 A repair has been applied, but the service may still be unhealthy. How should the platform verify recovery?

**Frame.** How do we prove the service has recovered?

**Knowns.** Artifact 1: recovery is defined before the fix by the original symptom, held for a set period, checked by a step separate from the one that applied the fix. If it fails, the change is undone and a person is involved.

**Unknowns.** How long recovery must hold for this service.

**Assumptions (provisional).**

- A hold period of 10 minutes is enough for the reference service. *Owner: SRE lead. Validated when: past recoveries show no relapse after 10 minutes.*

**Answer.**

1. **Define recovery before execution:**
    - the error rate is back to its pre-incident level
    - the failing path succeeds
    - no new error types appear
2. **Measure from monitoring**, independently of the step that executed the fix.
3. **Require it to hold** for the hold period (provisional: 10 minutes).
4. **If it holds:** mark the incident recovered, and ask a person to accept it.
5. **If it doesn't:** undo the change (it was approved as reversible), notify the approver, and return the incident to a person.

**Requirements.**

| **ID** | **Description** | **Priority** | **Measurement** | **Acceptance criterion** | **Validation method** | **Milestone** |
| --- | --- | --- | --- | --- | --- | --- |
| FR-30 | Recovery criteria are recorded before execution | Must | Executions without recorded criteria | 0 | Test suite | M3 |
| FR-31 | Recovery is verified from monitoring data, independently of the executor | Must | Verification source | 100% verified from monitoring | Code review and test | M3 |
| FR-32 | A failed verification undoes the change and escalates | Must | Behaviour when the fix doesn't help | 100% undone and escalated in the failed-fix test | Test with an ineffective patch | M3 |

### Q4.14 An engineer wants to stop an ongoing investigation. How should cancellation work?

**Frame.** How does an engineer stop an investigation cleanly?

**Knowns.** Artifact 1: people stay in control; the record is complete.

**Unknowns.** Whether cancellation should also undo changes already applied.

**Assumptions (provisional).**

- Cancelling stops further work but doesn't undo an already-applied, verified change automatically. *Owner: SRE lead. Validated when: agreed with on-call engineers.*

**Answer.**

- **Who:** an authorised engineer for the service, or the incident lead.
- **Effect:**
    - stop all running steps at the next safe point
    - cancel pending model calls and evidence queries
    - destroy disposable environments
    - withdraw open approval requests
- **Never mid-change:** a production change already executing completes, then verification runs. Nothing is left half-applied.
- **Record the reason:** who cancelled, when, and why. The incident stays open for the person to handle.
- **Idempotent:** cancelling twice has the same effect as once.

**Requirements.**

| **ID** | **Description** | **Priority** | **Measurement** | **Acceptance criterion** | **Validation method** | **Milestone** |
| --- | --- | --- | --- | --- | --- | --- |
| FR-33 | Cancellation stops all work within a time limit | Must | Time from cancel to all work stopped | ≤ 30 seconds p95 (provisional) | Cancellation test | M1 |
| FR-34 | Cancellation withdraws open approvals and destroys disposable environments | Must | Open approvals and environments after cancel | 0 | Test | M3 |
| FR-35 | Cancellation never leaves a production change half-applied | Must | Partial changes after cancel mid-execution | 0 | Test cancel during execution | M3 |

### Q4.15 An engineer needs to understand every action taken during an incident. How should the platform expose its timeline?

**Frame.** How does an engineer see everything that happened?

**Knowns.** Artifact 1: one incident record; every event, decision, approval and action added in time order, never edited, kept after closing.

**Unknowns.** How long timelines must be retained.

**Assumptions (provisional).**

- A retention period of 1 year meets review needs. *Owner: Engineering manager. Validated when: confirmed with the incident review process.*

**Answer.**

- **Append-only entries**, each with:
    - a timestamp
    - the actor (platform, or a named person)
    - the type (alert, evidence, hypothesis, approval, action, verification, cancellation)
    - a summary
    - links to details
- **Available** through the API and the command-line tool, filterable by type.
- **Exportable** for incident reviews.
- **Tamper-evident:** entries can't be edited or deleted; corrections are new entries.

**Requirements.**

| **ID** | **Description** | **Priority** | **Measurement** | **Acceptance criterion** | **Validation method** | **Milestone** |
| --- | --- | --- | --- | --- | --- | --- |
| FR-36 | Every platform and human action creates a timeline entry | Must | Actions without an entry | 0 in a full scenario run | Scenario audit | M1 |
| FR-37 | Timeline entries cannot be edited or deleted | Must | Successful edit or delete attempts | 0 | Security test | M1 |
| FR-38 | The timeline is available through the API and the command-line tool | Must | Availability of both | Both return the full timeline | Integration test | M1 |

### Q4.16 The incident intake service becomes unavailable occasionally. What reliability target should it meet?

**Frame.** How reliable must incident intake be?

**Knowns.** Artifact 2: alerts are never dropped; monitoring keeps detecting incidents if the platform fails.

**Unknowns.** Whether the monitoring system retries failed deliveries, and for how long.

**Assumptions (provisional).**

- Monitoring retries failed alert deliveries for at least 15 minutes. *Owner: Platform owner. Validated when: the monitoring system's retry behaviour is checked.*

**Answer.**

- **Availability of intake: 99.9% per month** (provisional), about 43 minutes of downtime a month.
- **No accepted incident is ever lost:** an incident is acknowledged only after it is durably stored.
- **Intake is independent of the workers:** it keeps accepting incidents when investigation is slow or down.
- **During an outage**, monitoring's retries carry alerts over, and engineers handle incidents manually as today.

**Requirements.**

| **ID** | **Description** | **Priority** | **Measurement** | **Acceptance criterion** | **Validation method** | **Milestone** |
| --- | --- | --- | --- | --- | --- | --- |
| NFR-01 | Intake availability | Must | Successful intake requests ÷ total, per month | ≥ 99.9% (provisional) | Production monitoring; load test | M4 |
| NFR-02 | Acknowledged incidents are never lost | Must | Lost incidents after crash tests | 0 | Crash test after acknowledgement | M4 |

### Q4.17 A worker crashes halfway through an investigation. How should the system recover without losing progress?

**Frame.** How does an investigation survive a worker crash?

**Knowns.** Artifact 1: the incident record is complete and in order.

**Unknowns.** Which steps are safe to repeat.

**Assumptions (provisional).**

- All steps can be made idempotent, or skipped when already complete. *Owner: Platform owner. Validated when: each step is reviewed for repeat safety.*

**Answer.**

- **Save progress after each step** (a checkpoint) in durable storage.
- **Detect crashes:** a worker that stops reporting is considered failed after a set time (provisional: 60 seconds).
- **Resume:** another worker picks up from the last checkpoint, and doesn't start over.
- **No duplicate side effects:** notifications, approvals and executions use unique keys, so repeating a step doesn't repeat its effect.
- **Record the restart** in the timeline.

**Requirements.**

| **ID** | **Description** | **Priority** | **Measurement** | **Acceptance criterion** | **Validation method** | **Milestone** |
| --- | --- | --- | --- | --- | --- | --- |
| NFR-03 | A crashed investigation resumes from its last checkpoint | Must | Time to resume; steps repeated | Resumes within 2 minutes; completed steps not repeated | Kill-worker test | M4 |
| NFR-04 | No duplicate notifications, approvals or executions after a crash | Must | Duplicates | 0 | Kill-worker test at each step | M4 |

### Q4.18 PostgreSQL becomes unavailable while several incidents are active. What should happen to the ongoing investigations?

**Frame.** What happens to active investigations when the database goes down?

**Knowns.** The incident record and checkpoints live in the database. Artifact 1: no action without a recorded approval.

**Unknowns.** How long database outages typically last.

**Assumptions (provisional).**

- Database outages are usually shorter than 15 minutes. *Owner: Platform owner. Validated when: the database's availability history is reviewed.*

**Answer.**

- **Pause safely:** workers stop at the next checkpoint and retry the database with backoff. They do not continue with unrecorded steps.
- **Never act unrecorded:** no approvals are accepted and no changes executed while the record can't be written.
- **Intake:** returns "temporarily unavailable" so monitoring retries. Optionally, buffer incoming alerts durably elsewhere.
- **Tell people:** notify on-call engineers that the platform is paused, so they handle incidents manually.
- **Resume** from checkpoints when the database returns.

**Requirements.**

| **ID** | **Description** | **Priority** | **Measurement** | **Acceptance criterion** | **Validation method** | **Milestone** |
| --- | --- | --- | --- | --- | --- | --- |
| NFR-05 | No action executes while the incident record can't be written | Must | Actions during a database outage | 0 | Database outage test | M4 |
| NFR-06 | Investigations resume from checkpoints after the database returns | Must | Investigations resumed | 100% resumed within 2 minutes of recovery | Outage test | M4 |
| NFR-07 | Engineers are notified that the platform is paused | Must | Time to notification | ≤ 2 minutes after the outage starts | Outage test | M4 |

### Q4.19 Monitoring detects an incident, but the platform takes several minutes to acknowledge it. How should acknowledgement latency be measured and bounded?

**Frame.** How do we measure and limit the time to acknowledge an incident?

**Knowns.** Artifact 2: first summary within 5 minutes.

**Unknowns.** The delay between monitoring detecting and sending the alert.

**Assumptions (provisional).**

- Monitoring's own timestamp on the alert is accurate. *Owner: Platform owner. Validated when: clocks are compared.*

**Answer.**

- **Measure two intervals:**
    - **Platform acknowledgement:** from receiving the alert to the incident record being created and the ID returned.
    - **End to end:** from monitoring's alert time to the first notification reaching an engineer.
- **Bounds (provisional):**
    - platform acknowledgement **p95 ≤ 2 seconds**
    - first notification **p95 ≤ 60 seconds** from alert time
    - first summary **p95 ≤ 5 minutes**
- **Alert on breaches.**

**Requirements.**

| **ID** | **Description** | **Priority** | **Measurement** | **Acceptance criterion** | **Validation method** | **Milestone** |
| --- | --- | --- | --- | --- | --- | --- |
| NFR-08 | Platform acknowledgement latency | Must | Receive-to-record time, p95 | ≤ 2 s (provisional) | Load test; production metric | M4 |
| NFR-09 | Time to first notification | Must | Alert-time-to-notification, p95 | ≤ 60 s (provisional) | Scenario timing | M1 |
| NFR-10 | Time to first summary | Must | Alert-time-to-summary, p95 | ≤ 5 min (from Artifact 2) | Scenario timing | M1 |

### Q4.20 An investigation continues consuming resources indefinitely. What maximum execution duration should be enforced?

**Frame.** How long may one investigation run?

**Knowns.** Artifact 2: the full investigation is limited to 15 minutes; when a limit is reached, the platform hands over with partial findings.

**Unknowns.** Whether some incident types need longer.

**Assumptions (provisional).**

- 15 minutes is enough for change-caused failures. *Owner: SRE lead. Validated when: test-suite investigations finish within it.*

**Answer.**

- **Investigation (evidence to diagnosis): 15 minutes maximum.**
- **Each step** has its own timeout (Q4.30 for model calls).
- **Waiting for a person** (approval) doesn't count against the investigation limit, but is limited by approval expiry (Q4.32).
- **Whole workflow:** a hard limit of 24 hours from opening (provisional), after which the platform stops and hands over.
- **On limit:** stop, release resources, hand over with findings so far (Q4.8).

**Requirements.**

| **ID** | **Description** | **Priority** | **Measurement** | **Acceptance criterion** | **Validation method** | **Milestone** |
| --- | --- | --- | --- | --- | --- | --- |
| NFR-11 | The investigation stops at 15 minutes and hands over | Must | Maximum investigation duration | No investigation exceeds 15 min + 30 s | Slow-source test | M1 |
| NFR-12 | Resources are released when a limit is reached | Must | Resources held after stop | 0 workers, environments or model calls active | Test | M4 |

### Q4.21 An incident triggers hundreds of model calls and unexpectedly increases the cloud bill. What expenditure limits should be enforced?

**Frame.** What spending limits stop a runaway incident?

**Knowns.** Artifact 2: a model-call budget per incident, set as a small fraction (provisional 10%) of the engineer time saved; a hard cap that hands over when reached; a monthly infrastructure ceiling with alerts.

**Unknowns.** The actual cost per incident.

**Assumptions (provisional).**

- Change-caused investigations need fewer than 30 model calls. *Owner: SRE lead. Validated when: measured on the test suite.*

**Answer.**

| **Limit** | **Value (provisional)** | **On breach** |
| --- | --- | --- |
| Model calls per incident | 30 | Stop and hand over |
| Token budget per incident | See Q4.33 | Stop and hand over |
| Spend per incident | Derived from the per-incident budget | Stop and hand over |
| Daily spend across all incidents | Set from the monthly budget ÷ 30 | Alert at 80%; at 100%, new investigations evidence-only |
| Monthly spend | Budget ceiling | Alerts at 50%, 80%, 100% |

All limits are enforced before each call, not after.

**Requirements.**

| **ID** | **Description** | **Priority** | **Measurement** | **Acceptance criterion** | **Validation method** | **Milestone** |
| --- | --- | --- | --- | --- | --- | --- |
| NFR-13 | The per-incident model-call cap is enforced before each call | Must | Calls beyond the cap | 0 | Runaway-loop test | M1 |
| NFR-14 | Daily spend limit triggers alerts and evidence-only mode | Must | Behaviour at 80% and 100% | Alert at 80%; no model calls at 100% | Simulated spend test | M4 |
| NFR-15 | Actual cost is recorded per incident | Must | Incidents with cost recorded | 100% | Report check | M1 |

### Q4.22 Several incidents arrive simultaneously. How should the system manage concurrent investigations?

**Frame.** How are simultaneous incidents handled?

**Knowns.** Artifact 2: related alerts are grouped; a provisional limit of 5 concurrent investigations; the rest queue by severity; no alert is dropped.

**Unknowns.** The real peak concurrency.

**Assumptions (provisional).**

- Past peak concurrency was 2 or fewer. *Owner: SRE lead. Validated when: past incident records are checked.*

**Answer.**

- **Group first:** related alerts join one incident (Q4.2).
- **Limit:** at most 5 investigations run at once (Q4.34).
- **Queue the rest by severity**, then arrival time. Engineers are told an incident is queued, and its position.
- **Fairness:** one service can't take every slot.
- **Never drop:** queued incidents are recorded and visible.

**Requirements.**

| **ID** | **Description** | **Priority** | **Measurement** | **Acceptance criterion** | **Validation method** | **Milestone** |
| --- | --- | --- | --- | --- | --- | --- |
| NFR-16 | Excess incidents are queued by severity and never dropped | Must | Dropped incidents; queue order | 0 dropped; order matches severity | Burst test with 20 incidents | M4 |
| NFR-17 | Engineers are told when their incident is queued | Should | Notification on queue | 100% of queued incidents notify | Burst test | M4 |

### Q4.23 Application logs contain customer information and credentials. How should the platform protect sensitive information?

**Frame.** How do we keep customer data and secrets safe?

**Knowns.**

- Artifact 2: synthetic test cases come first; read-only access with least privilege.
- Artifact 3: logs should never contain passwords, tokens or personal data, but in practice they may.

**Unknowns.** Which personal data and secret formats appear in real logs; the organisation's data-protection rules.

**Assumptions (provisional).**

- Common secret and personal-data patterns can be detected automatically. *Owner: Security lead. Validated when: detection is tested on a seeded corpus.*

**Answer.**

- **Redact on collection:** remove or mask secrets and personal data before storing evidence or sending anything to a model.
- **Never send secrets** to the model provider.
- **Least privilege:** read only the evidence needed, for the affected service and time window.
- **Encrypt** stored evidence, and in transit.
- **Limit retention** of raw evidence (provisional: 30 days); keep the timeline longer.
- **Control access:** only authorised people see an incident's evidence.
- **Audit:** log who accessed what.

**Requirements.**

| **ID** | **Description** | **Priority** | **Measurement** | **Acceptance criterion** | **Validation method** | **Milestone** |
| --- | --- | --- | --- | --- | --- | --- |
| NFR-18 | Secrets and personal data are redacted before storage and before model calls | Must | Seeded secrets found in stored evidence or prompts | 0 of a seeded corpus of 100 | Redaction test | M1 |
| NFR-19 | Evidence is encrypted at rest and in transit | Must | Encryption configuration | Enabled for all stores and connections | Configuration audit | M4 |
| NFR-20 | Raw evidence is deleted after the retention period | Should | Evidence older than the period | 0 | Retention job test | M4 |

### Q4.24 The database is corrupted and the team needs to restore operations. What recovery guarantees should be established?

**Frame.** What recovery promises do we make if the database is corrupted?

**Knowns.** The incident record is the source of truth, and must be complete.

**Unknowns.** The database's backup capabilities.

**Assumptions (provisional).**

- Point-in-time recovery is available. *Owner: Platform owner. Validated when: a restore is tested.*

**Answer.**

- **Recovery point objective: ≤ 5 minutes** of data loss (provisional).
- **Recovery time objective: ≤ 1 hour** to restore (provisional).
- **Backups:** continuous, plus a daily full backup, stored separately from the database.
- **Restore drills:** quarterly, timed against the objectives.
- **After a restore:** open incidents are re-checked against monitoring and the release history before any action resumes.

**Requirements.**

| **ID** | **Description** | **Priority** | **Measurement** | **Acceptance criterion** | **Validation method** | **Milestone** |
| --- | --- | --- | --- | --- | --- | --- |
| NFR-21 | Recovery point objective | Must | Data lost in a restore drill | ≤ 5 minutes (provisional) | Quarterly restore drill | M4 |
| NFR-22 | Recovery time objective | Must | Time to restore service | ≤ 1 hour (provisional) | Quarterly restore drill | M4 |
| NFR-23 | No action resumes after a restore until open incidents are re-checked | Must | Actions before re-check | 0 | Restore drill | M4 |

### Q4.25 A complete platform failure occurs. How long can the organization tolerate the platform remaining unavailable?

**Frame.** How long can the whole platform be down?

**Knowns.** Artifact 1: the platform assists engineers; it doesn't replace them. If it fails, engineers handle incidents manually as today.

**Unknowns.** How dependent teams become on it after adoption.

**Assumptions (provisional).**

- During the pilot, the manual process remains practised and available. *Owner: Engineering manager. Validated when: confirmed with on-call teams.*

**Answer.**

- **Maximum tolerable outage: 4 hours** during the pilot (provisional). Tighten it as adoption grows.
- **Recovery time objective: 1 hour** for normal failures.
- **Its failure must never block manual incident handling:** monitoring alerts still reach engineers directly.
- **Engineers are told** when the platform is down.

**Requirements.**

| **ID** | **Description** | **Priority** | **Measurement** | **Acceptance criterion** | **Validation method** | **Milestone** |
| --- | --- | --- | --- | --- | --- | --- |
| NFR-24 | Full-platform recovery time | Must | Time to restore after a total failure | ≤ 1 hour target; ≤ 4 hours maximum (provisional) | Disaster recovery drill | M4 |
| NFR-25 | Platform failure doesn't stop alerts reaching engineers | Must | Alerts delivered while the platform is down | 100% | Outage test | M4 |

### Q4.26 Engineers cannot determine why investigations are slow. Which performance measurements should the platform expose?

**Frame.** What should the platform measure about its own speed?

**Knowns.** Artifact 3: request durations should be measured as histograms with percentiles; traces follow work across steps.

**Unknowns.** Which steps dominate the time.

**Assumptions (provisional).**

- Each step can be timed and traced with the incident ID. *Owner: Platform owner. Validated when: a full trace of a scenario run is produced.*

**Answer.** Expose, per incident and as percentiles:

- **Queue wait:** time from acceptance to a worker starting.
- **Duration of each step:** evidence collection, diagnosis, patch generation, testing, execution, verification.
- **Per evidence source:** call duration, errors and timeouts.
- **Per model call:** duration, tokens in and out, retries and cost.
- **Human wait time:** time waiting for approval.
- **End-to-end:** time to first summary, to diagnosis, to verified recovery.

All are tagged with the incident ID, and traced across steps.

**Requirements.**

| **ID** | **Description** | **Priority** | **Measurement** | **Acceptance criterion** | **Validation method** | **Milestone** |
| --- | --- | --- | --- | --- | --- | --- |
| NFR-26 | Every step, source and model call is timed and traced with the incident ID | Must | Untimed steps in a full run | 0 | Scenario trace review | M1 |
| NFR-27 | Percentile dashboards or queries exist for each measurement | Should | Measurements available | All listed measurements queryable | Review | M4 |

### Q4.27 An external dependency becomes degraded but is not completely unavailable. How should the platform respond?

**Frame.** What should the platform do when a dependency is slow or partly failing?

**Knowns.** Artifact 2 and Artifact 3: bounded retries; stop calling a failing provider for a while; record gaps; lower confidence; continue with other sources.

**Unknowns.** Typical degradation patterns for each dependency.

**Assumptions (provisional).**

- Timeouts can be set per dependency from measured normal latency. *Owner: Platform owner. Validated when: normal latency is measured.*

**Answer.**

- **Timeouts** per call, set from normal latency.
- **Bounded retries with backoff** (provisional: 2 retries).
- **Circuit breaker:** after repeated failures, stop calling the dependency for a short period, then try again.
- **Mark evidence as partial**, and lower confidence in conclusions that depend on it.
- **Continue with other sources**, and hold actions that depended on the degraded data.
- **Alert** the team about the degradation.

**Requirements.**

| **ID** | **Description** | **Priority** | **Measurement** | **Acceptance criterion** | **Validation method** | **Milestone** |
| --- | --- | --- | --- | --- | --- | --- |
| NFR-28 | Degraded dependencies are bounded by timeouts, limited retries and a circuit breaker | Must | Calls to a failing dependency over time | Calls stop after the breaker trips; investigation still completes | Fault-injection test (slow and erroring) | M4 |
| NFR-29 | Evidence from a degraded source is marked partial, and confidence is lowered | Must | Behaviour on degraded sources | 100% marked; confidence lowered | Fault-injection test | M1 |

### Q4.28 An engineer submits an incident and expects an immediate response. What should the API intake p95 latency target be?

**Frame.** How fast must intake respond?

**Knowns.** Intake only validates, deduplicates, stores and returns an ID. Investigation is asynchronous (Q4.1).

**Assumptions (provisional).**

- A single database write and a lookup complete well under 100 ms. *Owner: Platform owner. Validated when: a load test is run.*

**Answer.** **p95 ≤ 300 ms; p99 ≤ 1 s.**

**Rationale.** Intake does minimal work, so it should feel instant. 300 ms leaves headroom for validation, deduplication and a durable write. A slow intake risks monitoring timing out and retrying, which creates duplicates.

| **ID** | **Description** | **Priority** | **Measurement** | **Acceptance criterion** | **Validation method** | **Milestone** |
| --- | --- | --- | --- | --- | --- | --- |
| NFR-30 | Intake latency | Must | p95 and p99 response time | p95 ≤ 300 ms, p99 ≤ 1 s (provisional) | Load test at 10× expected peak | M4 |

### Q4.29 The incident is accepted, but its processing event takes too long to become available to workers. What should the event publication p95 target be?

**Frame.** How quickly must an accepted incident reach the workers?

**Knowns.** The first summary is due within 5 minutes.

**Assumptions (provisional).**

- The event is written together with the incident record and published from there. *Owner: Platform owner. Validated when: the design in Layer 7 confirms it.*

**Answer.** **p95 ≤ 1 second** from acceptance to the event being available to workers; **p99 ≤ 5 seconds**.

**Rationale.** This delay sits inside the 5-minute summary budget, so it must be negligible. Writing the event with the incident record guarantees it is never lost, even if publishing is briefly delayed.

| **ID** | **Description** | **Priority** | **Measurement** | **Acceptance criterion** | **Validation method** | **Milestone** |
| --- | --- | --- | --- | --- | --- | --- |
| NFR-31 | Event publication latency | Must | Acceptance-to-available time | p95 ≤ 1 s, p99 ≤ 5 s (provisional); 0 lost events | Load and crash test | M4 |

### Q4.30 A model provider occasionally takes several minutes to return a response. What maximum model-call duration should be allowed?

**Frame.** How long may one model call take?

**Knowns.** The full investigation has 15 minutes; the first summary is due in 5 minutes.

**Assumptions (provisional).**

- Normal model calls for this task complete in under 20 seconds. *Owner: SRE lead. Validated when: measured on the test suite.*

**Answer.** **60 seconds per call**, hard timeout. A timed-out call counts as one attempt (Q4.31).

**Rationale.** A few multi-minute calls would consume the whole 15-minute investigation. 60 seconds is three times the expected normal duration, leaving room for slow but healthy responses while protecting the budget.

| **ID** | **Description** | **Priority** | **Measurement** | **Acceptance criterion** | **Validation method** | **Milestone** |
| --- | --- | --- | --- | --- | --- | --- |
| NFR-32 | Model-call timeout | Must | Longest model call | No call exceeds 60 s (provisional) | Slow-provider simulation | M1 |

### Q4.31 A model returns malformed output several times for the same incident. How many model attempts should the platform permit?

**Frame.** How many tries does the model get before we give up?

**Knowns.** Output is checked against a strict structure. Artifact 1: when the platform can't establish a cause, it hands over.

**Assumptions (provisional).**

- Most malformed outputs succeed on a retry with the error explained. *Owner: SRE lead. Validated when: retry success is measured.*

**Answer.** **3 attempts per step** (the original plus 2 retries). Each retry includes the validation error. After 3 failures, the step fails and the investigation hands over with an inconclusive result (Q4.8). All attempts count against the per-incident cap (Q4.21).

**Rationale.** One retry fixes most formatting problems, and two cover a transient error. More attempts rarely help, and they burn time and budget.

| **ID** | **Description** | **Priority** | **Measurement** | **Acceptance criterion** | **Validation method** | **Milestone** |
| --- | --- | --- | --- | --- | --- | --- |
| NFR-33 | Model attempts per step | Must | Attempts before failure | ≤ 3; then inconclusive and handed over | Malformed-output simulation | M1 |

### Q4.32 An approval request remains unanswered for several days. How long should an approval remain valid?

**Frame.** How long may an approval request, or an approval, stay valid?

**Knowns.** Artifact 1: approval is required, and recorded. Incidents are urgent.

**Assumptions (provisional).**

- Approvers respond within 15 minutes during an incident. *Owner: Engineering manager. Validated when: measured during the pilot.*

**Answer.**

- **An unanswered request** is escalated to the next approver after **15 minutes**, and expires after **60 minutes**.
- **A granted approval** must be executed within **15 minutes**, or it expires.
- **Any approval is void** if the service's version, configuration or the patch changes before execution.

**Rationale.** Production changes quickly during an incident. An approval given for one state of the service is unsafe hours later, so short validity forces a fresh check.

| **ID** | **Description** | **Priority** | **Measurement** | **Acceptance criterion** | **Validation method** | **Milestone** |
| --- | --- | --- | --- | --- | --- | --- |
| NFR-34 | Approval request expiry and escalation | Must | Escalation and expiry times | Escalates at 15 min; expires at 60 min (provisional) | Timer test | M3 |
| NFR-35 | A granted approval expires, or is voided by a change | Must | Executions after expiry or a change | 0 | State-change test | M3 |

### Q4.33 An investigation generates excessive input and output tokens. How should cumulative token limits be established?

**Frame.** How do we set the total token limits for one incident?

**Knowns.** Artifact 2: the per-incident model budget is a small fraction of the engineer time saved.

**Assumptions (provisional).**

- A change-caused investigation needs under 150,000 input and 15,000 output tokens. *Owner: SRE lead. Validated when: measured on the test suite.*

**Answer.**

1. **Derive the limit from the budget:** the per-incident budget divided by the provider's price per token.
2. **Set provisional caps:**
    - **200,000 input tokens and 20,000 output tokens per incident**
    - **16,000 input tokens per call**
3. **Enforce before each call:** check the remaining budget, and trim or summarise evidence to fit.
4. **On the cap:** stop and hand over.
5. **Review after the pilot**, from measured usage.

**Rationale.** Tying tokens to the money budget keeps them consistent with Artifact 2. The per-call cap stops one oversized prompt using the whole budget.

| **ID** | **Description** | **Priority** | **Measurement** | **Acceptance criterion** | **Validation method** | **Milestone** |
| --- | --- | --- | --- | --- | --- | --- |
| NFR-36 | Cumulative token limits per incident | Must | Tokens used per incident | ≤ 200k input, ≤ 20k output (provisional); hand over at cap | Large-evidence test | M1 |
| NFR-37 | Per-call input limit | Must | Largest prompt | ≤ 16k tokens (provisional) | Test | M1 |

### Q4.34 The system receives more incidents than its workers can handle. What maximum concurrent investigation count should be enforced?

**Frame.** How many investigations may run at once?

**Knowns.** Artifact 2: a provisional limit of 5, sized as twice the historical peak; the rest queue by severity; nothing is dropped.

**Assumptions (provisional).**

- Historical peak concurrency is 2 or fewer. *Owner: SRE lead. Validated when: past records are checked.*

**Answer.** **5 concurrent investigations; queue up to 50**, ordered by severity. Beyond 50 queued, new incidents are still recorded, and the incident lead is alerted.

**Rationale.** Five is double the assumed peak, and fits the spend limits (5 × the per-incident budget). Grouping related alerts keeps real concurrency low.

| **ID** | **Description** | **Priority** | **Measurement** | **Acceptance criterion** | **Validation method** | **Milestone** |
| --- | --- | --- | --- | --- | --- | --- |
| NFR-38 | Maximum concurrent investigations | Must | Concurrent running investigations | Never > 5 (provisional) | Burst test | M4 |
| NFR-39 | Queue overflow alerts the incident lead and still records incidents | Must | Behaviour above 50 queued | Recorded and alerted | Burst test with 60 incidents | M4 |
