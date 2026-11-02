# Artifact 2: Scope and Constraints Document

- **Layer:** 2, Scope and Constraints
- **Built from:** Artifact 1
- **Status:** Proposed: gaps resolved 2026-11-02 per [decisions.md](../../decisions.md); pending team sign-off
- **Sources:** [conversation.md](conversation.md), [decisions.md](../../decisions.md), [System Design](../../reference/system-design.md), [Technical specification](../../reference/technical-specification.md), and, for assumption owners and later refinements, the conversations of [Layer 1](../layer-01-problem-discovery/conversation.md), [Layer 3](../layer-03-sre-fundamentals/conversation.md), [Layer 4](../layer-04-requirements/conversation.md), [Layer 6](../layer-06-system-architecture/conversation.md), [Layer 7](../layer-07-data-apis-events/conversation.md), [Layer 9](../layer-09-agent-architecture/conversation.md), [Layer 11](../layer-11-safe-remediation/conversation.md) and [Layer 12](../layer-12-reliability-evaluation/conversation.md)

## Required sections

| Required section | Questions this section answers | Done |
| --- | --- | --- |
| 2.1 Supported environments | Q2.1, Q2.3, Q2.7 | Yes |
| 2.2 Supported application types | Q2.2 | Yes |
| 2.3 Deployment boundaries | Q2.4–Q2.6, Q2.8–Q2.10, Q2.14 | Yes |
| 2.4 Budget constraints | Q2.15–Q2.18 | Partial |
| 2.5 Provider assumptions | Q2.11, Q2.12, Q2.19, Q2.22 | Yes |
| 2.6 Explicit non-goals | Q2.1, Q2.9–Q2.13 | Yes |
| 2.7 Unresolved dependencies | Q2.20–Q2.23 | Partial |

## 2.1 Supported environments

*Answers Q2.1, Q2.3, Q2.7.*

**A2-a:** One organisation; Python applications only; one deployment environment. *(Q2.1–Q2.3)*

**First version** means everything built through M4, shipped in two releases: Release A (Advisory) and Release B (Controlled remediation) (ADR-44).

| Dimension | First version | Later |
| --- | --- | --- |
| Organisations | **One organisation**, used by several of its teams. Teams share the platform, but each team sees and acts only on its own services ([Q2.1](conversation.md)) | Several independent organisations are a non-goal; see 2.6 |
| Deployment environment | **One environment**: one Linux host under Docker Compose, running the reference application and the platform. The test environment runs on a separate host (ADR-51). The design depends only on that host, so it can sit in a cloud or on private infrastructure | Reached only through clearly defined connection points (for evidence, changes and fixes), so a second environment can be added later without redesign |
| Data the platform runs on | **Synthetic test cases first**, based on the reference scenario: built, cleaned failures with a known cause and known correct fix ([Q2.7](conversation.md)) | In stages, below |

Real incidents come later, in this order ([Q2.7](conversation.md)):

1. Replay past incidents with sensitive data removed.
2. Advisory mode on live incidents: the platform investigates and recommends, but takes no action (Release A).
3. Approved fixes on live incidents (Release B).

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
- **Keep the record.** Everything collected is snapshotted for 30 days. Only the evidence a diagnosis cited is kept long-term, in the incident record, for 1 year. Bulky items go to the object store from the start (ADR-57).

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
| Undoing a change | Write, **only after approval**, and only the specific change. Release B only: in Release A the credential isn't provisioned at all (ADR-44) | Affected service only |
| Draft pull requests in the review system | Write, draft only, in both releases; no direct commits (ADR-44). The worker holds this credential and no other write credential (ADR-51) | Services in scope |

Every access uses its own named identity, with the least permission needed. It is approved by the data owner and logged ([Q2.5](conversation.md)).

### Releases and source

- **Release history:** read the record of releases, with each one's time, author, version and the configuration it changed.
- **Source code:** read-only access, used to compare the failing version with the previous one, with a focus on configuration changes.
- **Draft pull requests:** the platform may open a draft in the review system, in both releases. Direct commits never happen (ADR-44).
- **Undoing a change:** only through the organisation's existing release process, after approval, in Release B. The platform never edits source code or history directly.

