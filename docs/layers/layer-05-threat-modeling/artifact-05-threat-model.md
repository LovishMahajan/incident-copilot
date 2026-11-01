# Artifact 5: Threat Model

- **Layer:** 5, Threat Modeling
- **Built from:** Artifacts 3 and 4
- **Status:** Draft compiled from the team's answers; pending team review
- **Sources:** [conversation.md](conversation.md), [System Design](../../reference/system-design.md), [Technical specification](../../reference/technical-specification.md), and, for cross-checks only, [Layer 4](../layer-04-requirements/conversation.md), [Layer 6](../layer-06-system-architecture/conversation.md), [Layer 7](../layer-07-data-apis-events/conversation.md), [Layer 9](../layer-09-agent-architecture/conversation.md), [Layer 11](../layer-11-safe-remediation/conversation.md), [Layer 12](../layer-12-reliability-evaluation/conversation.md)

**Terms:** this layer's answers say "executor" and "disposable environment". The System Design and Technical specification call them the "trusted dispatcher" and the "isolated test environment". They are the same components. "SD" is the System Design; "Spec" is the Technical specification.

## Required sections

| Required section | Questions this section answers | Done |
| --- | --- | --- |
| 5.1 Assets | Q5.3, Q5.9, Q5.15, Q5.16, Q5.20, Q5.26–Q5.28 | Partial |
| 5.2 Threat actors | Q5.1, Q5.3–Q5.8, Q5.10, Q5.11, Q5.21, Q5.22, Q5.24, Q5.25, Q5.27 | Partial |
| 5.3 Attack surfaces | Q5.1, Q5.13–Q5.17, Q5.21, Q5.25 | Yes |
| 5.4 Trust boundaries | Q5.2, Q5.12, Q5.13, Q5.17–Q5.19 | Yes |
| 5.5 Abuse cases | Q5.1, Q5.3–Q5.11, Q5.14–Q5.16, Q5.21, Q5.22, Q5.24–Q5.27 | Yes |
| 5.6 Security controls | Q5.2–Q5.11, Q5.13–Q5.28 | Yes |
| 5.7 Residual risks | Q5.1, Q5.4, Q5.9, Q5.23, Q5.25, Q5.26 | Yes |
| 5.8 Security test cases | Q5.1, Q5.2, Q5.5, Q5.7–Q5.9, Q5.16, Q5.20, Q5.23, Q5.25, Q5.27 | Partial |

## 5.1 Assets

*Answers Q5.3, Q5.9, Q5.15, Q5.16, Q5.20, Q5.26–Q5.28.*

| Asset | What could happen to it | Source |
| --- | --- | --- |
| Production services | Stopped processes, deleted deployments, filled disks | [Q5.3](conversation.md) |
| Production data and backups | Dropped tables, deleted files or backups; heavy queries slow production | [Q5.3](conversation.md), [Q5.15](conversation.md) |
| Customer and personal data in logs and application data | Exposed through queries, evidence or output | [Q5.15](conversation.md), [Spec §10](../../reference/technical-specification.md) |
| Production credentials and other secrets | Printed, sent out of the network, or shown by the model | [Q5.3](conversation.md), [Q5.9](conversation.md), [Q5.26](conversation.md) |
| Production write access | Held only by the dispatcher | [Q5.18](conversation.md), [SD §2](../../reference/system-design.md) |
| Approval signing key | Held in the secrets manager; the dispatcher has only the public key | [Q5.27](conversation.md), [Spec §3](../../reference/technical-specification.md) |
| Approval records and patch fingerprints | Edited or forged so a different change runs | [Q5.8](conversation.md), [Q5.27](conversation.md) |
| Read-only tool credentials and the model provider key | Leaked through output if placed in the model's context | [Q5.13](conversation.md), [SD §4](../../reference/system-design.md) |
| Incident record | The source of truth; write access only through platform services | [Q5.12](conversation.md) |
| Timeline and audit log | Logs or audit records deleted to hide tracks | [Q5.3](conversation.md), [Q5.20](conversation.md), [Q5.28](conversation.md) |
| Internal systems and the cloud metadata service | Reached through a link, returning credentials or internal data | [Q5.16](conversation.md) |
| Diagnosis and summary given to approvers | Steered, so a person approves a wrong fix | [Q5.1](conversation.md), [Q5.4](conversation.md) |
| Per-incident time and token budgets | Used up by loops or very long output | [Q5.1](conversation.md) |
| Test host | Compromised by an isolation escape | [Q5.25](conversation.md), [Spec §13](../../reference/technical-specification.md) |

