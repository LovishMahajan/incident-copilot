# Artifact 2: Scope and Constraints Document

- **Layer:** 2, Scope and Constraints
- **Built from:** Artifact 1
- **Status:** Draft compiled from the team's answers; pending team review
- **Sources:** [conversation.md](conversation.md), [System Design](../../reference/system-design.md), [Technical specification](../../reference/technical-specification.md), and, for assumption owners and later refinements, the conversations of [Layer 1](../layer-01-problem-discovery/conversation.md), [Layer 3](../layer-03-sre-fundamentals/conversation.md), [Layer 4](../layer-04-requirements/conversation.md), [Layer 6](../layer-06-system-architecture/conversation.md), [Layer 7](../layer-07-data-apis-events/conversation.md), [Layer 9](../layer-09-agent-architecture/conversation.md), [Layer 11](../layer-11-safe-remediation/conversation.md) and [Layer 12](../layer-12-reliability-evaluation/conversation.md)

## Required sections

| Required section | Questions this section answers | Done |
| --- | --- | --- |
| 2.1 Supported environments | Q2.1, Q2.3, Q2.7 | Partial |
| 2.2 Supported application types | Q2.2 | Yes |
| 2.3 Deployment boundaries | Q2.4–Q2.6, Q2.8–Q2.10, Q2.14 | Yes |
| 2.4 Budget constraints | Q2.15–Q2.18 | Partial |
| 2.5 Provider assumptions | Q2.11, Q2.12, Q2.19, Q2.22 | Partial |
| 2.6 Explicit non-goals | Q2.1, Q2.9–Q2.13 | Yes |
| 2.7 Unresolved dependencies | Q2.20–Q2.23 | Partial |

## 2.1 Supported environments

*Answers Q2.1, Q2.3, Q2.7.*

**A2-a:** One organisation; Python applications only; one deployment environment. *(Q2.1–Q2.3)*

| Dimension | First version | Later |
| --- | --- | --- |
| Organisations | **One organisation**, used by several of its teams. Teams share the platform, but each team sees and acts only on its own services ([Q2.1](conversation.md)) | Several independent organisations are a non-goal; see 2.6 |
| Deployment environment | **One environment**: wherever the reference application runs ([Q2.3](conversation.md)) | Reached only through clearly defined connection points (for evidence, changes and fixes), so a second environment can be added later without redesign |
| Data the platform runs on | **Synthetic test cases first**, based on the reference scenario: built, cleaned failures with a known cause and known correct fix ([Q2.7](conversation.md)) | In stages, below |

Real incidents come later, in this order ([Q2.7](conversation.md)):

1. Replay past incidents with sensitive data removed.
2. Advisory mode on live incidents: the platform investigates and recommends, but takes no action.
3. Approved fixes on live incidents.

**Gap:** the sources do not say which environment the reference application runs in (cloud or private infrastructure). Q2.3 fixes it only as "wherever the reference application runs", and Q2.9 leaves open whether that application runs on Kubernetes.

## 2.2 Supported application types

*Answers Q2.2.*

| Constraint | Value |
| --- | --- |
| Language | **Python only**, the language of the reference scenario ([Q2.2](conversation.md)) |
| Evidence that doesn't depend on language | Errors, health signals, release and configuration history: designed so other languages can be added later |
| Failure type | Failures caused by a recent release or configuration change ([Q1.16](../layer-01-problem-discovery/conversation.md)); in the consolidated design, change-caused failures in Python web applications ([System Design, 10](../../reference/system-design.md)) |

Each extra language multiplies the error formats, the fixes and the tests needed. For change-caused failures, the key evidence is when the change happened and what it changed, which is largely the same whatever the language ([Q2.2](conversation.md)).

## 2.3 Deployment boundaries

*Answers Q2.4–Q2.6, Q2.8–Q2.10, Q2.14.*

**A2-b:** Evidence is read from existing monitoring with read-only access; alerts come from existing monitoring. *(Q2.4–Q2.6)*

**A2-c:** Fixes are tested in a disposable, isolated environment; synthetic test cases come first. *(Q2.7, Q2.14)*

### Fit with existing monitoring

- **Don't replace it.** Existing monitoring keeps detecting incidents.
- **Start from its alerts.** The platform starts work when it receives an alert from existing monitoring.
- **Read, don't own.** It reads evidence from existing sources with read-only access, and doesn't copy everything into a new store.
- **Keep the record.** It saves only the evidence it used, in the incident record.

([Q2.4](conversation.md))