([Q2.6](conversation.md))

### Interfaces

The first version works with only a command-line tool and an HTTP API ([Q2.8](conversation.md)).

| Interface | Does |
| --- | --- |
| HTTP API | Receives alerts and exposes the incident record, approvals and verification results |
| Command-line tool | Lets engineers view an incident, see evidence and timeline, approve or reject a fix, and accept recovery |
| Notifications | Carry a short summary and a link or command for the approval step |

The approval steps run against production only in Release B. The Approval Service, dispatcher and Verifier are built and tested at M3 on synthetic targets (ADR-44).

### Platform shape

| Decision | Value | Revisit |
| --- | --- | --- |
| Hosting | No Kubernetes for the first version. Run on the simplest setup that restarts the platform if it fails, Docker Compose on one Linux host, packaged so it could move to Kubernetes later ([Q2.9](conversation.md), ADR-51) | Several environments or organisations in scope, more processes than simple hosting can manage, more than 20 services, or sustained concurrency above 5 (ADR-51) |
| Structure | No microservices. One codebase with clear internal modules (intake, evidence, investigation, remediation, verification and record), run as four processes: API, worker, dispatcher and isolated test environment ([Q2.10](conversation.md), ADR-51) | Split a further module out only when it has a different scaling, security or release need |
| Reference application on Kubernetes | It doesn't run there: it runs under Docker Compose (ADR-51). If a later target runs on Kubernetes, the platform still has to read its releases and health. That is integration, not hosting the platform on Kubernetes ([Q2.9](conversation.md)) | |

### Disposable execution

1. **Build a copy:** a fresh, short-lived copy of the affected service at the failing version, with the reference failure reproduced.
2. **Isolate it:** no access to production systems, production data or real credentials; no network at all (ADR-60); strict time and resource limits. Patches are limited to configuration and reverts, so nothing new needs installing. A package mirror would take a new ADR, a filtering proxy and Security lead sign-off, and none is planned.
3. **Apply and test the fix there:** run the original symptom check (errors gone, user-facing checks passing).
4. **Destroy it:** delete the copy after the test, and keep only the results in the incident record.
5. **Production is still blocked:** in Release A the production write credential isn't provisioned, and a person applies the fix by hand through the normal release process. In Release B the dispatcher applies it, on a signed approval (ADR-44).

| Phase | Production boundary |
| --- | --- |
| Release A: Advisory | Fixes run in the disposable copy only; people apply any production change. The incident ends `testing` → `handed_over` with the tested recommendation (ADR-44) |
| Release B: Controlled remediation | Approved fixes applied to production by the dispatcher, on a signed approval, gated as in Artifact 1, 1.5. Entry: M3 and M4 gates, then the pilot accuracy and approval review. The write credential is provisioned by a recorded decision (ADR-44) |

([Q2.14](conversation.md))

## 2.4 Budget constraints

*Answers Q2.15–Q2.18.*

**A2-d:** First summary within 5 minutes; full investigation within 15 minutes; a model-call budget per incident. *(Q2.16, Q2.18)*

### Infrastructure ([Q2.15](conversation.md))

- A **fixed monthly model spend ceiling** of $500 (provisional, ADR-56), with alerts at 50%, 80% and 100%. Infrastructure cost is separate.
- The **simplest hosting**: one codebase in four processes on simple hosts (ADR-51), no Kubernetes, no GPUs.
- **Disposable copies are short-lived:** destroyed after each test, with a cap of 3 running at once and the rest queued (provisional, ADR-56).
- **Don't duplicate data:** read evidence from existing monitoring; long-term, keep only what the incident record needs (ADR-57).
- **Retention limits** on stored evidence, listed below.
- **Monthly review**, recording cost per incident.

### Model spend per incident ([Q2.16](conversation.md))