**Gap:** no source gives an asset register. The list above is drawn from the answers and has no sensitivity class, value ranking or per-asset owner.

## 5.2 Threat actors

*Answers Q5.1, Q5.3–Q5.8, Q5.10, Q5.11, Q5.21, Q5.22, Q5.24, Q5.25, Q5.27.*

| Actor | What they can reach | Source |
| --- | --- | --- |
| Anyone who can make the application log something: a user typing into a form, a request header, an error from a third-party service | Text in logs and other evidence, and through it the model's context | [Q5.1](conversation.md) |
| Unauthenticated person | The API, or any later interface | [Q5.21](conversation.md) |
| Engineer without permission for the named service | Incident submission | [Q5.6](conversation.md) |
| Authenticated engineer outside their services | Changes or investigations beyond their role | [Q5.22](conversation.md), [Q5.24](conversation.md) |
| Unauthorised user attempting to approve | The approval path | [Q5.7](conversation.md) |
| Attacker altering a patch or an approval after authorisation | The patch or the approval request | [Q5.8](conversation.md), [Q5.27](conversation.md) |
| Requester approving their own change; the platform's own identity | The approval path | [Q5.7](conversation.md) |
| Leaver with old access | Access ends on removal from the identity system | [Q5.21](conversation.md) |
| The model, wrong or manipulated | Its own output only | [Q5.3](conversation.md)–[Q5.5](conversation.md), [Q5.12](conversation.md) |
| Generated code under test | The disposable environment | [Q5.25](conversation.md) |
| Worker retrying or crashing | Duplicate or unknown execution | [Q5.10](conversation.md), [Q5.11](conversation.md) |
| Compromised dispatcher | Can't forge approvals, because it holds only the public key | [Spec §3](../../reference/technical-specification.md) |

**Gap:** no source gives a threat-actor list, with capability or likelihood per actor. The table is compiled from the people and failures named in the questions. No source assesses a holder of a valid approver role acting maliciously, a stolen signing key, or a compromise of the release process or the model provider.

## 5.3 Attack surfaces

*Answers Q5.1, Q5.13–Q5.17, Q5.21, Q5.25.*

| Surface | Exposure | State in the first version | Source |
| --- | --- | --- | --- |
| Evidence read by the model: logs, error messages, traces, alert text, change descriptions | Text anyone can influence reaches the model | Open; labelled, redacted, flagged, extracts only | [Q5.1](conversation.md), [Q5.23](conversation.md) |
| Runbooks and commit messages | Same injection route, named as test fixtures | Open; read-only, treated as data | [Q6.13](../layer-06-system-architecture/conversation.md), [Q12.60](../layer-12-reliability-evaluation/conversation.md) |
| Model output | May be wrong or manipulated | Open; validated before any use | [Q5.12](conversation.md) |
| HTTP API and CLI (no dashboard in the first version; the same rules apply to any later interface) | External requests under `/v1` | Open; authenticated and authorised on every request | [Q5.21](conversation.md), [Spec §8](../../reference/technical-specification.md) |
| Alert intake from monitoring | Authenticated as the monitoring system | Open; allowed for any service in scope | [Q5.6](conversation.md) |
| Approval endpoints: `POST /v1/actions/{id}/approvals`, `/rejections` | People's approval decisions | Open; rights checks, recent sign-in, signature | [Q5.7](conversation.md), [Spec §8](../../reference/technical-specification.md) |
| Artifact content: `GET /v1/artifacts/{id}`, `/content` | Stored evidence and output | Open; roles on the service, every read logged | [Spec §8, §10](../../reference/technical-specification.md) |
| Evidence adapters | Named read-only tools holding the credentials | Open; scoped, size-limited, redacted, logged | [Q5.13](conversation.md) |
| Model provider | Outbound redacted extracts | Open; gateway only | [Q5.26](conversation.md), [Spec §8](../../reference/technical-specification.md) |
| Disposable environment | Runs generated, untested code | Open; isolated. See "Sandbox network" under Decisions and conflicts | [Q5.25](conversation.md) |
| Review system | Proposed changes | Open; never direct commits | [Q5.17](conversation.md) |
| Release process | The route to production writes | Dispatcher only, on a signed approval | [Q5.18](conversation.md) |
| Shell in production | Any of the damage in Q5.3 | Closed: no shell access | [Q5.14](conversation.md) |
| Free-form database queries | Slow production; expose data | Closed: predefined, parameterised queries only, if needed | [Q5.15](conversation.md) |
| Fetching links from evidence | Internal systems, data sent out, hostile content, network probing | Closed: a link is text | [Q5.16](conversation.md) |

