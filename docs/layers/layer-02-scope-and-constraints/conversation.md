# Layer 2: Scope and Constraints — Conversation

- **Central question:** What are we allowed to build?
- **Built from:** Artifact 1
- **Focus for this layer:** Architecture starts with constraints, not technology selection. Establish boundaries, budgets and assumptions before choosing any tool.
- **Deliverable:** [Artifact 2: Scope and Constraints Document](artifact-02-scope-and-constraints-document.md)
- **Questions:** 23

## Conversation log

| Questions | Agent | Conversation link | Decision reached | Open unknowns and owner | Artifact section |
| --- | --- | --- | --- | --- | --- |
| Q2.1–Q2.23 | TBD | [Shared conversation](https://claude.ai/share/c6a48a50-d511-4886-b4f5-0a3000f6a725) (needs sign-in; not read for these docs) | Recorded per question below | Listed under each question's Unknowns and Assumptions | [Artifact 2: Scope and Constraints Document](artifact-02-scope-and-constraints-document.md) |

## Where Claude and ChatGPT disagreed

TBD: the source documents do not record any disagreements. The team fills this in.

## Questions and answers

### Q2.1 An organization wants to deploy the platform across several engineering teams. Should the initial system serve one engineering organization or multiple independent organizations?

**Frame.** Should the first version serve one engineering organisation, or several independent ones?

**Answer.** **One organisation**, used by several of its teams. Teams share the platform, but each team sees and acts only on its own services. Serving many independent organisations is a non-goal for the first version (Artifact 1, 1.7).

**Reasoning.** Separate organisations need strict isolation of data, permissions, billing and configuration between them, which is a large body of work that proves nothing about the core loop. Several teams inside one organisation already test ownership and access, at much lower cost.

### Q2.2 The organization’s applications use different programming languages. Which languages should the initial platform support?

**Frame.** Applications use different languages; which should the first version support?

**Answer.** **Python only**, the language of the reference scenario. Anything the investigation reads that doesn’t depend on language (errors, health signals, release and configuration history) should be designed so other languages can be added later.

**Reasoning.** Each extra language multiplies the error formats, the fixes and the tests needed. For change-caused failures, the key evidence is when the change happened and what it changed, and that is largely the same whatever the language. So one language proves the loop, and the rest can follow.

### Q2.3 Some applications run on cloud infrastructure while others operate on private infrastructure. Which deployment environments should the platform support?

**Frame.** Some applications run in the cloud, others on private infrastructure; which should the first version support?

**Answer.** **One environment**: wherever the reference application runs. The platform reaches that environment only through clearly defined connection points (for evidence, changes and fixes), so a second environment can be added later without redesign. Supporting both cloud and private infrastructure is deferred.

**Reasoning.** Each environment has different access, networking and security rules. Supporting both doubles the integration and security work before the core value is proven. Keeping the connection points behind a defined boundary leaves room to add the other environment later.

### Q2.4 The organization already uses monitoring platforms. How should the new platform integrate with these existing systems?

**Frame.** The organisation already has monitoring; how should the new platform fit in?

**Answer.**

- **Don’t replace it.** Existing monitoring keeps detecting incidents (detection is a non-goal).
- **Start from its alerts.** The platform starts work when it receives an alert from existing monitoring.
- **Read, don’t own.** It reads evidence from existing sources with read-only access, and doesn’t copy everything into a new store.
- **Keep the record.** It saves only the evidence it used, in the incident record.

**Reasoning.** Engineers already trust their monitoring. Replacing it adds risk and effort with no gain. Reading from it keeps the platform small and avoids a second, competing view of the truth.

### Q2.5 The investigation requires logs, metrics and traces, but the platform currently has no access to them. Which data access permissions and integrations must be established?

**Frame.** The investigation needs error records, health signals and traces, but the platform has no access yet. What must be set up?

**Answer**

| **Access** | **Level** | **Scope** |
| --- | --- | --- |
| Alerts from existing monitoring | Receive | Services in scope |
| Error records | Read-only | Services in scope, recent window only |
| Health signals | Read-only | Services in scope |
| Request traces (if collected) | Read-only | Services in scope |
| Release and configuration history | Read-only | Services in scope |
| Service ownership and on-call | Read-only | All services in scope |
| Notifications to engineers | Send | Owners, on-call, change authors |
| Undoing a change | Write, **only after approval**, and only the specific change | Affected service only |

Every access uses its own named identity, with the least permission needed. It is approved by the data owner and logged.

**Reasoning.** Read-only access lets the platform investigate without being able to cause harm. The one write permission is narrow and gated by human approval

### Q2.6 An engineer needs to understand whether a failure was caused by a recent deployment. How could the platform access deployment history and source repositories?

**Frame.** To tell whether a release caused the failure, how does the platform see release history and source code?

**Answer.**

- **Release history:** read the record of releases, with each one’s time, author, version and the configuration it changed.
- **Source code:** read-only access, used to compare the failing version with the previous one, with a focus on configuration changes.
- **Undoing a change:** only through the organisation’s existing release process, after approval. The platform never edits source code or history directly.

**Reasoning.** The scenario’s cause is a configuration change in a release, so the release record and the difference between versions are the decisive evidence. Going through the existing release process for any fix keeps the normal checks in place, and prohibited action 2 (never overwrite history) is respected.

### Q2.7 Real production incidents contain sensitive information and are difficult to reproduce. Should the initial implementation use real incidents or sanitized synthetic fixtures?

**Frame.** Real incidents hold sensitive data and are hard to reproduce. What should the first version use?

**Answer.** **Synthetic test cases first**, based on the reference scenario. These are built, cleaned failures with a known cause and known correct fix. Real incidents come later, in this order:

1. Replay past incidents with sensitive data removed.
2. Advisory mode on live incidents: the platform investigates and recommends, but takes no action.
3. Approved fixes on live incidents.

**Reasoning.** Synthetic cases can be repeated, are safe to share, and come with a known right answer. That is exactly what is needed to measure accuracy and to run the honesty check. Moving to real data in stages earns trust without exposing sensitive data early.

### Q2.8 Stakeholders expect a sophisticated dashboard, but the engineering team has limited time. Could the first version operate using only a CLI and HTTP API?

**Frame.** With limited time, can the first version work with only a command-line tool and an HTTP API?

**Answer.** **Yes.**

- **HTTP API:** receives alerts and exposes the incident record, approvals and verification results.
- **Command-line tool:** lets engineers view an incident, see evidence and timeline, approve or reject a fix, and accept recovery.
- **Notifications:** carry a short summary and a link or command for the approval step.

**Reasoning.** The value is in the investigation, the approval gate and the verification, not the screens. A command-line tool covers everything the on-call engineer needs at 2 AM. Because the API is built first, a dashboard can be added later without changing the core. For stakeholders, the end-to-end demo shows the full loop without a dashboard.

### Q2.9 The team is considering deploying the application on Kubernetes even though the initial workload is small. What operational problem would Kubernetes solve, and is that problem present yet?

**Frame.** What problem would Kubernetes solve, and do we have that problem yet?

**Answer.**

| **What Kubernetes solves** | **Do we have it now?** |
| --- | --- |
| Running many services across many machines | No: one small platform, one organisation |
| Automatic scaling under heavy, changing load | No: a handful of incidents at a time |
| Self-healing and restart across a cluster | Partly, but a simpler host with restart-on-failure covers it |
| Standard deployment across many teams | Not yet |

**Decision.** **Don’t use it for the first version.** Run the platform on the simplest setup that restarts it if it fails, and package it so it could move to Kubernetes later. Revisit when concurrent incidents, the number of services, or team count make scaling and scheduling a real problem. Record this as a decision with that trigger.

**Reasoning.** Kubernetes adds operational work (cluster upgrades, networking, security, on-call knowledge) that a small team would have to carry before it delivers any benefit. Its problems aren’t present yet. Packaging the platform portably keeps the option open at almost no cost.

**Separate point.** If the reference application itself runs on Kubernetes, the platform still has to read that application’s releases and health. That is integration, not hosting the platform on Kubernetes.

### Q2.10 Engineers propose splitting every component into an independent microservice. What problems would this introduce, and does the initial workload justify them?

**Frame.** What problems would microservices bring, and does a small first workload justify them?

**Answer**

| **Problem microservices introduce** | **Cost for us now** |
| --- | --- |
| Network calls between parts, which can fail or be slow | New failure modes inside the platform that’s meant to handle failures |
| Data split across services, hard to keep consistent | The incident record must stay complete and in order (Artifact 1, UC8) |
| Many things to deploy, version and monitor | Operational load on a small team |
| Harder testing and debugging across boundaries | Slower development |
| A complete picture needs a trace across services | More tooling before any user value |

**Decision.** **No.** Build one deployable application with clear internal modules: intake, evidence, investigation, remediation, verification and record. Split a module out only when it has a different scaling, security or release need. The likely first candidate is the sandbox that runs fixes (Q2.14), because it needs strict isolation.

**Reasoning.** The workload is small, there is one team, and there is one organisation. Microservices solve scaling and team-independence problems we don’t have, and they add distributed-failure problems we can’t afford. Clear internal modules keep the option to split later.

### Q2.11 The initial implementation requires an AI model, but the team is considering training its own model. What capabilities would justify developing a custom model?

**Frame.** What would justify building our own model instead of using an existing one?

**Answer.** Train or fine-tune only if all of these hold:

- **A proven ceiling:** well-designed prompts on a capable existing model, with the right evidence supplied, measurably fail on our evaluation set.
- **Enough labelled data:** a large set of past incidents with confirmed causes and fixes.
- **A narrow, stable task:** for example, classifying failure types in our own formats.
- **A hard requirement existing models can’t meet**, on cost, speed, or data that cannot leave the organisation.

**Decision.** **Not for the first version.** Use an existing model, give it gathered evidence, and measure it. Revisit when the evaluation data shows the ceiling has been reached.

**Reasoning.** None of these conditions holds today. There is no labelled incident data, and the task (connecting errors to a recent change) is well within a capable model’s reach when it is given the evidence. Training needs data, compute and ongoing upkeep, all before we know whether it is needed.

### Q2.12 The team wants to operate its own GPU infrastructure. What workload, cost or controlrequirements would justify this investment?

**Frame.** What workload, cost or control needs would justify operating our own GPUs?

**Answer.** Only one of these would justify it:

- **Volume:** sustained model usage high enough that owning hardware is cheaper than paying per call, at measured utilisation.
- **Control:** a rule that incident data must never leave the organisation’s infrastructure, and no provider can meet it contractually.
- **Speed:** response times that a hosted provider cannot meet.
- **A self-hosted or custom model** is required (Q2.11).

**Decision.** **No.** Use a hosted model with usage caps. Record the triggers for revisiting: monthly model spend above a set threshold, or a data-residency rule that a provider cannot meet.

**Reasoning.** The first workload is a handful of incidents, a few model calls each. GPU infrastructure is a fixed cost and a new on-call burden that would sit idle most of the time. Model provider outages are handled by fallback behaviour (Q2.19), not by owning hardware.

### Q2.13 Several organizations may eventually use the platform. Which technical and security require-ments would multi-tenant operation introduce?

**Frame.** If several organisations eventually use it, what technical and security requirements follow?

**Answer**

- **Data isolation:** each organisation’s evidence, records and settings are separated and can never be seen by another.
- **Identity and access:** separate users, roles and approvers per organisation; no shared admin powers across them.
- **Separate credentials:** each organisation’s access keys to its monitoring and code are stored and used separately.
- **Execution isolation:** sandboxes and fixes for one organisation can never reach another’s systems.
- **Fair use:** per-organisation limits on load and model spend, so one can’t starve others.
- **Model-provider handling:** one organisation’s data never appears in another’s prompts or cache.
- **Audit and deletion:** per-organisation audit trails, retention rules and full deletion on exit.
- **Compliance:** contracts and data-processing terms per organisation; possibly data kept in specific regions.
- **Separate operations:** per-organisation configuration, upgrades and incident communication.

**Decision.** Out of scope for the first version (Q2.1). Design one small thing now: tag every record with an organisation identifier, so isolation can be enforced later without migrating data.

**Reasoning.** These requirements are substantial and mostly about security. Adding the identifier now is cheap; adding it later means rewriting every record.

### Q2.14 The AI can generate a repair but the team is not ready to let it modify production. How could execution be restricted to a disposable environment?

**Frame.** The AI can produce a repair, but production is off-limits. How do we confine execution to a disposable environment?

**Answer**

1. **Build a copy:** a fresh, short-lived copy of the affected service at the failing version, with the reference failure reproduced.
2. **Isolate it:** no access to production systems, production data or real credentials; no outbound network except what the test needs; strict time and resource limits.
3. **Apply and test the fix there:** run the original symptom check (errors gone, user-facing checks passing).
4. **Destroy it:** delete the copy after the test, and keep only the results in the incident record.
5. **Production is still blocked:** the platform’s production write permission stays off. A person applies the fix through the normal release process, or (later) approves it for the platform to apply (Q2.5).

**Phases**

- **First version:** fixes run in the disposable copy only; people apply any production change.
- **Later:** approved fixes applied to production by the platform, gated as in Artifact 1, 1.5.

**Reasoning.** Separating “can try a fix” from “can change production” lets the team prove fixes work without risk. Fresh, isolated, short-lived copies mean a bad fix, or a malicious one produced through injected input, cannot reach anything real.

### Q2.15 The organization has a limited initial infrastructure budget. How should infrastructure spending be bounded?

**Frame.** How do we keep infrastructure cost within a small budget?

**Answer**

- Set a **fixed monthly ceiling**, with alerts at 50%, 80% and 100%.
- Use the **simplest hosting**: one deployable application (Q2.10), no Kubernetes (Q2.9), no GPUs (Q2.12).
- **Disposable copies are short-lived:** destroyed after each test, with a cap on how many run at once.
- **Don’t duplicate data:** read evidence from existing monitoring; store only what the incident record needs (Q2.4).
- **Set retention limits** on stored evidence.
- **Review monthly**, recording cost per incident.

**Reasoning.** Most infrastructure cost comes from always-on capacity and copied data. Avoiding both keeps cost close to zero when there are no incidents.

### Q2.16 Every incident requires several model calls. How would you establish an acceptable model expenditure per incident?

**Frame.** Each incident needs several model calls. How do we decide what is acceptable to spend?

**Answer**

1. **Start from value:** the engineer time saved per incident, multiplied by an hourly cost, is the most the platform can be worth per incident.
2. **Set the budget as a small fraction of that value.** The starting fraction is provisional, at 10%, so the platform clearly pays for itself.
3. **Enforce it:** a maximum number of model calls and tokens per incident. When the budget is reached, stop and hand over with the findings so far.
4. **Measure it:** record the actual cost of every incident, and adjust the budget after the pilot.
5. **Save calls:** gather and line up evidence in code, and call the model only for reasoning.

**Reasoning.** Linking the budget to time saved makes it defensible to management (Artifact 1, 1.6). A hard cap per incident stops a single runaway investigation from consuming the budget.

### Q2.17 The organization experiences sudden traffic spikes and multiple services can fail simultane-ously. How many concurrent incidents should the system support?

**Frame.** Traffic spikes can break several services at once. How many incidents should the platform handle at the same time?

**Answer**

- **Size from history:** take the highest number of simultaneous incidents seen in past records, and double it for headroom. The provisional starting point is 5 concurrent investigations.
- **Group related alerts:** alerts that share a start time and a likely cause become one incident, not many. A spike often has one root cause.
- **Queue the rest by severity:** beyond the limit, incidents wait in a queue ordered by severity, and engineers are told they’re queued.
- **Never drop an alert:** every alert is recorded, even if its investigation waits.

**Reasoning.** Grouping matters more than raw capacity: in a spike, ten alerts usually have one cause. A bounded limit protects cost and model budget (Q2.15, Q2.16), and the queue makes sure nothing is lost.

### Q2.18 Some investigations require several minutes to collect evidence and evaluate hypotheses. How long should an investigation be allowed to run?

**Frame.** Some investigations take minutes. How long should one be allowed?

**Answer.** Provisional time limits:

| **Milestone** | **Limit** |
| --- | --- |
| First summary to the on-call engineer | 5 minutes after the alert |
| Full investigation, with ranked causes | 15 minutes |
| Each evidence or model step | Its own short timeout, so one slow source can’t block the rest |

When a limit is reached, the platform stops, shares what it has found and what is missing, and hands over to a person (Artifact 1, UC4).

**Reasoning.** An investigation is only useful if it finishes faster than an engineer would. Time limits keep it bounded, and a partial result handed over is better than a late complete one.

### Q2.19 The model provider becomes unavailable during an incident. How should the platform behave?

**Frame.** If the model provider fails during an incident, what should the platform do?

**Answer**

1. **Keep doing everything that doesn’t need the model:** receive the alert, open the record, notify people, gather evidence, build the timeline, and list recent changes around the start time.
2. **Retry briefly**, then stop calling the provider for a short period, so it doesn’t pile up failing calls.
3. **Tell people plainly** that the analysis is unavailable, and share the evidence and timeline.
4. **Never propose a fix without the model’s reasoning.** People decide.
5. **Resume the analysis** automatically when the provider returns, if the incident is still open.

A second provider can be considered later, if outages prove frequent.

**Reasoning.** The evidence and timeline are often enough for an engineer, as in the scenario where errors start right after a release. Degrading to “here is the evidence” keeps the platform useful, and the safety rules hold.

### Q2.20 Several architectural requirements have not yet been validated. How would you distinguish fixed requirements from assumptions?

**Frame.** How do we separate what is fixed from what we only believe?

**Answer.** Label every requirement as one of these:

| **Type** | **Definition** | **Example** |
| --- | --- | --- |
| **Fixed** | Given by the scenario, a stakeholder, a rule or a regulation | Production changes need a person’s approval |
| **Decision** | Chosen by the team, with a rationale | One organisation first |
| **Assumption** | Believed without evidence yet | Recent changes cause most incidents |

Each assumption gets an owner, a validation condition, a deadline, and a note of what breaks if it’s wrong.

**Reasoning.** Treating an assumption as fixed is how architectures fail late. Labelling makes it visible what still needs evidence.

### Q2.21 The team records a decision to use PostgreSQL, but this choice is based on an unverified assumption about data volume. How would you distinguish this architectural decision from the assumption behind it?

**Frame.** “Use PostgreSQL” rests on an unverified guess about data volume. How do we record both?

**Answer.** Record them as two linked entries:

- **Decision:** use a single relational database for the incident record. *Rationale:* strong consistency, ordered records, and a familiar operations model. *Status:* provisional.
- **Assumption it rests on:** data volume stays within what one database handles comfortably. *Owner:* platform owner. *Validated when:* volume is measured during the pilot. *If false:* revisit storage for evidence (for example, store bulky evidence separately and keep the record relational).

Note also the reversal cost and the trigger for revisiting.

**Reasoning.** Separating them means that if the assumption fails, the team knows exactly which decision to revisit, rather than reopening the whole design. Under Layer 2’s focus, the decision is about the *kind* of store; the specific product stays provisional.

### Q2.22 An assumption about model availability turns out to be false after implementation. Which assumptions could invalidate the architecture if they change?

**Frame. Which assumptions, if wrong, would invalidate the design?**

**Answer**

| **Assumption** | **If it’s wrong** |
| --- | --- |
| **A capable model is available and affordable** | **The investigation core needs a different approach** |
| **Incident data may be sent to an outside provider** | **Self-hosting needed (Q2.12)** |
| **Evidence sources are accessible read-only** | **No investigation is possible** |
| **Releases and configuration changes are recorded with times and authors** | **The cause can’t be linked to a change** |
| **A disposable copy can reproduce the failure** | **Fixes can’t be tested safely** |
| **Incident volume and concurrency stay small** | **Scaling design needed (Q2.9, Q2.10)** |
| **One organisation only** | **Multi-tenant work needed (Q2.13)** |
| **An approver can be reached at night** | **Approval flow must change** |

**Reasoning.** These are the assumptions the whole flow depends on. Each needs an early test and a fallback plan.

### Q2.23 Multiple engineering decisions remain unresolved. How would you record these decisions, their rationale, their owners and the conditions required to validate them?

**Frame.** How do we record open decisions, with their rationale, owners and validation conditions?

**Answer.** Keep one **decision log** in the team repository, one entry per decision:

| **Field** | **Content** |
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

Review open entries at every layer boundary.

**Reasoning.** One log, linked to its assumptions, makes every decision traceable and reversible on purpose. It also feeds the “unresolved dependencies”