1. **Start from value:** the engineer time saved per incident, multiplied by an hourly cost, is the most the platform can be worth per incident.
2. **Set the budget as a small fraction of that value.** The starting fraction is provisional, at 10%. The per-incident cap is $2.00 (provisional, ADR-56), a placeholder kept from the code until the value is measured. At 10%, it assumes $20 of engineer time saved per incident.
3. **Enforce it:** a maximum number of model calls and tokens per incident. When the budget is reached, stop and hand over with the findings so far.
4. **Measure it:** record the actual cost of every incident, and adjust the budget after the pilot.
5. **Save calls:** gather and line up evidence in code, and call the model only for reasoning.

### Concurrency ([Q2.17](conversation.md))

- **Size from history:** take the highest number of simultaneous incidents seen in past records, and at least double it for headroom. The provisional limit is 5 concurrent investigations, at least double the assumed peak of 2 (ADR-55).
- **Group related alerts:** alerts that share a start time and a likely cause become one incident, not many.
- **Queue the rest by severity:** beyond the limit, incidents wait in a queue ordered by severity, and engineers are told they're queued.
- **Never drop an alert:** every alert is recorded, even if its investigation waits. A rejected alert is stored in a dead-letter record, and the incident lead is notified (ADR-58).

### Time ([Q2.18](conversation.md))

| Milestone | Provisional limit |
| --- | --- |
| First summary to the on-call engineer | Target: ≤ 5 minutes p95 after the alert. Missing it doesn't stop the work |
| Full investigation, with ranked causes | Limit: 15 minutes |
| Each evidence or model step | Its own short timeout, so one slow source can't block the rest |

When the 15-minute limit is reached, the platform stops, shares what it has found and what is missing, and hands over to a person.

### Values set after this layer

Layer 2 sets the rules above. Later layers and the register supply these provisional values ([System Design, 9](../../reference/system-design.md), [Q4.34](../layer-04-requirements/conversation.md), ADR-55, ADR-56, ADR-57). The money values are placeholders, so that limits can be built and tested:

| Limit | Value (provisional) |
| --- | --- |
| Currency | USD |
| Model spend per incident | $2.00 |
| Monthly ceiling | $500: 250 incidents at the per-incident cap, so sized on model spend |
| Model calls per incident | 30 |
| Tokens per incident | 200,000 input, 20,000 output |
| Tokens per call | 16,000 input |
| Time per model call | 60 s |
| Concurrent investigations | 5; queue up to 50 by severity. Beyond 50 queued, new incidents are still recorded, and the incident lead is alerted |
| Daily spend, all incidents | $16.67 (monthly ÷ 30); alert at 80%. At 100%, new investigations are evidence-only; running ones finish their current step, then stop and hand over (ADR-55) |
| Disposable copies at once | 3, queued beyond that |
| Retention | All collected evidence snapshots 30 days. Evidence cited by a diagnosis, redacted model input and output, timeline and audit, patches and test logs 1 year (ADR-57, which also lists traces, logs, metrics and backups) |

**Needs the team:** the actual monthly budget and hourly cost, Owner: Engineering manager, settled when: finance confirms (ADR-56).

**Needs measurement:** the engineer time saved per incident, which tests the $2.00 cap against the 10% rule, Owner: Engineering manager, settled when: measured in the pilot (ADR-56).

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

Recorded triggers for revisiting GPUs: monthly model spend above 80% of the ceiling for 3 straight months (provisional, ADR-56), or a data-residency rule that a provider cannot meet.

### Provider outage ([Q2.19](conversation.md))

1. **Keep doing everything that doesn't need the model:** receive the alert, open the record, notify people, gather evidence, build the timeline, and list recent changes around the start time.
2. **Retry briefly**, then stop calling the provider for a short period, so it doesn't pile up failing calls. The Model Gateway owns both (ADR-27, ADR-61).
3. **Tell people plainly** that the analysis is unavailable, and share the evidence and timeline.
4. **Never propose a fix without the model's reasoning.** People decide.
5. **No automatic resume.** The incident goes from `inconclusive` to `handed_over`. Once the provider returns, a person may request re-investigation (ADR-46).