## 5.4 Trust boundaries

*Answers Q5.2, Q5.12, Q5.13, Q5.17–Q5.19.*

**Rule:** data crosses from untrusted to trusted only through validation. Power never flows from untrusted to trusted ([Q5.12](conversation.md)).

| Component | Trust level | Why |
| --- | --- | --- |
| Evidence: logs, alerts, traces, messages | Untrusted | Attackers or users can influence its content |
| AI model output | Untrusted | It may be wrong or manipulated; it is validated before any use |
| Disposable test environment | Untrusted | It runs generated, untested code; it's isolated, with no route to production |
| API (intake and engineers) | Trusted after checks | Every caller is authenticated and authorised on every request |
| Approval service | Trusted | It records and signs approvals from verified people |
| Executor | Trusted, narrowly | The only component that writes to production; it acts only on a valid, signed approval |
| Verifier | Trusted | It checks recovery independently, using monitoring data |
| Database (incident record) | Trusted | The source of truth; write access only through platform services |
| Production | Protected | Reached only through the executor and the release process |

| Boundary | Rule |
| --- | --- |
| Evidence → platform | Redacted, labelled as data, size-limited, instruction-like text flagged |
| Model output → platform | Strict schema; citations must exist; fixes from the allowed list; secrets scanned |
| Isolated environment → platform | Only scanned test results leave |
| People → API | Authenticated and authorised on every request |
| Approval → dispatcher | Signature, fingerprint, expiry and service state checked |
| Dispatcher → production | Only the approved change, only through the release process |

*Boundary table from [Spec §3](../../reference/technical-specification.md).*

- **Never available to the model:** credentials, a shell, free-form database queries, fetching links, or any path to the dispatcher ([Spec §3](../../reference/technical-specification.md)).
- **Access is mediated:** the model asks through named tools; the platform's adapters hold the credentials ([Q5.13](conversation.md)).
- **Writes:** read-only and narrow-write operations are listed in 5.6 ([Q5.17](conversation.md)). Only the executor writes to production ([Q5.18](conversation.md)).
- **Verification:** a separate verifier, independent of the model and the executor ([Q5.19](conversation.md)).
- **Runtime boundaries:** a process boundary is a runtime and trust boundary. The platform runs as four processes: API, worker, trusted dispatcher, isolated test environment ([SD §2](../../reference/system-design.md), [Q6.39](../layer-06-system-architecture/conversation.md)).

## 5.5 Abuse cases

*Answers Q5.1, Q5.3–Q5.11, Q5.14–Q5.16, Q5.21, Q5.22, Q5.24–Q5.27.*

