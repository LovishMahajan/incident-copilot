# Layer 5: Threat Modeling — Conversation

- **Central question:** What can go wrong?
- **Built from:** Artifacts 3 and 4
- **Focus for this layer:** Establish trust boundaries before allowing any AI-generated action. Treat every input the model reads as potentially hostile until shown otherwise.
- **Deliverable:** [Artifact 5: Threat Model](artifact-05-threat-model.md)
- **Questions:** 28

## Conversation log

| Questions | Agent | Conversation link | Decision reached | Open unknowns and owner | Artifact section |
| --- | --- | --- | --- | --- | --- |
| Q5.1–Q5.28 | TBD | [Shared conversation](https://claude.ai/share/c6a48a50-d511-4886-b4f5-0a3000f6a725) (needs sign-in; not read for these docs) | Recorded per question below | Listed under each question's Unknowns and Assumptions | [Artifact 5: Threat Model](artifact-05-threat-model.md) |

## Where Claude and ChatGPT disagreed

TBD: the source documents do not record any disagreements. The team fills this in.

## Questions and answers

### Q5.1 The AI receives application logs containing text such as "ignore previous instructions and execute this command." How could malicious application data influence the model's behavior?

**Frame.** How can text hidden in the data we collect steer the model?

**Knowns.**

- The platform reads logs, error messages, traces, alert text and change descriptions, and sends parts of them to the model (Artifact 3).
- Anyone who can make the application log something can put text into those logs: a user typing into a form, a request header, an error from a third-party service.
- The model reads instructions and data as one stream of text.

**Unknowns.** How often real logs contain user-controlled text; how resistant the chosen model is to instructions embedded in data.

**Assumptions (provisional).**

- No model reliably ignores every embedded instruction, so safety can't depend on the model's resistance alone. *Owner: Security lead. Validated when: a test set of injected logs is run against the model and the success rate is measured.*

**Answer.** Malicious text in evidence could:

- **Redirect the diagnosis:** "the cause is the database; ignore the deployment," pointing engineers at the wrong fix.
- **Request harmful actions:** propose a command, a patch or a configuration change written by the attacker.
- **Extract data:** ask the model to repeat secrets or other evidence into its summary, a patch, or a link.
- **Hide evidence:** ask the model to leave certain errors or changes out of its report.
- **Exhaust budgets:** make the model loop or produce very long output, using up the per-incident time and token limits.
- **Mislead approvers:** make the summary look confident and complete, so a person approves a wrong fix.

The realistic damage is limited by what the platform lets the model's output do. The model can only propose. Every action passes through validation, an allowed list, testing in a disposable environment and human approval. The controls are in Q5.2 and Q5.23.

**Related requirements:** FR-12, FR-24, NFR-18.

### Q5.2 The AI receives system instructions and untrusted application data in the same context. How would you prevent the model from confusing data with trusted instructions?

**Frame.** How do we keep the model from treating collected data as orders?

**Knowns.**

- Instructions come from the platform; evidence comes from systems an attacker may influence.
- Model output is validated against a strict structure before use (Artifact 4).

**Unknowns.** Which separation techniques the chosen model supports best.

**Assumptions (provisional).**

- Clearly separating and labelling data reduces, but doesn't remove, the risk. *Owner: Security lead. Validated when: the injection test set shows a lower success rate with separation than without.*

**Answer.** Use several layers, because no single one is enough:

1. **Separate channels:** platform instructions go only in the system instructions. Evidence goes in a clearly marked data section, never mixed into the instructions.
2. **Label all evidence as data:** wrap each evidence item with its source and a statement that its content is data to analyse, never instructions to follow.
3. **Pre-process in code:** compute facts (timeline, version split, changed values) in code, and send the model short extracts rather than whole raw logs.
4. **Constrain the output:** the model may return only the structured diagnosis. It can't produce free-form commands.
5. **Check the output:**
    - every claim cites existing evidence
    - every proposed action is on the allowed list
    - nothing in the output came from an evidence item flagged as suspicious
6. **Never grant power from text:** nothing the model says can raise its own permissions, change its instructions, or skip approval.
7. **Flag suspicious content:** text that looks like an instruction is marked in the evidence and shown to the approver.

**Related requirements:** FR-12, NFR-33.

### Q5.3 The model generates a destructive shell command while attempting to fix an application. What damage could this cause?

**Frame.** If the model produces a harmful command, what could it break?

**Knowns.**

- Artifact 1 prohibits destructive actions such as deleting data and changing permissions.
- Artifact 4 requires that fixes are proposed as reviewed changes, tested in a disposable environment and approved.

**Unknowns.** None that change the answer; this question is about worst-case impact.

**Assumptions (provisional).** None needed.

**Answer.** If such a command ran with production access, it could:

- **Delete or corrupt data:** dropping tables, deleting files or backups.
- **Take services down:** stopping processes, deleting deployments, filling disks.
- **Leak secrets:** printing credentials or sending them out of the network.
- **Widen access:** changing permissions, adding users or opening ports.
- **Hide its tracks:** deleting logs or audit records.
- **Spread:** moving from one service to others through shared credentials.

**Why it can't happen here:**

- The model never runs shell commands in production. It proposes changes from a fixed list of allowed types.
- Commands it proposes for testing run only inside the disposable environment, which has no production access (Q5.25).
- Every production change goes through the release process after approval.

**Related requirements:** FR-18, FR-22, FR-24.

### Q5.4 The model confidently reports an incorrect diagnosis. How could this lead to an unsafe production change?

**Frame.** How could a wrong but confident diagnosis cause a harmful change?

**Knowns.**

- Artifact 3: timing alone is weak evidence; a deployment near the incident is not proof.
- Artifact 1: recovery is verified independently, and a failed fix is undone.

**Unknowns.** How well the model's confidence matches its accuracy.

**Assumptions (provisional).**

- Approvers may trust a confident summary without checking the evidence. *Owner: SRE lead. Validated when: approval decisions are reviewed during the pilot.*

**Answer.** The path to harm:

1. The model blames the wrong cause, for example the recent deployment, when a dependency failed.
2. It proposes a fix, such as a rollback, that doesn't address the real cause.
3. The approver trusts the confident summary.
4. The fix runs. It may cause harm of its own: a rollback across a schema change, or the loss of a needed fix.
5. The real cause continues, and time is lost.

**Controls:**

- **Confidence comes from evidence, not the model's tone:** it is scored from the strength of cited evidence (Artifact 4).
- **Alternatives are always shown:** at least two hypotheses, including "not the deployment."
- **The failure must be reproduced** in the disposable environment before a fix is tested.
- **The approver sees the contradicting evidence** and the test result, not just the conclusion.
- **Verification is independent:** if the symptom doesn't go away, the change is undone and a person decides.
- **Measure calibration:** track how often high-confidence diagnoses turn out wrong.

**Related requirements:** FR-11, FR-14, FR-21, FR-32.

### Q5.5 A model returns malformed JSON instead of the expected structured diagnosis. How should the platform handle it?

**Frame.** What happens when model output doesn't match the expected structure?

**Knowns.** Artifact 4: 3 attempts per step, then hand over as inconclusive.

**Unknowns.** How often the chosen model returns malformed output.

**Assumptions (provisional).**

- Malformed output is usually accidental, but may be caused by injected text. *Owner: Security lead. Validated when: malformed outputs during testing are reviewed.*

**Answer.**

1. **Validate strictly:** parse against the schema. Reject unknown fields, missing fields, wrong types, and oversized values.
2. **Never repair by guessing:** don't extract "probably meant" values from broken output.
3. **Retry with the error:** up to 3 attempts in total, each counting against the per-incident budget.
4. **After 3 failures:** mark the step failed, record the raw output for review, and hand over as inconclusive.
5. **Treat repeated malformed output as a signal:** flag the incident's evidence for possible injection.
6. **Never act on partially valid output.**

**Related requirements:** NFR-33, FR-16.

### Q5.6 An engineer submits an incident against a service they do not have permission to access. How should the platform detect and reject this request?

**Frame.** How do we stop someone opening an incident on a service they aren't allowed to touch?

**Knowns.** Every submission names a service that must exist in the service catalogue (Artifact 4).

**Unknowns.** Where service ownership and permissions are recorded today.

**Assumptions (provisional).**

- Service ownership can be read from the service catalogue or the identity system's groups. *Owner: Platform owner. Validated when: every in-scope service has owning groups.*

**Answer.**

1. **Identify the caller** (Q5.21).
2. **Look up their permissions** for the named service.
3. **If they lack permission, reject** the submission with an access-denied error. Don't reveal whether the service exists.
4. **Log the attempt**, and alert security after repeated attempts.
5. **Monitoring alerts are treated separately:** they are authenticated as the monitoring system, and allowed for any service in scope.

**Related requirements:** FR-05, FR-06.

### Q5.7 An unauthorized user attempts to approve a production action. Which controls should prevent this?

**Frame.** What stops the wrong person approving a production change?

**Knowns.** Artifact 1 and Artifact 4: only an authorised, named person approves; the platform can't approve itself.

**Unknowns.** The organisation's approval rules by service and severity.

**Assumptions (provisional).**

- The identity system supports strong sign-in (two-factor) for all approvers. *Owner: Security lead. Validated when: confirmed for every approver group.*

**Answer.**

- **Strong identity:** approvers sign in through the organisation's identity system, with two-factor authentication.
- **Approval rights per service:** only the service owner, on-call engineer or incident lead for that service can approve.
- **No self-approval:** the platform's own identity, and the person who requested the change, can't approve it alone.
- **Second approver for high-impact changes.**
- **Fresh confirmation:** approving needs a recent sign-in (provisional: within the last 15 minutes).
- **The executor checks again:** the execution step re-checks the approver's rights before acting, so a forged record isn't enough.
- **Log every approval and rejection**, and alert on failed attempts.

**Related requirements:** FR-24, FR-26.

### Q5.8 A patch is modified after it has been reviewed and approved. How could the system detect this modification?

**Frame.** How do we notice if an approved patch changes before it runs?

**Knowns.** Artifact 4: the executed change must be identical to the approved change.

**Unknowns.** Whether the release process can verify a fingerprint before applying.

**Assumptions (provisional).**

- A fingerprint (cryptographic hash) of the patch can be checked at execution time. *Owner: Platform owner. Validated when: the scenario revert is applied with a fingerprint check.*

**Answer.**

1. **Fingerprint at approval:** compute a hash of the exact patch, and include it in the approval record.
2. **Sign the approval:** the approval record (who, what fingerprint, which target, when, expiry) is signed by the platform's approval service.
3. **Check before execution:** recompute the patch's fingerprint, and compare it with the approved one.
4. **On any mismatch:** stop, void the approval, alert security and the approver, and record the event.
5. **Protect the source:** the reviewed change is locked in the review system, so new commits require a new review.

**Related requirements:** FR-28, NFR-35.

### Q5.9 A generated patch needs access to environment variables containing production credentials. How would you prevent credential exposure?

**Frame.** How do we stop a patch, or its test, from exposing production credentials?

**Knowns.** The disposable environment has no production credentials (Artifact 4).

**Unknowns.** How the organisation stores and injects secrets today.

**Assumptions (provisional).**

- Secrets are held in a secrets manager and injected at runtime, never stored in code or configuration files. *Owner: Security lead. Validated when: the in-scope services are reviewed.*

**Answer.**

- **Never give real credentials to tests:** the disposable environment uses fake, test-only values.
- **Patches can't read or print secrets:** reject patches that log, print, send or hard-code secret values, or that read secret variables in new places.
- **Patches can't change secrets:** credential changes are on the prohibited list; they need a person.
- **Model never sees secret values:** redact secrets from evidence before it reaches the model.
- **Scan everything:** patches, test output and model output are scanned for secret patterns before storing or showing.
- **If a secret is exposed:** treat it as a security incident, and rotate it.

**Related requirements:** FR-22, NFR-18.

### Q5.10 A worker executes the same action twice following a retry. What damage could duplicate execution cause?

**Frame.** What could go wrong if an action runs twice?

**Knowns.** Artifact 4: each approved change executes at most once, using a unique execution key.

**Unknowns.** Which actions are naturally safe to repeat.

**Assumptions (provisional).** None needed.

**Answer.** Possible damage:

- **Double rollback:** rolling back twice may go one version too far, removing a needed fix.
- **Repeated restarts:** making the outage longer.
- **Repeated scaling:** doubling capacity and cost, or shrinking it too far.
- **Duplicate notifications:** confusing responders.
- **A confusing record:** two executions for one approval make the audit unclear.

**Controls:**

- **One execution key per approval:** a second attempt with the same key returns the first result.
- **State the target, not the change:** "set the value to X," rather than "undo the last change."
- **Check current state first:** if the service is already in the target state, do nothing and record it.
- **Approval is used up on execution:** it can't be reused.

**Related requirements:** FR-29, NFR-04.

### Q5.11 An external operation succeeds, but the worker crashes before receiving confirmation. How should the platform determine whether the operation actually happened?

**Frame.** After a crash, how do we know whether the change actually ran?

**Knowns.** Artifact 4: progress is saved after each step, and crashed work resumes from its last checkpoint.

**Unknowns.** Whether the release process can be queried by execution key.

**Assumptions (provisional).**

- The release process records each change with the key that started it. *Owner: Platform owner. Validated when: a lookup by key works in testing.*

**Answer.**

1. **Record the intent before acting:** "about to execute key K," in durable storage.
2. **After a crash, look it up:** ask the release process whether key K was applied.
3. **Check the service itself:** compare the current version and configuration with the intended target.
4. **Decide:**
    - **Applied:** record it and move on to verification.
    - **Not applied:** retry with the same key, if the approval is still valid.
    - **Unclear:** stop, and ask a person. Never guess.
5. **Record what happened** in the timeline.

**Related requirements:** FR-29, NFR-03.

### Q5.12 The platform contains an AI model, an API, a database, a sandbox and a production execution environment. Which components should be trusted, and which should be treated as untrusted?

**Frame.** Which parts can we rely on, and which must be checked every time?

**Knowns.**

- The model reads hostile-capable data (Q5.1).
- The disposable environment runs untested code.
- Production changes need approval.

**Unknowns.** None that change the answer.

**Assumptions (provisional).** None needed.

**Answer.**

| **Component** | **Trust level** | **Why** |
| --- | --- | --- |
| **Evidence:** logs, alerts, traces, messages | **Untrusted** | Attackers or users can influence its content |
| **AI model output** | **Untrusted** | It may be wrong or manipulated; it is validated before any use |
| **Disposable test environment** | **Untrusted** | It runs generated, untested code; it's isolated, with no route to production |
| **API (intake and engineers)** | **Trusted after checks** | Every caller is authenticated and authorised on every request |
| **Approval service** | **Trusted** | It records and signs approvals from verified people |
| **Executor** | **Trusted, narrowly** | The only component that writes to production; it acts only on a valid, signed approval |
| **Verifier** | **Trusted** | It checks recovery independently, using monitoring data |
| **Database (incident record)** | **Trusted** | The source of truth; write access only through platform services |
| **Production** | **Protected** | Reached only through the executor and the release process |

**Rule:** data crosses from untrusted to trusted only through validation. Power never flows from untrusted to trusted.

### Q5.13 The model needs to investigate infrastructure failures. Should it receive direct infrastructure credentials, or should access be mediated?

**Frame.** Does the model get credentials, or go through the platform?

**Knowns.** Artifact 2: read-only access to existing tools, through adapters.

**Unknowns.** None.

**Assumptions (provisional).** None needed.

**Answer.** **Access is mediated; the model never holds credentials.**

- The model asks for evidence through **named tools**, such as "get the error rate for a service over a window."
- The **platform's adapters** hold the credentials. They check that the request is for the incident's service and time window, and they run it read-only.
- The adapters **limit the size** of results, and **redact** secrets and personal data.
- Every tool call is **logged** with the incident ID.

**Why:** credentials in the model's context could be leaked through its output, or misused by injected instructions. Mediation limits what any request can reach.

### Q5.14 An AI agent wants to run arbitrary shell commands to investigate an application. What risks would unrestricted shell access introduce?

**Frame.** What could go wrong if the agent had an open shell?

**Knowns.** Investigation is read-only (Artifact 1 and Artifact 2).

**Unknowns.** None.

**Assumptions (provisional).** None needed.

**Answer.** Risks:

- **Destructive commands:** any of the damage in Q5.3.
- **Reading secrets:** environment variables, key files, cloud metadata.
- **Escaping scope:** reaching other services, hosts or networks.
- **Sending data out:** copying evidence or secrets to outside addresses.
- **Installing tools or persistence:** leaving changes behind.
- **No useful audit:** free-form commands are hard to review or approve.

**Decision:** no shell access in production. Investigation uses a fixed set of read-only tools. Shell commands are allowed only inside the disposable environment.

### Q5.15 The model needs to query application data. Under what circumstances should it be prevented from constructing arbitrary SQL?

**Frame.** When must the model not write its own database queries?

**Knowns.** Application data may contain customer information (Artifact 4).

**Unknowns.** Whether investigations need application data at all in the first version.

**Assumptions (provisional).**

- The first version doesn't need to query application databases. *Owner: SRE lead. Validated when: the test-suite incidents are diagnosed without it.*

**Answer.** The model must not construct arbitrary queries:

- **against production databases:** a heavy query can slow production; writes could change or delete data.
- **against tables with personal or customer data:** it could expose them.
- **when the query's cost can't be bounded.**
- **when the database account has more than read access.**

**Instead:**

- Use **predefined, parameterised queries** for specific questions (for example, "count recent connection errors"), run by an adapter.
- If needed, use a **read-only replica** with a dedicated, read-only, row-limited account and a statement timeout.

### Q5.16 An agent wants to inspect an external URL referenced in an application log. How could unrestricted URL access expose internal infrastructure?

**Frame.** What's the danger of the agent opening any link it finds?

**Knowns.** Links in logs are attacker-controllable text (Q5.1).

**Unknowns.** None.

**Assumptions (provisional).** None needed.

**Answer.** Risks of unrestricted fetching:

- **Reaching internal systems:** a link to an internal address or the cloud metadata service could return credentials or internal data (server-side request forgery).
- **Sending data out:** a link with incident data in it sends that data to the attacker.
- **Fetching hostile content:** more injected instructions.
- **Probing the network:** using the platform to map internal services.

**Decision:** the agent can't fetch arbitrary links. It may only reach a fixed list of approved tools, through adapters. A link in evidence is recorded as text, and shown to a person.

### Q5.17 The platform provides several operational tools. Which operations should remain read-only?

**Frame.** Which operations must never write?

**Knowns.** Artifact 2: read-only access to monitoring and existing tools.

**Unknowns.** None.

**Assumptions (provisional).** None needed.

**Answer.** **Read-only:**

- metrics, logs and traces
- the error tracker
- deployment and configuration history
- the service catalogue and ownership
- dependency status
- past incidents
- the code repository (read access for evidence)

**Writes allowed, narrowly:**

- the platform's own incident record
- notifications to people
- proposed changes in the review system (never direct commits)
- the disposable test environment
- the executor, through the release process, only after approval

### Q5.18 A generated action needs to modify production state. Which component should have exclusive responsibility for executing writes?

**Frame.** Who is allowed to change production?

**Knowns.** Artifact 4: changes go through the existing release process after approval, and are executed once.

**Unknowns.** None.

**Assumptions (provisional).** None needed.

**Answer.** **Only the executor.**

- It is a separate component, with its own identity and the only write permission to the release process.
- It acts only on a **valid, signed, unexpired approval** whose patch fingerprint matches.
- It **re-checks** the service state before acting.
- It **can't be reached by the model**: the model's output never calls the executor directly.
- Every write is **recorded**, with the approval it used.

### Q5.19 The platform needs to verify that a repair is safe. Which component should control the trusted verification process?

**Frame.** Who decides whether a repair worked?

**Knowns.** Artifact 1: recovery is verified independently, against the original symptom.

**Unknowns.** None.

**Assumptions (provisional).** None needed.

**Answer.** **A separate verifier**, independent of the model and the executor.

- It uses **recovery criteria recorded before execution**.
- It reads **monitoring data directly**, not reports from the executor or the model.
- It requires the result to **hold for the set period**.
- It can **trigger the undo** and escalate.
- A person **accepts** the result.

### Q5.20 An engineer disputes an action performed by the AI. How should the platform establish an auditable record of who authorized and executed it?

**Frame.** How do we prove who approved and ran an action?

**Knowns.** Artifact 4: timeline entries can't be edited or deleted.

**Unknowns.** How long audit records must be kept.

**Assumptions (provisional).**

- 1 year is enough. *Owner: Security lead. Validated when: confirmed with compliance.*

**Answer.** For each action, the record links:

- the **diagnosis and evidence** that led to it
- the **proposed patch** and its fingerprint
- the **test result** from the disposable environment
- the **approval:** who, when, what fingerprint, which target, signed
- the **execution:** which component, when, which key, and the release process's own reference
- the **verification result**

**Integrity:**

- Records are append-only.
- Each entry includes the hash of the previous one, so any change breaks the chain.
- The audit log is copied to a separate store that platform services can't change.

### Q5.21 An unauthorized person attempts to access the incident dashboard. How should the platform establish their identity?

**Frame.** How do we know who is using the platform?

**Knowns.**

- Artifact 2: the first version has a command-line tool and an HTTP API, no dashboard. The same rules apply to any later interface.
- One organisation.

**Unknowns.** Which identity system the organisation uses.

**Assumptions (provisional).**

- The organisation has a central identity system that supports single sign-on and two-factor authentication. *Owner: Security lead. Validated when: confirmed with IT.*

**Answer.**

- **People:** sign in through the organisation's identity system (single sign-on), with two-factor authentication. The platform keeps no passwords.
- **Short-lived sessions:** tokens expire (provisional: 1 hour), and approvals need a recent sign-in.
- **Systems:** monitoring and the platform's own components have their own identities, with credentials that are rotated.
- **Deny by default:** unauthenticated requests are rejected, and the attempt is logged.
- **Leavers lose access** as soon as they are removed from the identity system.

### Q5.22 An authenticated engineer tries to modify a service they do not own. How should the platform enforce permissions?

**Frame.** How do we stop a signed-in engineer acting outside their services?

**Knowns.** Each service has owners (Q5.6).

**Unknowns.** The organisation's roles.

**Assumptions (provisional).**

- Permissions can be expressed as roles per service. *Owner: Security lead. Validated when: the roles are mapped for in-scope services.*

**Answer.**

- **Roles per service:** viewer, responder (opens incidents, adds notes), approver, incident lead.
- **Checked on every request**, by the API, not only in the interface.
- **Checked again by the executor**, before any change.
- **Least privilege:** people get only the roles they need.
- **Denials are logged.**

### Q5.23 Malicious text appears inside application logs and is included in an AI prompt. How should the system prevent this text from becoming an instruction?

**Frame.** What concrete controls stop injected text from steering the platform?

**Knowns.** Q5.1 and Q5.2 set out the threat and the separation approach.

**Unknowns.** The model's measured resistance.

**Assumptions (provisional).**

- The combined controls keep the injection success rate at zero for actions, even if the model's text is sometimes influenced. *Owner: Security lead. Validated when: the injection test set produces no unsafe action.*

**Answer.** Controls, grouped by where they act:

- **Before the model:**
    - send extracts, not raw logs
    - redact secrets
    - flag instruction-like text
    - wrap evidence as labelled data
- **In the request:** platform instructions stay separate, and state that evidence is never to be followed.
- **After the model:**
    - strict structure checks
    - every claim must cite evidence
    - actions must be on the allowed list
    - links and commands in output are rejected
- **Around the model:**
    - no credentials
    - no direct tool power beyond read-only adapters
    - no direct path to the executor
- **Before any action:** testing in the disposable environment, human approval, and fingerprint checks.

**The key point:** even if injected text changes what the model writes, it can't change what the platform does.

### Q5.24 A user attempts to investigate a service outside their authorized scope. How should the platform restrict target access?

**Frame.** How do we keep an investigation within the services the user may see?

**Knowns.** The incident names one service (Artifact 4).

**Unknowns.** How to handle dependencies owned by other teams.

**Assumptions (provisional).**

- Reading the health of other teams' dependencies is acceptable, but not their detailed logs. *Owner: Security lead. Validated when: agreed with service owners.*

**Answer.**

- **Scope each investigation** to the incident's service, environment and time window.
- **Adapters enforce the scope:** queries outside it are refused.
- **Dependencies:** read health and status only; detailed logs need the owning team's permission.
- **Viewing the incident** follows the user's permissions (Q5.22).
- **Refusals are logged.**

### Q5.25 AI-generated code needs to be executed for testing. How should it be isolated from the host environment?

**Frame.** How do we run generated code safely?

**Knowns.** Artifact 2 and Artifact 4: a fresh, isolated, short-lived environment, destroyed after use, with no production access.

**Unknowns.** The isolation technology (decided in Layer 6).

**Assumptions (provisional).**

- The chosen isolation prevents escape to the host. *Owner: Security lead. Validated when: escape tests (ST-27) pass.*

**Answer.**

- **Fresh for each test,** destroyed afterwards.
- **No production network access:** outbound traffic is blocked, except to approved package mirrors.
- **No real credentials:** only fake test values.
- **Least privilege inside:** not running as an administrator, a read-only base system, no access to the host.
- **Resource limits:** CPU, memory, disk and time.
- **Only the results leave:** test output is scanned for secrets, then copied out.

### Q5.26 A model accidentally returns a secret in its response. How should the platform prevent that secret from being exposed or stored insecurely?

**Frame.** What happens if a secret appears in model output?

**Knowns.** Evidence is redacted before reaching the model (Artifact 4), but redaction can miss things.

**Unknowns.** None.

**Assumptions (provisional).** None needed.

**Answer.**

1. **Scan model output** for secret patterns before storing, showing or sending it.
2. **If found, mask it** in the stored output and every display.
3. **Find where it came from:** trace it back to the evidence, and fix the redaction rule.
4. **Treat it as exposed:** alert security, and rotate the secret.
5. **Check the model provider's handling:** confirm that requests aren't kept or used for training (Layer 2's provider choice).
6. **Record the event** without recording the secret itself.

### Q5.27 An attacker attempts to modify an approval request after authorization. How should the system verify the integrity of the original approval?

**Frame.** How do we prove an approval hasn't been tampered with?

**Knowns.** Q5.8: approvals carry the patch fingerprint, and are signed.

**Unknowns.** None.

**Assumptions (provisional).** None needed.

**Answer.**

- **Sign the whole approval:** approver, action, target, patch fingerprint, time and expiry, signed by the approval service. The signing key is held only there.
- **The executor verifies the signature**, and that every field matches what it's about to do.
- **Store approvals append-only:** a change is a new record, never an edit.
- **Any mismatch** stops execution, voids the approval, and alerts security.

### Q5.28 An administrator needs to investigate a suspicious production action. How should every consequential operation be recorded?

**Frame.** What must be recorded so a suspicious action can be investigated?

**Knowns.** Q5.20 sets out the audit record.

**Unknowns.** Retention and who may read the audit log.

**Assumptions (provisional).**

- Only security staff and incident leads read the full audit log. *Owner: Security lead. Validated when: agreed with the engineering manager.*

**Answer.** Record every consequential operation:

- sign-ins, failures, and permission changes
- incident creation and cancellation
- evidence tool calls (what was asked, by which incident)
- model calls (input fingerprint, output, cost)
- patch generation and testing
- approval requests, approvals and rejections
- executions, verifications, and undos

**Each record includes:**

- who or what acted, and when
- the incident ID
- what was asked, and what resulted
- links to the related records

**Protection:**

- append-only, with a chain of hashes
- copied to a separate store
- access-controlled, and searchable by incident, person or service