There is no automatic fallback to another provider (ADR-38). A second provider is considered if provider-caused hand-overs exceed 5% of incidents in 90 days, or there are 2 outages over 60 minutes in a quarter (provisional, ADR-56).

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
| The platform writing to production before Release B | [Q2.14](conversation.md) | Release B's entry conditions and a recorded decision (ADR-44); see 2.3 |
| Other kinds of incident, many services failing at once, a polished interface | [Q1.16](../layer-01-problem-discovery/conversation.md) | Come later. Out-of-scope failure types are recorded, recognised and handed over, never fixed (ADR-58) |

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

**Decision.** Out of scope for the first version. Design one small thing now: tag every top-level record with an organisation identifier, reached by join from the rest (ADR-64), so isolation can be enforced later without migrating data.

## 2.7 Unresolved dependencies

*Answers Q2.20–Q2.23.*

### Requirement labels ([Q2.20](conversation.md))

| Type | Definition | Example |
| --- | --- | --- |
| **Fixed** | Given by the scenario, a stakeholder, a rule or a regulation | Production changes need a person's approval |
| **Decision** | Chosen by the team, with a rationale | One organisation first |
| **Assumption** | Believed without evidence yet | Recent changes cause most incidents |

Each assumption gets an owner, a validation condition, a deadline, and a note of what breaks if it's wrong.

**Changing a Fixed item.** A Fixed item stays fixed until a new accepted ADR replaces it; a routine team choice can't relax it. Production approval is Fixed for the whole first version, Release B included. After that, a specific low-risk, reversible action may run without approval only through its own ADR, one action at a time, once proven safe, with an owner and a review trigger, signed off by the Engineering manager and Security lead ([Q1.22](../layer-01-problem-discovery/conversation.md), [System Design, 8](../../reference/system-design.md)). Irreversible actions and the actions System Design section 8 prohibits always need a person.

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

These follow from the Layer 2 answers and the consolidated design ([System Design, 10 and 12](../../reference/system-design.md)). Every open entry here traces back to [decisions.md](../../decisions.md), the decision log.

Deadlines are milestone gates, and are provisional (A16). **Before M*n*** means settled before work on that milestone starts. **Before the pilot** means after the M4 gate and before the pilot starts. **Pilot review** is the accuracy and approval review that precedes Release B (ADR-44). System Design section 10 lists its unknowns as "to settle before build", so those are Before M1.

| Dependency | From | Owner (role) | Deadline | Settled when |
| --- | --- | --- | --- | --- |
| Named people for each role | [Q2.23](conversation.md) | Engineering manager | Before M1 | Names entered in [decisions.md](../../decisions.md) |
| Release process can apply, undo and look up one change by key | [Q2.6](conversation.md), ADR-47 | Platform owner | Before M1 | Scenario revert done through it |
| On-call rotas cover every in-scope service, including nights | A8 | Engineering manager | Before M1 | Rotas confirmed |
| Historical peak concurrency | [Q2.17](conversation.md), A6 | SRE lead | Before M1 | Past incident records checked |
| Provider agreement on data retention and training | A2 | Security lead | Before M1 | Terms confirmed |
| In-scope alerts carry a stable service label and identifier | [System Design, 10](../../reference/system-design.md) | Platform owner | Before M1 | Alert samples inspected |
| How often real logs contain personal data or secrets | [System Design, 10](../../reference/system-design.md) | Security lead | Before M1 | Seeded and sampled corpus reviewed |
| Test host that runs containers under gVisor, separate from the platform host | [System Design, 12](../../reference/system-design.md), ADR-51 | Platform owner | Before M2 | A container runs there under gVisor with no network |
| Named identities and data-owner approval for each access in 2.3 | [Q2.5](conversation.md) | Security lead | Before M3 | Each access has its own identity, its data owner's approval and a log. Due before Release A's first production read; M1 and M2 have no production access |
| The organisation accepts the approval rules in ADR-49 | A8 | Engineering manager | Before M3 | Rules written down and accepted |
| Identity system with groups per service | [System Design, 12](../../reference/system-design.md) | Security lead | Before M3 | Approve rights resolve per service through the groups |
| Managed PostgreSQL with point-in-time recovery | [System Design, 12](../../reference/system-design.md) | Platform owner | Before M4 | A restore is tested |
| Actual monthly budget and hourly cost | A14 | Engineering manager | Before the pilot | Finance confirms (ADR-56) |
| Recorded decision to switch on Release B | ADR-44 | Engineering manager | Pilot review | Decision recorded and the write credential provisioned |