### Access to establish

| Access | Level | Scope |
| --- | --- | --- |
| Alerts from existing monitoring | Receive | Services in scope |
| Error records | Read-only | Services in scope, recent window only |
| Health signals | Read-only | Services in scope |
| Request traces (if collected) | Read-only | Services in scope |
| Release and configuration history | Read-only | Services in scope |
| Service ownership and on-call | Read-only | All services in scope |
| Notifications to engineers | Send | Owners, on-call, change authors |
| Undoing a change | Write, **only after approval**, and only the specific change | Affected service only |

Every access uses its own named identity, with the least permission needed. It is approved by the data owner and logged ([Q2.5](conversation.md)).

### Releases and source

- **Release history:** read the record of releases, with each one's time, author, version and the configuration it changed.
- **Source code:** read-only access, used to compare the failing version with the previous one, with a focus on configuration changes.
- **Undoing a change:** only through the organisation's existing release process, after approval. The platform never edits source code or history directly.

([Q2.6](conversation.md))

### Interfaces

The first version works with only a command-line tool and an HTTP API ([Q2.8](conversation.md)).

| Interface | Does |
| --- | --- |
| HTTP API | Receives alerts and exposes the incident record, approvals and verification results |
| Command-line tool | Lets engineers view an incident, see evidence and timeline, approve or reject a fix, and accept recovery |
| Notifications | Carry a short summary and a link or command for the approval step |

### Platform shape

| Decision | Value | Revisit |
| --- | --- | --- |
| Hosting | No Kubernetes for the first version. Run on the simplest setup that restarts the platform if it fails, packaged so it could move to Kubernetes later ([Q2.9](conversation.md)) | When concurrent incidents, the number of services, or team count make scaling and scheduling a real problem |
| Structure | No microservices. One deployable application with clear internal modules: intake, evidence, investigation, remediation, verification and record ([Q2.10](conversation.md)) | Split a module out only when it has a different scaling, security or release need. Likely first candidate: the sandbox that runs fixes |
| Reference application on Kubernetes | If it runs there, the platform still has to read its releases and health. That is integration, not hosting the platform on Kubernetes ([Q2.9](conversation.md)) | |

### Disposable execution

1. **Build a copy:** a fresh, short-lived copy of the affected service at the failing version, with the reference failure reproduced.
2. **Isolate it:** no access to production systems, production data or real credentials; no outbound network except what the test needs; strict time and resource limits.
3. **Apply and test the fix there:** run the original symptom check (errors gone, user-facing checks passing).
4. **Destroy it:** delete the copy after the test, and keep only the results in the incident record.
5. **Production is still blocked:** the platform's production write permission stays off. A person applies the fix through the normal release process, or (later) approves it for the platform to apply.

| Phase | Production boundary |
| --- | --- |
| First version | Fixes run in the disposable copy only; people apply any production change |
| Later | Approved fixes applied to production by the platform, gated as in Artifact 1, 1.5 |

([Q2.14](conversation.md))

## 2.4 Budget constraints

*Answers Q2.15–Q2.18.*

**A2-d:** First summary within 5 minutes; full investigation within 15 minutes; a model-call budget per incident. *(Q2.16, Q2.18)*

### Infrastructure ([Q2.15](conversation.md))

- A **fixed monthly ceiling**, with alerts at 50%, 80% and 100%.
- The **simplest hosting**: one deployable application, no Kubernetes, no GPUs.
- **Disposable copies are short-lived:** destroyed after each test, with a cap on how many run at once.
- **Don't duplicate data:** read evidence from existing monitoring; store only what the incident record needs.
- **Retention limits** on stored evidence.
- **Monthly review**, recording cost per incident.

### Model spend per incident ([Q2.16](conversation.md))

1. **Start from value:** the engineer time saved per incident, multiplied by an hourly cost, is the most the platform can be worth per incident.
2. **Set the budget as a small fraction of that value.** The starting fraction is provisional, at 10%.
3. **Enforce it:** a maximum number of model calls and tokens per incident. When the budget is reached, stop and hand over with the findings so far.
4. **Measure it:** record the actual cost of every incident, and adjust the budget after the pilot.
5. **Save calls:** gather and line up evidence in code, and call the model only for reasoning.

### Concurrency ([Q2.17](conversation.md))