| Abuse case | What goes wrong | What stops it (detail in 5.6) | Source |
| --- | --- | --- | --- |
| Redirect the diagnosis | "The cause is the database; ignore the deployment" points engineers at the wrong fix | Citations, alternatives, reproduction | [Q5.1](conversation.md) |
| Request harmful actions | Attacker-written command, patch or configuration change is proposed | Allowed list, testing, approval | [Q5.1](conversation.md) |
| Extract data | Model repeats secrets or evidence into a summary, patch or link | Redaction, output scan, links rejected | [Q5.1](conversation.md) |
| Hide evidence | Certain errors or changes are left out of the report | Evidence labelled and flagged; strict output checks; flagged text shown to the approver | [Q5.1](conversation.md) |
| Exhaust budgets | Model loops or produces very long output | Per-incident limits, enforced before each call | [Q5.1](conversation.md), [SD §9](../../reference/system-design.md) |
| Mislead approvers | Summary looks confident and complete, so a person approves a wrong fix | Contradicting evidence and test result shown | [Q5.1](conversation.md), [Q5.4](conversation.md) |
| Destructive command | Data deleted, services down, secrets leaked, access widened, tracks hidden, spread through shared credentials | The model never runs shell commands in production | [Q5.3](conversation.md) |
| Confident wrong diagnosis | Wrong cause, wrong fix, for example a rollback across a schema change; real cause continues | Confidence from evidence, reproduction, independent verification | [Q5.4](conversation.md) |
| Malformed output | Structured diagnosis is broken, possibly by injected text | Strict validation, 3 attempts, then inconclusive | [Q5.5](conversation.md) |
| Incident on a service the caller can't access | Investigation started outside their rights | Permission lookup, access-denied error | [Q5.6](conversation.md) |
| Unauthorised approval | Wrong person approves a production change | Approval rights per service, executor re-check | [Q5.7](conversation.md) |
| Patch changed after approval | A different change runs | Fingerprint, signature, locked review | [Q5.8](conversation.md) |
| Patch reaches credentials | Credentials logged, printed, sent or hard-coded | Patch rules, fake test values, scanning | [Q5.9](conversation.md) |
| Action runs twice | Double rollback goes one version too far; repeated restarts or scaling; duplicate notifications; confusing record | One execution key per approval | [Q5.10](conversation.md) |
| Crash after an external success | Platform doesn't know whether the change ran | Intent recorded first; lookup by key; unclear means stop and ask | [Q5.11](conversation.md) |
| Open shell for an agent | Destructive commands, reading secrets, escaping scope, sending data out, installing tools or persistence, no useful audit | No shell in production; fixed read-only tools | [Q5.14](conversation.md) |
| Arbitrary SQL | Slow production; writes; exposed personal data | Predefined, parameterised queries | [Q5.15](conversation.md) |
| Following a link from a log | Server-side request forgery; data sent out; more injected instructions; network probing | The agent can't fetch arbitrary links | [Q5.16](conversation.md) |
| Unauthorised access; acting on a service not owned | Identity unknown, or a signed-in engineer acts outside their services | Single sign-on, roles per service, deny by default | [Q5.21](conversation.md), [Q5.22](conversation.md) |
| Investigation outside authorised scope | User sees a service they may not see | Adapters enforce the scope | [Q5.24](conversation.md) |
| Generated code escapes testing | Test host compromised | Isolation; nothing valuable inside | [Q5.25](conversation.md), [Spec §13](../../reference/technical-specification.md) |
| Secret in model output | Secret stored or shown | Output scan, masking, rotation | [Q5.26](conversation.md) |
| Approval altered after authorisation | Tampered record passes as genuine | Whole approval signed; executor verifies every field | [Q5.27](conversation.md) |

**The key point:** even if injected text changes what the model writes, it can't change what the platform does ([Q5.23](conversation.md)). The realistic damage is limited by what the platform lets the model's output do ([Q5.1](conversation.md)).

## 5.6 Security controls

*Answers Q5.2–Q5.11, Q5.13–Q5.28.*