**Needs the team:** named people for each role, in place of the role owners used throughout this artifact and the register, Owner: Engineering manager, settled when: names are entered in [decisions.md](../../decisions.md).

**Needs the team:** whether the release process supports apply, undo and lookup by key, Owner: Platform owner, settled when: the scenario revert is done through it.

**Needs the team:** on-call rotas and night coverage, Owner: Engineering manager, settled when: rotas are confirmed.

**Needs the team:** provider terms on retention and training, Owner: Security lead, settled when: terms are confirmed.

**Needs measurement:** real alert samples, Owner: Platform owner, settled when: samples are inspected. Peak concurrency, Owner: SRE lead, settled when: past records are checked. Personal data in logs, Owner: Security lead, settled when: samples are inspected.

### Decision log format ([Q2.23](conversation.md))

The one **decision log** is [decisions.md](../../decisions.md), in the team repository, one entry per decision, numbered in the ADR series (ADR-45). Review open entries at every layer boundary.

The log feeds this section: an entry that is still open appears under Dependencies to settle. The Q2.23 reasoning stops mid-way, at "It also feeds the “unresolved dependencies”", and this is the plain reading of that sentence.

| Field | Content |
| --- | --- |
| ID and title | e.g. ADR-51: process topology, which records no Kubernetes for the first version |
| Status | Proposed, accepted, superseded or reversed |
| Decision | What was decided |
| Rationale | Why, and which alternatives were rejected |
| Assumptions it rests on | Linked assumption IDs (A1, A2…) |
| Owner | A named person. Roles stand in until names are entered |
| Validation condition | What evidence confirms it, and by when |
| Revisit trigger | What would reopen it |
| Reversal cost | How hard it is to undo |
| Date and links | Which conversation and artifact it came from |

### A decision and the assumption behind it ([Q2.21](conversation.md))

Recorded as two linked entries. Under Layer 2's focus, the decision is about the *kind* of store; the specific product stays provisional.

| Entry | Content |
| --- | --- |
| Decision | Use a single relational database for the incident record. *Rationale:* strong consistency, ordered records, and a familiar operations model. *Status:* provisional |
| Assumption it rests on (A9) | Data volume stays within what one database handles comfortably. *Owner:* platform owner. *Validated when:* volume is measured during the pilot. *If false:* revisit storage for the record itself. Bulky evidence is already stored separately, in the object store, from the start (ADR-57) |

The entry also notes the reversal cost and the trigger for revisiting.

## Assumptions

Layer 2's answers have no per-question assumption lists, and Q2.23 names the ID form (A1, A2…) without numbering any assumption. IDs here follow source order: Q2.22 (A1–A8), Q2.21 (A9), the Q2.20 example (A10), then the values the team labelled provisional (A11–A13), then the values added to close gaps (A14–A16). Owners and conditions come from the assumption's own answer, or from a later answer that states the same assumption, cited in the row. Where the sources gave neither, the owner is the role the register or System Design gives that area, and the condition is provisional. Deadlines follow the rules in 2.7.

These IDs are separate from the shared-known labels. **A1** is an assumption; **A1-a** is a shared known from Artifact 1. **A2** is an assumption; **A2-a** to **A2-d** are the labels defined in 2.1, 2.3 and 2.4.