- **Size from history:** take the highest number of simultaneous incidents seen in past records, and double it for headroom. The provisional starting point is 5 concurrent investigations.
- **Group related alerts:** alerts that share a start time and a likely cause become one incident, not many.
- **Queue the rest by severity:** beyond the limit, incidents wait in a queue ordered by severity, and engineers are told they're queued.
- **Never drop an alert:** every alert is recorded, even if its investigation waits.

### Time ([Q2.18](conversation.md))

| Milestone | Provisional limit |
| --- | --- |
| First summary to the on-call engineer | 5 minutes after the alert |
| Full investigation, with ranked causes | 15 minutes |
| Each evidence or model step | Its own short timeout, so one slow source can't block the rest |

When a limit is reached, the platform stops, shares what it has found and what is missing, and hands over to a person.

### Values set after this layer

Layer 2 sets the rules above. Later layers supply these provisional values ([System Design, 9](../../reference/system-design.md), [Q4.34](../layer-04-requirements/conversation.md)):

| Limit | Value (provisional) |
| --- | --- |
| Model calls per incident | 30 |
| Tokens per incident | 200,000 input, 20,000 output |
| Tokens per call | 16,000 input |
| Time per model call | 60 s |
| Concurrent investigations | 5; queue up to 50 by severity. Beyond 50 queued, new incidents are still recorded, and the incident lead is alerted |
| Daily spend, all incidents | Monthly budget ÷ 30; alert at 80%, evidence-only at 100% |
| Retention | Raw evidence and model input and output 30 days; timeline and audit 1 year ([Technical specification, 10](../../reference/technical-specification.md)) |

**Gap:** no source gives an amount for the fixed monthly ceiling, the hourly cost or the engineer time saved per incident, so the per-incident budget has no monetary value yet.

**Gap:** no source gives the cap on how many disposable copies run at once (Q2.15).

## 2.5 Provider assumptions

*Answers Q2.11, Q2.12, Q2.19, Q2.22.*