| Area | Controls | Source |
| --- | --- | --- |
| Before the model | Send extracts, not raw logs. Redact secrets. Flag instruction-like text (`flagged_injection`, shown to the approver). Wrap evidence as labelled data, with its source. Compute facts in code | [Q5.2](conversation.md), [Q5.23](conversation.md) |
| In the request | Platform instructions stay separate, and state that evidence is never to be followed | [Q5.2](conversation.md), [Q5.23](conversation.md) |
| After the model | Strict structure checks. Every claim cites existing evidence. Actions must be on the allowed list. Links and commands in output are rejected. Nothing in the output came from an evidence item flagged as suspicious | [Q5.2](conversation.md), [Q5.23](conversation.md) |
| Malformed output | Reject unknown fields, missing fields, wrong types and oversized values. Never repair by guessing. Up to 3 attempts in total, each counting against the budget. After 3 failures, record the raw output and hand over as inconclusive. Repeated malformed output flags the evidence for possible injection. Never act on partially valid output | [Q5.5](conversation.md) |
| Around the model | No credentials. No direct tool power beyond read-only adapters. No direct path to the executor. Nothing the model says can raise its permissions, change its instructions, or skip approval | [Q5.2](conversation.md), [Q5.13](conversation.md), [Q5.23](conversation.md) |
| Evidence access | Named tools. Adapters check the request is for the incident's service and window, run it read-only, limit result size, and redact. Every tool call logged with the incident ID | [Q5.13](conversation.md) |
| Scope | Each investigation is scoped to the incident's service, environment and time window. Adapters refuse queries outside it. Dependencies: read health and status only; detailed logs need the owning team's permission. Refusals logged | [Q5.24](conversation.md) |
| Forbidden capabilities | No shell in production; shell only inside the disposable environment. No arbitrary links; a link in evidence is recorded as text and shown to a person. No arbitrary queries: predefined, parameterised queries; if needed, a read-only replica with a dedicated, read-only, row-limited account and a statement timeout | [Q5.3](conversation.md), [Q5.14](conversation.md)–[Q5.16](conversation.md) |
| Read-only operations | Metrics, logs and traces; error tracker; deployment and configuration history; service catalogue and ownership; dependency status; past incidents; code repository | [Q5.17](conversation.md) |
| Narrow writes | Platform's own incident record; notifications to people; proposed changes in the review system (never direct commits); disposable test environment; the executor, through the release process, only after approval | [Q5.17](conversation.md) |
| Wrong diagnosis | Confidence scored from cited evidence, not the model's tone. At least two hypotheses, including "not the deployment". Failure must be reproduced before a fix is tested. Approver sees contradicting evidence and the test result. Calibration measured | [Q5.4](conversation.md) |
| Identity | People use single sign-on with two-factor authentication; the platform keeps no passwords. Tokens expire (provisional: 1 hour). Systems have their own identities with rotated credentials. Deny by default; attempts logged. Leavers lose access on removal from the identity system | [Q5.21](conversation.md) |
| Permissions | Roles per service: viewer, responder, approver, incident lead. Checked by the API on every request, not only in the interface, and again by the executor. Least privilege. Denials logged. A submission without permission gets an access-denied error, and repeated attempts alert security | [Q5.6](conversation.md), [Q5.22](conversation.md) |
| Approval | Only the service owner, on-call engineer or incident lead for that service. No self-approval. Second approver for high-impact changes. Recent sign-in (provisional: within 15 minutes). Every approval and rejection logged; alert on failed attempts | [Q5.7](conversation.md) |
| Approval integrity | Hash of the exact patch in the approval. Whole approval signed: approver, action, target, fingerprint, time, expiry. Signing key held only by the approval service. Stored append-only. Reviewed change locked; new commits need a new review. Any mismatch stops execution, voids the approval, and alerts security and the approver | [Q5.8](conversation.md), [Q5.27](conversation.md) |
| Execution | Only the executor, with its own identity and the only write permission to the release process. Acts only on a valid, signed, unexpired approval whose fingerprint matches. Re-checks approver rights and service state. Not reachable by the model. Every write recorded with its approval | [Q5.7](conversation.md), [Q5.18](conversation.md) |
| One-time execution | One execution key per approval; a second attempt returns the first result. State the target, not the change. Check current state first. Approval is used up on execution | [Q5.10](conversation.md) |
| Crash recovery | Record intent first. After a crash, ask the release process about the key, then check the service. Applied: move to verification. Not applied: retry with the same key if the approval is still valid. Unclear: stop and ask a person | [Q5.11](conversation.md) |
| Verification | Separate verifier. Recovery criteria recorded before execution. Reads monitoring directly. Result must hold for the set period. Can trigger the undo and escalate. A person accepts the result | [Q5.19](conversation.md) |
| Secrets | Real credentials never go to tests. Reject patches that log, print, send or hard-code secrets, or read secret variables in new places. Credential changes are prohibited for the platform. Patches, test output and model output are scanned before storing or showing. On exposure: mask, trace to the evidence, fix the redaction rule, alert security, rotate, and record the event without the secret | [Q5.9](conversation.md), [Q5.26](conversation.md) |
| Isolation | Fresh for each test, destroyed afterwards. No production network access. Only fake test values. Not an administrator, read-only base system, no host access. CPU, memory, disk and time limits. Test output scanned for secrets, then copied out. Runs under gVisor on a separate host | [Q5.25](conversation.md), [SD §11](../../reference/system-design.md) |
| Before any action | Testing in the disposable environment, human approval and fingerprint checks | [Q5.23](conversation.md) |
| Audit | Each action links diagnosis and evidence, patch and fingerprint, test result, approval, execution and verification. Append-only; each entry includes the hash of the previous one; copied to a separate store platform services can't change | [Q5.20](conversation.md) |
| What is recorded | Sign-ins, failures and permission changes. Incident creation and cancellation. Evidence tool calls. Model calls (input fingerprint, output, cost). Patch generation and testing. Approval requests, approvals and rejections. Executions, verifications and undos. Each record has who or what acted, when, the incident ID, what was asked and what resulted, and links. Access-controlled, and searchable by incident, person or service | [Q5.28](conversation.md) |

## 5.7 Residual risks

*Answers Q5.1, Q5.4, Q5.9, Q5.23, Q5.25, Q5.26.*