| Assumption | Status | Owner | Validated when | Deadline |
| --- | --- | --- | --- | --- |
| **A1:** A capable model is available and affordable | Provisional | SRE lead | The M1 gate passes: accuracy ≥ 80% and budget caps hold ([Technical specification, 12](../../reference/technical-specification.md)) | Before M2 |
| **A2:** Incident data may be sent to an outside provider | Provisional | Security lead ([System Design, 10](../../reference/system-design.md)) | Provider terms on data retention are confirmed | Before M1 |
| **A3:** Evidence sources are accessible read-only | Provisional | Security lead ([Q1.22](../layer-01-problem-discovery/conversation.md)) | The platform's access is confirmed to be read-only | Before M3 |
| **A4:** Releases and configuration changes are recorded with times and authors | Provisional | Platform owner ([Q3.7](../layer-03-sre-fundamentals/conversation.md), [Q3.26](../layer-03-sre-fundamentals/conversation.md)) | The release record shows configuration differences per release, and the release process emits a record for each deployment | Before M1 |
| **A5:** A disposable copy can reproduce the failure | Provisional | Platform owner ([Q4.10](../layer-04-requirements/conversation.md)) | The HTTP 500 errors reproduce there | Before M3 |
| **A6:** Incident volume and concurrency stay small | Provisional | SRE lead ([Q4.34](../layer-04-requirements/conversation.md)) | Past records are checked | Before M1 |
| **A7:** One organisation only | Provisional | Engineering manager | No second organisation is planned through the pilot | Before M1 |
| **A8:** An approver can be reached at night | Provisional | Engineering manager ([Q1.22](../layer-01-problem-discovery/conversation.md)) | The on-call rota covers every service in scope | Before M1 |
| **A9:** Data volume stays within what one database handles comfortably | Provisional | Platform owner | Volume is measured during the pilot | Pilot review |
| **A10:** Recent changes cause most incidents | Provisional | SRE lead | Past incident records show that most incidents followed a release or configuration change | Before M1 |
| **A11:** Model spend at 10% of the value of engineer time saved is the right starting fraction | Provisional | Engineering manager | The actual cost of every incident is recorded, and the budget is adjusted after the pilot | Pilot review |
| **A12:** 5 concurrent investigations is enough | Provisional | SRE lead ([Q4.34](../layer-04-requirements/conversation.md)) | Past records are checked | Before M1 |
| **A13:** The 5-minute and 15-minute time limits are right | Provisional | SRE lead | The M1 gate passes on the test suite, then the pilot confirms ([System Design, 10](../../reference/system-design.md)) | Before M2 |
| **A14:** The spend and capacity values in ADR-56 are workable | Provisional | Engineering manager (ADR-56) | Finance confirms the money values, and the rest are reviewed after the pilot | Before the pilot |
| **A15:** The retention periods in ADR-57 are allowed | Provisional | Security lead (ADR-57) | Provider terms and any legal review don't require shorter periods | Before M3 |
| **A16:** The milestone deadlines in 2.7 and in this table are the right gates | Provisional | Engineering manager | Dates are set once staffing is confirmed, and the team signs off the gates | Before M1 |

## Decisions and conflicts

**Decisions.** Every decision in this layer appears in a section above. Q2.13's organisation identifier is in 2.6; Q2.21's relational-store decision is in 2.7.