Assumption IDs are defined under [Assumptions](#assumptions). The two provider assumptions the design depends on ([Q2.22](conversation.md)):

| ID | Assumption | If it's wrong |
| --- | --- | --- |
| A1 | A capable model is available and affordable | The investigation core needs a different approach |
| A2 | Incident data may be sent to an outside provider | Self-hosting needed (Q2.12) |

### Model and hosting ([Q2.11](conversation.md), [Q2.12](conversation.md))

Use an existing hosted model with usage caps, give it gathered evidence, and measure it.

| Option | Decision | What would justify it |
| --- | --- | --- |
| Train or fine-tune our own model | Not for the first version. Revisit when the evaluation data shows the ceiling has been reached | **All** of: a proven ceiling on our evaluation set; enough labelled incidents with confirmed causes and fixes; a narrow, stable task; a hard requirement existing models can't meet, on cost, speed, or data that cannot leave the organisation |
| Operate our own GPUs | No | **Any one** of: sustained volume where owning hardware is cheaper at measured utilisation; a rule that incident data must never leave the organisation's infrastructure, which no provider can meet contractually; response times a hosted provider cannot meet; a required self-hosted or custom model |

Recorded triggers for revisiting GPUs: monthly model spend above a set threshold, or a data-residency rule that a provider cannot meet.

### Provider outage ([Q2.19](conversation.md))

1. **Keep doing everything that doesn't need the model:** receive the alert, open the record, notify people, gather evidence, build the timeline, and list recent changes around the start time.
2. **Retry briefly**, then stop calling the provider for a short period, so it doesn't pile up failing calls.
3. **Tell people plainly** that the analysis is unavailable, and share the evidence and timeline.
4. **Never propose a fix without the model's reasoning.** People decide.
5. **Resume the analysis** automatically when the provider returns, if the incident is still open.

A second provider can be considered later, if outages prove frequent.

**Gap:** no source sets the "set threshold" for monthly model spend (Q2.12), or says how frequent outages must be before a second provider is considered (Q2.19).

## 2.6 Explicit non-goals

*Answers Q2.1, Q2.9–Q2.13.*

| Not in the first version | Source | What would reopen it |
| --- | --- | --- |
| Serving several independent organisations | [Q2.1](conversation.md), [Q2.13](conversation.md) | See multi-tenant requirements below |
| Replacing existing monitoring, or detecting incidents | [Q2.4](conversation.md) | |
| Languages other than Python | [Q2.2](conversation.md) | Language-independent evidence is designed so others can be added |
| Both cloud and private infrastructure | [Q2.3](conversation.md) | Deferred; a second environment is added behind the connection points |
| A sophisticated dashboard | [Q2.8](conversation.md) | Can be added later without changing the core, because the API is built first |
| Kubernetes for the platform | [Q2.9](conversation.md) | See 2.3 |
| Microservices | [Q2.10](conversation.md) | See 2.3 |
| A custom or fine-tuned model | [Q2.11](conversation.md) | See 2.5 |
| Operating GPUs | [Q2.12](conversation.md) | See 2.5 |
| The platform writing to production | [Q2.14](conversation.md) | Later phase; see 2.3 and the first conflict under Decisions and conflicts |
| Other kinds of incident, many services failing at once, a polished interface | [Q1.16](../layer-01-problem-discovery/conversation.md) | Come later |

### Multi-tenant requirements, if several organisations are served later ([Q2.13](conversation.md))

- **Data isolation:** each organisation's evidence, records and settings are separated and can never be seen by another.
- **Identity and access:** separate users, roles and approvers per organisation; no shared admin powers across them.
- **Separate credentials:** each organisation's access keys to its monitoring and code are stored and used separately.
- **Execution isolation:** sandboxes and fixes for one organisation can never reach another's systems.
- **Fair use:** per-organisation limits on load and model spend, so one can't starve others.
- **Model-provider handling:** one organisation's data never appears in another's prompts or cache.
- **Audit and deletion:** per-organisation audit trails, retention rules and full deletion on exit.
- **Compliance:** contracts and data-processing terms per organisation; possibly data kept in specific regions.
- **Separate operations:** per-organisation configuration, upgrades and incident communication.

**Decision.** Out of scope for the first version. Design one small thing now: tag every record with an organisation identifier, so isolation can be enforced later without migrating data.

## 2.7 Unresolved dependencies

*Answers Q2.20–Q2.23.*

### Requirement labels ([Q2.20](conversation.md))

| Type | Definition | Example |
| --- | --- | --- |
| **Fixed** | Given by the scenario, a stakeholder, a rule or a regulation | Production changes need a person's approval |
| **Decision** | Chosen by the team, with a rationale | One organisation first |
| **Assumption** | Believed without evidence yet | Recent changes cause most incidents |

Each assumption gets an owner, a validation condition, a deadline, and a note of what breaks if it's wrong.

### Assumptions that could invalidate the design ([Q2.22](conversation.md))

Each needs an early test and a fallback plan.

| ID | Assumption | If it's wrong |
| --- | --- | --- |
| A1 | A capable model is available and affordable | The investigation core needs a different approach |
| A2 | Incident data may be sent to an outside provider | Self-hosting needed (Q2.12) |
| A3 | Evidence sources are accessible read-only | No investigation is possible |
| A4 | Releases and configuration changes are recorded with times and authors | The cause can't be linked to a change |
| A5 | A disposable copy can reproduce the failure | Fixes can't be tested safely |
| A6 | Incident volume and concurrency stay small | Scaling design needed (Q2.9, Q2.10) |
| A7 | One organisation only | Multi-tenant work needed (Q2.13) |
| A8 | An approver can be reached at night | Approval flow must change |

### Dependencies to settle

These follow from the Layer 2 answers; the owners and conditions come from the consolidated design ([System Design, 10 and 12](../../reference/system-design.md)).

| Dependency | From | Owner (role) | Settled when |
| --- | --- | --- | --- |
| Named identities and data-owner approval for each access in 2.3 | [Q2.5](conversation.md) | TBD | TBD |
| Release process can apply, undo and look up one change by key | [Q2.6](conversation.md) | Platform owner | Scenario revert done through it |
| On-call rotas cover every in-scope service, including nights | A8 | Engineering manager | Rotas confirmed |
| Organisation's approval rules by service and severity | A8 | Engineering manager | Rules written down |
| Historical peak concurrency | [Q2.17](conversation.md), A6 | SRE lead | Past incident records checked |
| Provider agreement on data retention | A2 | Security lead | Terms confirmed |

### Decision log format ([Q2.23](conversation.md))

Keep one **decision log** in the team repository, one entry per decision. Review open entries at every layer boundary.

| Field | Content |
| --- | --- |
| ID and title | e.g. D-04: no Kubernetes for the first version |
| Status | Proposed, accepted, superseded or reversed |
| Decision | What was decided |
| Rationale | Why, and which alternatives were rejected |
| Assumptions it rests on | Linked assumption IDs (A1, A2…) |
| Owner | A named person |
| Validation condition | What evidence confirms it, and by when |
| Revisit trigger | What would reopen it |
| Reversal cost | How hard it is to undo |
| Date and links | Which conversation and artifact it came from |

### A decision and the assumption behind it ([Q2.21](conversation.md))

Recorded as two linked entries. Under Layer 2's focus, the decision is about the *kind* of store; the specific product stays provisional.

| Entry | Content |
| --- | --- |
| Decision | Use a single relational database for the incident record. *Rationale:* strong consistency, ordered records, and a familiar operations model. *Status:* provisional |
| Assumption it rests on (A9) | Data volume stays within what one database handles comfortably. *Owner:* platform owner. *Validated when:* volume is measured during the pilot. *If false:* revisit storage for evidence (for example, store bulky evidence separately and keep the record relational) |

The entry also notes the reversal cost and the trigger for revisiting.

**Gap:** the repository has no decision log file, and no answer assigns entries, deadlines or named people. Q2.20 requires a deadline per assumption and Q2.23 a named owner per decision; the sources give neither. The Q2.23 reasoning also stops mid-sentence ("It also feeds the "unresolved dependencies""), so how the log feeds this section isn't stated.

## Assumptions

Layer 2's answers have no per-question assumption lists, and Q2.23 names the ID form (A1, A2…) without numbering any assumption. IDs here follow source order: Q2.22 (A1–A8), Q2.21 (A9), the Q2.20 example (A10), then the values the team labelled provisional (A11–A13). Owners and conditions come from the assumption's own answer, or from a later answer that states the same assumption, cited in the row.

These IDs are separate from the shared-known labels. **A1** is an assumption; **A1-a** is a shared known from Artifact 1. **A2** is an assumption; **A2-a** to **A2-d** are the labels defined in 2.1, 2.3 and 2.4.

| Assumption | Status | Owner | Validated when |
| --- | --- | --- | --- |
| **A1:** A capable model is available and affordable | Provisional | TBD | TBD (Q2.11 says measure it on the evaluation set; no condition is set) |
| **A2:** Incident data may be sent to an outside provider | Provisional | Security lead ([System Design, 10](../../reference/system-design.md)) | Provider terms on data retention are confirmed |
| **A3:** Evidence sources are accessible read-only | Provisional | Security lead ([Q1.22](../layer-01-problem-discovery/conversation.md)) | The platform's access is confirmed to be read-only |
| **A4:** Releases and configuration changes are recorded with times and authors | Provisional | Platform owner ([Q3.7](../layer-03-sre-fundamentals/conversation.md), [Q3.26](../layer-03-sre-fundamentals/conversation.md)) | The release record shows configuration differences per release, and the release process emits a record for each deployment |
| **A5:** A disposable copy can reproduce the failure | Provisional | Platform owner ([Q4.10](../layer-04-requirements/conversation.md)) | The HTTP 500 errors reproduce there |
| **A6:** Incident volume and concurrency stay small | Provisional | SRE lead ([Q4.34](../layer-04-requirements/conversation.md)) | Past records are checked |
| **A7:** One organisation only | Provisional | TBD | TBD |
| **A8:** An approver can be reached at night | Provisional | Engineering manager ([Q1.22](../layer-01-problem-discovery/conversation.md)) | The on-call rota covers every service in scope |
| **A9:** Data volume stays within what one database handles comfortably | Provisional | Platform owner | Volume is measured during the pilot |
| **A10:** Recent changes cause most incidents | Provisional | TBD | TBD |
| **A11:** Model spend at 10% of the value of engineer time saved is the right starting fraction | Provisional | TBD | The actual cost of every incident is recorded, and the budget is adjusted after the pilot |
| **A12:** 5 concurrent investigations is enough | Provisional | SRE lead ([Q4.34](../layer-04-requirements/conversation.md)) | Past records are checked |
| **A13:** The 5-minute and 15-minute time limits are right | Provisional | TBD | TBD (the first-summary target is measured on the test suite, then the pilot, per [System Design, 10](../../reference/system-design.md)) |

## Decisions and conflicts

**Decisions.** Every decision in this layer appears in a section above. Q2.13's organisation identifier is in 2.6; Q2.21's relational-store decision is in 2.7.

**Conflicts found while compiling.** None of these was resolved here. Where the last column names a later value, the consolidated sources state it; the rest need a team decision.

| # | Layer 2 says | Later or other source says | Status |
| --- | --- | --- | --- |
| 1 | Production write permission stays off in the first version; people apply any production change ([Q2.14](conversation.md)). But [Q2.5](conversation.md) lists a write access "only after approval" among the accesses to establish | The trusted dispatcher applies approved changes from M3. Applying approved fixes is switched on only after a recorded decision, after an advisory pilot ([System Design, 1 and 12](../../reference/system-design.md), [Technical specification, 12](../../reference/technical-specification.md)) | Later value: built at M3, off until the recorded decision. "First version" needs one meaning: the two sources draw it at different points |
| 2 | **Resume the analysis** automatically when the provider returns, if the incident is still open ([Q2.19](conversation.md)) | The incident becomes inconclusive and is handed over ([Q12.27](../layer-12-reliability-evaluation/conversation.md)). The allowed transitions have no path back to investigating ([Technical specification, 2](../../reference/technical-specification.md)) | Unresolved: team to decide whether the resume step stays |
| 3 | One deployable application ([Q2.10](conversation.md)) | One codebase run as four processes ([System Design, 2](../../reference/system-design.md)), "a modular monolith, deployed as a small number of processes" ([Q6.7](../layer-06-system-architecture/conversation.md)) | Later value: four processes |
| 4 | No outbound network except what the test needs ([Q2.14](conversation.md)) | No network by default; a package mirror only if ever needed ([Q11.21](../layer-11-safe-remediation/conversation.md), [System Design, 11](../../reference/system-design.md)) | Later value: no network |
| 5 | Saves only the evidence it used ([Q2.4](conversation.md)); store only what the incident record needs ([Q2.15](conversation.md)) | Collected results are normalised, fingerprinted and snapshotted; raw evidence is kept 30 days ([Technical specification, 5.1 and 10](../../reference/technical-specification.md), [Q7.17](../layer-07-data-apis-events/conversation.md)) | Unresolved: team to confirm whether snapshots cover all collected evidence or only what was used |
| 6 | Source code is read-only; the platform never edits source code directly; the access table has no write to a review system ([Q2.5](conversation.md), [Q2.6](conversation.md)) | The platform sends a proposed change to the review system, with no direct commits ([Technical specification, 8](../../reference/technical-specification.md)); `draft_patch` creates a draft there ([Q9.18](../layer-09-agent-architecture/conversation.md)) | Unresolved: the access in 2.3 omits this one. The direct-edit rule still holds |
| 7 | Kubernetes revisit trigger: concurrent incidents, the number of services, or team count ([Q2.9](conversation.md)) | Several environments or organisations, or more processes than simple hosting can manage ([Q6.33](../layer-06-system-architecture/conversation.md)) | Unresolved: two different triggers |
| 8 | The Q2.21 fallback, bulky evidence stored separately, applies only if assumption A9 proves false | A write-once object store for large items is in the design from the start ([System Design, 2](../../reference/system-design.md), [Q7.17](../layer-07-data-apis-events/conversation.md)) | Later value: separate store from the start. A9 still stands for the record itself |
| 9 | 5 minutes is a limit, and reaching a limit stops the platform ([Q2.18](conversation.md)) | First summary is a target of ≤ 5 min p95; only the 15-minute investigation limit stops and hands over ([System Design, 1 and 9](../../reference/system-design.md)) | Later value: p95 target |
| 10 | Production approval is the Q2.20 example of **Fixed** | The team may later let specific low-risk, reversible actions run without approval, each as a logged decision ([Q1.22](../layer-01-problem-discovery/conversation.md), [System Design, 8](../../reference/system-design.md)) | Unresolved: a Fixed item that a team decision can relax fits the Q2.20 labels poorly |
| 11 | Decision IDs of the form D-04 in one log ([Q2.23](conversation.md)) | Later layers use ADR-01 onward, with no D-numbered entries; the only Kubernetes record is the Q6.33 table, and it has no ADR number | Unresolved: team to choose one scheme; no log exists yet (see 2.7) |

**Label wording.** Layer 3 states A2-c once as "changes can be tested in a disposable environment" ([Q3.7](../layer-03-sre-fundamentals/conversation.md)) and elsewhere as the wording kept in 2.3.

## Acceptance checks

- [ ] Every required section above is present and complete.
- [x] Every section lists the questions it answers, and every question in this layer is answered by at least one section.
- [ ] Every assumption is marked provisional and has a named owner and a condition under which it will be validated.
- [ ] Every disagreement between Claude and ChatGPT that affected a decision is recorded with the team's resolution.
- [ ] The artifact is consistent with every artifact it is built from; any conflict is recorded as an explicit decision.
- [x] The artifact was produced with the team's coding agent and is saved in the team repository.

Reviewed by: TBD
Date: TBD