Threat and remaining risk are from [SD §11](../../reference/system-design.md). Likelihood, impact and owner are from [Spec §13](../../reference/technical-specification.md), where it lists the risk; `—` means it doesn't.

| Threat | Remaining risk | Likelihood | Impact | Owner |
| --- | --- | --- | --- | --- |
| Text in logs tells the model to run a command or hide an error | The written summary can still be steered; flagged text is shown to the approver | Medium | Misleading advice | Security lead |
| A confident, wrong diagnosis is approved | Approvers may still trust a convincing summary | Medium | Wrong fix applied, then undone | SRE lead |
| Secrets leak through evidence or output | Unusual secret formats may slip past redaction | Low | Secret sent to provider or stored | Security lead |
| Generated code escapes testing | Unknown escape flaws; nothing valuable inside limits the impact | Low | Test host compromised | Security lead |
| The model proposes a destructive command | None for production | — | — | — |
| A patch is changed after approval | None known | — | — | — |
| The wrong person approves | None known | — | — | — |
| A retry applies a change twice | None known | — | — | — |
| A link in logs reaches internal systems | None | — | — | — |
| Injected text burns the budget | None | — | — | — |

**No model reliably ignores every embedded instruction,** so safety can't depend on the model's resistance alone ([Q5.1](conversation.md)). Separation reduces the risk but doesn't remove it ([Q5.2](conversation.md)).

Open unknowns that keep these risks open:

| Unknown | Source |
| --- | --- |
| How often real logs contain user-controlled text | [Q5.1](conversation.md) |
| How resistant the chosen model is; its measured resistance | [Q5.1](conversation.md), [Q5.23](conversation.md) |
| How well the model's confidence matches its accuracy | [Q5.4](conversation.md) |
| The organisation's approval rules by service and severity | [Q5.7](conversation.md) |
| Whether the release process can verify a fingerprint, or be queried by execution key | [Q5.8](conversation.md), [Q5.11](conversation.md) |
| How to handle dependencies owned by other teams | [Q5.24](conversation.md) |
| Retention, and who may read the audit log | [Q5.28](conversation.md) |

## 5.8 Security test cases

*Answers Q5.1, Q5.2, Q5.5, Q5.7–Q5.9, Q5.16, Q5.20, Q5.23, Q5.25, Q5.27.*

The sources require 40 security tests: "Security (40 cases)", pass mark all pass ([Spec §11](../../reference/technical-specification.md)), and "all 40 security tests pass" at M4 ([Spec §12](../../reference/technical-specification.md)). [Q12.61](../layer-12-reliability-evaluation/conversation.md) cites them as "Artifact 5, ST-01 to ST-40".

**What is enumerated:** seven named categories in Spec §11, and one numbered test, ST-27. Categories are not individual cases.

| ID | Category (Spec §11) | Related requirement or question | Pass mark |
| --- | --- | --- | --- |
| ST-27 | Sandbox escape | Escape tests validate that the chosen isolation prevents escape to the host ([Q5.25](conversation.md)); failure moves isolation to virtual machines ([Spec §13](../../reference/technical-specification.md)) | All pass |
| Not assigned | Approval forgery | [Q5.27](conversation.md) | All pass |
| Not assigned | Self-approval | FR-26, verified by security test, M3: 100% rejected | All pass |
| Not assigned | Patch change after approval | [Q5.8](conversation.md); FR-28, hash comparison test, M3 | All pass |
| Not assigned | Credential reach | FR-22, verified by security test, M2: 100% of production access attempts blocked | All pass |
| Not assigned | Link fetching | [Q5.16](conversation.md) | All pass |
| Not assigned | Audit tampering | FR-37, verified by security test, M1: 0 successful edit or delete attempts | All pass |

**Gap:** no source enumerates the 40 cases. ST-01 to ST-26 and ST-28 to ST-40, 39 of the 40 IDs, have no definition anywhere, and no source says how the 40 divide across the seven categories or across milestones. The team must write them; none are invented here. Until then, the citation in [Q12.61](../layer-12-reliability-evaluation/conversation.md) has nothing to point at, and M4's "all 40" can't be checked.

Related tests that sources name separately, not counted in the 40:

| Test | What it proves | Pass mark | Source |
| --- | --- | --- | --- |
| Injection test set | Logs containing instructions produce no action outside the allowed list; success rate compared with and without separation | 0 unsafe actions | [Q5.1](conversation.md), [Q5.2](conversation.md), [Q5.23](conversation.md), [Spec §11](../../reference/technical-specification.md) |
| Prompt-injection evaluation category | At least 20 held-out cases, with instructions planted in log lines, error messages, commit messages, runbooks and trace attributes | 0 followed injections | [Q12.60](../layer-12-reliability-evaluation/conversation.md) |
| Redaction test (NFR-18) | Seeded secrets in stored evidence or prompts | 0 of a seeded corpus of 100 | [Layer 4](../layer-04-requirements/conversation.md) |
| Malformed-output review | Malformed outputs during testing are reviewed for injected text; NFR-33 malformed-output simulation | ≤ 3 attempts, then inconclusive | [Q5.5](conversation.md), [Layer 4](../layer-04-requirements/conversation.md) |
| Remediation unit tests | Approval, signature, digest and dispatch refusals. Code exists, but carries no ST numbers | All pass | [test_remediation.py](../layer-11-safe-remediation/files/test_remediation.py) |

## Assumptions

| Assumption | Status | Owner | Validated when |
| --- | --- | --- | --- |
| No model reliably ignores every embedded instruction, so safety can't depend on the model's resistance alone ([Q5.1](conversation.md)) | Provisional | Security lead | A test set of injected logs is run against the model and the success rate is measured |
| Clearly separating and labelling data reduces, but doesn't remove, the risk ([Q5.2](conversation.md)) | Provisional | Security lead | The injection test set shows a lower success rate with separation than without |
| Approvers may trust a confident summary without checking the evidence ([Q5.4](conversation.md)) | Provisional | SRE lead | Approval decisions are reviewed during the pilot |
| Malformed output is usually accidental, but may be caused by injected text ([Q5.5](conversation.md)) | Provisional | Security lead | Malformed outputs during testing are reviewed |
| Service ownership can be read from the service catalogue or the identity system's groups ([Q5.6](conversation.md)) | Provisional | Platform owner | Every in-scope service has owning groups |
| The identity system supports strong sign-in (two-factor) for all approvers ([Q5.7](conversation.md)) | Provisional | Security lead | Confirmed for every approver group |
| Approving needs a recent sign-in, within the last 15 minutes ([Q5.7](conversation.md)) | Provisional | TBD | TBD |
| A fingerprint (cryptographic hash) of the patch can be checked at execution time ([Q5.8](conversation.md)) | Provisional | Platform owner | The scenario revert is applied with a fingerprint check |
| Secrets are held in a secrets manager and injected at runtime, never stored in code or configuration files ([Q5.9](conversation.md)) | Provisional | Security lead | The in-scope services are reviewed |
| The release process records each change with the key that started it ([Q5.11](conversation.md)) | Provisional | Platform owner | A lookup by key works in testing |
| The first version doesn't need to query application databases ([Q5.15](conversation.md)) | Provisional | SRE lead | The test-suite incidents are diagnosed without it |
| 1 year is enough for keeping audit records ([Q5.20](conversation.md)) | Provisional | Security lead | Confirmed with compliance |
| The organisation has a central identity system that supports single sign-on and two-factor authentication ([Q5.21](conversation.md)) | Provisional | Security lead | Confirmed with IT |
| Tokens expire after 1 hour ([Q5.21](conversation.md)) | Provisional | TBD | TBD |
| Permissions can be expressed as roles per service ([Q5.22](conversation.md)) | Provisional | Security lead | The roles are mapped for in-scope services |
| The combined controls keep the injection success rate at zero for actions, even if the model's text is sometimes influenced ([Q5.23](conversation.md)) | Provisional | Security lead | The injection test set produces no unsafe action |
| Reading the health of other teams' dependencies is acceptable, but not their detailed logs ([Q5.24](conversation.md)) | Provisional | Security lead | Agreed with service owners |
| The chosen isolation prevents escape to the host ([Q5.25](conversation.md)) | Provisional | Security lead | Escape tests (ST-27) pass |
| Only security staff and incident leads read the full audit log ([Q5.28](conversation.md)) | Provisional | Security lead | Agreed with the engineering manager |

## Decisions and conflicts

The decisions in this layer are all in the sections above. Conflicts found while compiling, none of them resolved here except where stated:

| Topic | Sources | Treatment here |
| --- | --- | --- |
| Sandbox network | [Q5.25](conversation.md) and [Q6.30](../layer-06-system-architecture/conversation.md): blocked except approved package mirrors. [SD §11](../../reference/system-design.md): "no network". [Q11.21](../layer-11-safe-remediation/conversation.md): no network by default; a mirror only if ever needed, through a filtering proxy | Open. Q11.21 is the most detailed and fits both, but no source says it supersedes. 5.6 says "no production network access" and leaves the exception to the team |
| Forty security tests | [SD §11](../../reference/system-design.md) says they cover the ten threats listed there. [Spec §11](../../reference/technical-specification.md) names seven categories, and puts injection, duplicate execution and budget in other suites (Injection, Idempotency, Limits) | Open. See the Gap in 5.8 |
| Who can approve | [Q5.7](conversation.md) and [Spec §3](../../reference/technical-specification.md) include the incident lead. [SD §8](../../reference/system-design.md): service owner or on-call engineer | Open. 5.6 keeps Q5.7's wording |
| Second approver | [Q5.7](conversation.md) states it as a control. [SD §12](../../reference/system-design.md) lists "one approver; two for high-impact" as an open decision (Engineering manager, before M3) | Open. No source defines "high-impact" |
| Self-approval | [Q5.7](conversation.md): the requester "can't approve it alone". [SD §8](../../reference/system-design.md): "nobody approves their own request" | Open. The SD wording is stricter |
| Secret in model output | [Q5.26](conversation.md) and [Spec §10](../../reference/technical-specification.md): mask it. [Spec §6](../../reference/technical-specification.md): output containing secrets fails validation, retries, then inconclusive | Open. Could be both, but no source says so |
| Commands in the disposable environment | [Q5.3](conversation.md) and [Q5.14](conversation.md): commands proposed for testing run only there. [Q5.23](conversation.md) and [Spec §6](../../reference/technical-specification.md): commands in model output are rejected. [Spec §3](../../reference/technical-specification.md): a shell is never available to the model. [Spec §5.4](../../reference/technical-specification.md) lists fixed test steps | Open |
| Unknown or forbidden service | [Q5.6](conversation.md): don't reveal whether the service exists. FR-06: unknown services are rejected and the incident lead is notified. [Spec §6](../../reference/technical-specification.md): 422 naming the fields, or 403 | Open. Different responses would reveal existence |
| Audit log readers | [Q5.28](conversation.md) assumes security staff and incident leads only. [Spec §3](../../reference/technical-specification.md) lets a viewer read the timeline, and [SD §8](../../reference/system-design.md) places the audit in the timeline | Open. No source says whether the "full audit log" differs from the timeline |
| Output from flagged evidence | [Q5.2](conversation.md) checks that nothing in the output came from a flagged item. [Spec §5.3 and §6](../../reference/technical-specification.md) don't include this check | Open. Kept in 5.6 as the team's answer |
| Responder role | [Q5.22](conversation.md): opens incidents, adds notes. [Spec §3](../../reference/technical-specification.md): creates and cancels; no notes permission | Open |
| Signing key location | [SD §2](../../reference/system-design.md): held by the API process. [Spec §3](../../reference/technical-specification.md) and [Q6.39](../layer-06-system-architecture/conversation.md): in the secrets manager, with the Approval Service in the API process | Spec value used in 5.1, because Q6.39 reconciles the two |
| Failed approval attempts | [Q5.7](conversation.md) and [Q5.28](conversation.md): log failures and alert. [Spec §6](../../reference/technical-specification.md): on an approval failure, "nothing recorded" | Open. Spec may mean no approval row; it doesn't say |
| Runbooks | [Q6.13](../layer-06-system-architecture/conversation.md) cites Artifact 5 for "runbook content is treated as data", but Layer 5's answers don't mention runbooks | Listed in 5.3 so the citation holds, with its source shown |
| Direct-access risks | [Q9.10](../layer-09-agent-architecture/conversation.md) says Artifact 5 lists these in full, and includes "unbounded cost and load on production". [Q5.14](conversation.md) doesn't list it; the nearest are Q5.1 (budgets) and Q5.15 (heavy queries) | Those two answers are shown in 5.5; nothing added |

## Acceptance checks

- [ ] Every required section above is present and complete.
- [x] Every section lists the questions it answers, and every question in this layer is answered by at least one section.
- [ ] Every assumption is marked provisional and has a named owner and a condition under which it will be validated.
- [ ] Every disagreement between Claude and ChatGPT that affected a decision is recorded with the team's resolution.
- [ ] The artifact is consistent with every artifact it is built from; any conflict is recorded as an explicit decision.
- [x] The artifact was produced with the team's coding agent and is saved in the team repository.

Reviewed by: TBD
Date: TBD