| Item | Resolution | Decided by |
| --- | --- | --- |
| Conflict 1: production write off (Q2.14) vs write access after approval (Q2.5) and the M3 dispatcher; two meanings of "first version" | First version is everything through M4, in Releases A and B. Release A has no write credential; a recorded decision provisions it for Release B | ADR-44 |
| Conflict 2: automatic resume after an outage (Q2.19) vs inconclusive and handed over (Q12.27) | No automatic resume. A person may request re-investigation | ADR-46 |
| Conflict 3: one deployable (Q2.10) vs four processes | One codebase, four processes | ADR-51 |
| Conflict 4: outbound network the test needs (Q2.14) vs no network | No network. A mirror needs a new ADR | ADR-60 |
| Conflict 5: saves only evidence used (Q2.4, Q2.15) vs snapshots of everything collected | All collected snapshots kept 30 days; only cited evidence kept long-term | ADR-57 |
| Conflict 6: read-only source and no review-system access (Q2.5, Q2.6) vs drafts in the review system | Draft pull requests allowed in both releases and added to 2.3. Direct commits never | ADR-44 |
| Conflict 7: Kubernetes triggers in Q2.9 vs Q6.33 | One list, in 2.3. Team count is dropped; 20 services and concurrency above 5 give the Q2.9 triggers numbers | ADR-51 |
| Conflict 8: bulky evidence separate only if A9 fails (Q2.21) vs object store from the start | Object store from the start. A9 still stands for the record | ADR-57 |
| Conflict 9: 5 minutes as a stopping limit (Q2.18) vs a p95 target | ≤ 5 minutes p95 is a target and stops nothing; only the 15-minute limit stops and hands over. The later source wins, and ADR-65's gates match | Layer decision (proposed) |
| Conflict 10: production approval is Fixed (Q2.20) vs the team may later relax it (Q1.22, System Design 8) | Stays Fixed for the whole first version. A Fixed item changes only through a new accepted ADR; each later exception is one action, signed off by Engineering manager and Security lead. See 2.7 | Layer decision (proposed) |
| Conflict 11: D-04 IDs (Q2.23) vs ADR numbers; no log existed | ADR series, and [decisions.md](../../decisions.md) is the log. The Kubernetes record is in ADR-51 | ADR-45 |
| Label wording: Layer 3 states A2-c once as "changes can be tested in a disposable environment" (Q3.7) | The wording in 2.3 is kept | Layer decision (proposed) |
| Gap 2.1: where the reference application runs, and whether on Kubernetes | One Linux host under Docker Compose; test environment on a separate host; not Kubernetes | ADR-51 |
| Gap 2.1: cloud or private infrastructure (not stated in ADR-51) | Either: the design depends only on the Linux host | Layer decision (proposed) |
| Gap 2.4: no monthly ceiling, hourly cost or time saved | $500 ceiling and $2.00 cap, as placeholders. Budget, hourly cost and time saved stay open in 2.4 | ADR-56 |
| Gap 2.4: cap on disposable copies | 3, queued beyond that | ADR-56 |
| Scope of the monthly ceiling: Q2.15 bounds infrastructure, ADR-56 sizes $500 on model spend | The $500 ceiling is model spend only. Infrastructure cost is separate and stays with the actual budget, open in 2.4 | ADR-56 |
| Gap 2.5: spend threshold for GPUs (Q2.12), and outage frequency for a second provider (Q2.19) | Model spend above 80% of ceiling for 3 straight months, for GPUs, self-hosting and Tinker; the second-provider trigger as in 2.5 | ADR-56 |
| Gap 2.7: no decision log | [decisions.md](../../decisions.md) | ADR-45 |
| Gap 2.7: no deadlines per assumption or dependency (Q2.20) | Milestone gates, provisional, in 2.7 and Assumptions (A16) | Layer decision (proposed) |
| Gap 2.7: no named owners (Q2.23) | Roles for now. Named people stay open in 2.7 | ADR-45 |
| Gap 2.7: the Q2.23 reasoning stops mid-sentence | The log feeds 2.7. The source sentence stays as it is, and 2.7 notes that it stops mid-way | Layer decision (proposed) |
| Register entries lack Q2.23's assumption-link and validation-condition fields | Q2.23's field list stays the format. Until entries gain those fields, this layer's Assumptions table holds that link | Layer decision (proposed) |
| Q2.13 tags every record; ADR-64 places `org_id` on top-level records only | Top-level records, reached by join elsewhere | ADR-64 |
| Code only: monthly ceiling, daily limit and copy cap aren't in the reference code | Listed for the code changes | ADR-56 |

## Acceptance checks

- [ ] Every required section above is present and complete.
- [x] Every section lists the questions it answers, and every question in this layer is answered by at least one section.
- [x] Every assumption is marked provisional and has a named owner and a condition under which it will be validated.
- [ ] Every disagreement between Claude and ChatGPT that affected a decision is recorded with the team's resolution.
- [ ] The artifact is consistent with every artifact it is built from; any conflict is recorded as an explicit decision.
- [x] The artifact was produced with the team's coding agent and is saved in the team repository.

Reviewed by: TBD
Date: TBD
