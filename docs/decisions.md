# Decision Register

The one decision log the worksheet asks for (Q2.23). It settles the cross-layer gaps and conflicts found in Artifacts 1–13.

**Status of everything here: Proposed (2026-11-02, amended through 2026-11-17), pending team sign-off.** Owners are roles, because the sources name no people. Each decision can be overturned in review; change it here first, then in the artifacts it lists.

## How to read the docs

When two documents disagree, the higher one wins:

1. This register
2. The layer artifacts (`artifact-*.md`)
3. The [System Design](reference/system-design.md) and [Technical specification](reference/technical-specification.md), as amended by the notes at their top
4. `conversation.md`: the team's original answers, kept unchanged as the record of how each decision was reached
5. Code: `src/` is the code the team builds on (ADR-67). `files/` in each layer is the filed copy of the code that artifact was written against. The Room 7 originals are in git commit 013bcf4

Files marked "proposed" in a layer's `files/` folder, such as `schema.sql`, are new specifications written to close gaps. They are not part of the team's Room 7 tabs.

## Decisions

Fields are a short form of Q2.23, because links to assumptions and validation conditions sit in each artifact's Assumptions table. "Reversal" is how hard the decision is to undo. Each row in "Revisit when" is the trigger that reopens it.

### ADR-27: Only the Model Gateway retries model calls

Formalises a decision Layer 10 cites but no document records. The gateway owns the 3 attempts for every model failure, and returns failures as values. Temporal re-runs a model activity once only when its worker is lost, and the re-run reuses any recorded result (ADR-61).

- **Rationale:** two retry layers multiply calls and spend. **Owner:** SRE lead. **Reversal:** Low. **Revisit when:** a failure class needs retrying above the gateway.
- **Resolves:** Layer 10 C17, Layer 8 C4 (see ADR-52 for crash recovery).

### ADR-37: Keyword retrieval now; vector or hybrid only on measured benefit

Formalises the Layer 12 citation. Adopt vector or hybrid only if it improves end-to-end results without losing exact-identifier recall, and is worth the extra system.

- **Owner:** SRE lead. **Reversal:** Medium. **Revisit when:** the retrieval evaluation in Artifact 10 misses its targets.

### ADR-38: No automatic cross-provider fallback

Formalises the Layer 10 and 12 citation. `allow_cross_provider_fallback = false`. A provider outage gives evidence and a timeline only.

- **Owner:** SRE lead. **Reversal:** Low. **Revisit when:** the second-provider trigger in ADR-56 is met.

### ADR-44: Release boundaries

"First version" now has one meaning: everything built through M4, shipped in two releases. Production write stays impossible until the second is switched on.

| | Release A: Advisory | Release B: Controlled remediation |
| --- | --- | --- |
| Covers | Intake, evidence, diagnosis, review, patch drafting, sandbox testing, recommendation | Approval, dispatcher, rollback, independent verification against production |
| Who changes production | People, by hand | Dispatcher, on a signed approval |
| Production write credential | Not provisioned at all | Provisioned by a recorded decision |
| Incident ending | `testing` → `handed_over` with the tested recommendation; the action goes `testing` → `withdrawn` | `testing` → `awaiting_approval` and onward |
| Built and tested | M1 and M2, on synthetic incidents | M3 and M4, on synthetic targets |
| Goes live | Advisory pilot, after the M4 gates | After the pilot accuracy and approval review (Engineering manager) |

The Approval Service, dispatcher and Verifier are built and tested at M3 on synthetic targets. They run against production only in Release B.

- **Rationale:** the sources describe a phased start. Layer 1 states the end goal, Layer 2 and the System Design state the starting position. **Owner:** Engineering manager. **Reversal:** Low. **Revisit when:** the pilot review is complete.
- **Resolves:** Layer 1 conflict 1, Layer 2 conflicts 1 and 6 (draft pull requests are allowed in both releases; direct commits never).

### ADR-45: One decision scheme

ADR numbers continue from the existing series and this file is the log. Q2.23's `D-04` was an example format, and no D-numbered entry exists. ADR numbers 25, 26, 28, 30–36 and 39–42 were never assigned in any source and stay unused. ADR-29 and ADR-43 stay as Layer 11 defines them.

- **Owner:** Platform owner. **Reversal:** Low.
- **Resolves:** Layer 2 gap in 2.7 and conflict 11, Layer 10 C17.

### ADR-46: Canonical state machines

`states.py` is the authoritative base (14 incident states, 14 action states), with three additions. Everything else (the schema enums, contracts, Technical specification section 2, tests) must match it.

**Added transitions**

| Machine | Transition | Trigger |
| --- | --- | --- |
| Incident | `handed_over` → `investigating` | A person requests re-investigation. New workflow run `incident-<id>-r<N>`, starting at `-r2`. No automatic path exists |
| Incident | `not_recovered` → `reconciling` | A rollback's outcome is unknown |
| Action | `proposed` → `awaiting_approval` | Rollback actions only, which skip `testing` (ADR-48) |

That gives 30 incident transitions and 20 action transitions.

**Rules for existing entries**

- `diagnosed` → `awaiting_approval` from the Technical specification is removed: testing always comes first.
- `→ cancelled` applies from exactly the 6 states in `states.py`. `executing`, `reconciling`, `verifying`, `not_recovered` and `recovered` never go to `cancelled`.
- `cancelled` is final for the platform. The incident itself stays with people outside the platform (reading of Q4.14).
- Time, budget or invalid-output limits during investigation go `investigating` → `inconclusive` → `handed_over`, so the record always says "Root cause not established". `investigating` → `handed_over` is for a security or data-risk stop only. `diagnosed` → `handed_over`: no allowed fix can be drafted, or Release A with sandbox unavailable. `verifying` → `handed_over`: verification inconclusive (ADR-66).
- **24-hour limit:** every non-terminal, non-waiting state hands over. `open` passes through `investigating` to get there. `recovered` stays `recovered` until the incident lead closes it: the limit stops work, and no work remains.
- **Provider returns after an outage:** no automatic resume. The incident stays `inconclusive` → `handed_over`, and a person may request re-investigation. Q2.19's automatic resume is superseded.
- Q3.15's nine stages describe how a team tracks an incident. They are not workflow states.

- **Rationale:** the answers and tests depend on `testing` and `reconciling`, and the specification's table is older. **Owner:** Platform owner. **Reversal:** Medium before M1, High after. **Revisit when:** a state has no test path.
- **Resolves:** Layer 2 conflict 2, Layer 3 lifecycle row, Layer 8 C1, C2, C3, C8, C10, C12, C19, Layer 7 action lifecycle, Layer 12 workflow states.

### ADR-47: Execution identity

| Concept | Rule |
| --- | --- |
| Action | Immutable. A new patch or target is a new action with a new `action_id` |
| Execution key | Equals the `action_id`, and is passed unchanged to the release process on every attempt |
| Attempt | One `execution_attempts` row per try: same key, increasing `attempt_no` |
| Applied | At most one row with outcome `applied` per key, enforced by a partial unique index |
| Approval | Authorises the action. Two approvers still authorise one key. Attempts record the deciding `approval_id` for audit |
| Retry | Only while the action is still `applying`: same key, next `attempt_no`, after a lookup shows the change wasn't applied. Once reconciliation sets `not_applied`, which is final, any further try is a new action needing a new approval |
| Reconciliation record | An `unknown` attempt stays frozen. Reconciliation adds a new attempt row marked `reconciliation` |
| Unknown outcome | Blocks every further change on that deployment, not just that key, until a person clears it |

`exec:<approval_id>` and `undo:<approval_id>` are retired.

The audit's suggestion of a separate key per attempt is not adopted: a retry must reuse the key, or the release process can't recognise it as a repeat and may apply the change twice.

- **Rationale:** the invariant is "each approved change at most once". The change is the action, and an approval-based key breaks when two people approve one action. **Owner:** Security lead. **Reversal:** High after M3. **Revisit when:** the release process can't dedupe on a caller-supplied key (dependency in System Design section 12).
- **Resolves:** Layer 8 C15, Layer 11 C1 and C16, System Design section 13 glossary.

### ADR-48: Rollback is its own action

ADR-43 stands. `is_undo` is retired.

| Question | Decision |
| --- | --- |
| Identity | New immutable action, kind `rollback`, with `parent_action_id`, its own execution key |
| Approval | A **conditional approval** signed with the fix, in the same decision. For high-impact fixes both approvers sign it |
| Lifetime | Starts on a `failed` verification verdict. `valid_until` = parent's `valid_until` + hold period + 15 minutes |
| Without a conditional approval | A fresh approval is required. Escalation and expiry follow ADR-49 |
| Testing | Not re-tested in the sandbox: it restores the known prior revision. `test_bundle_digest` and `recovery_predicates_digest` copy the parent's values |
| Trigger | The workflow, on the Verifier's verdict. The Verifier writes nothing to production and has no dispatcher path |
| Incident state | Stays `not_recovered` throughout, or `reconciling` if the rollback's outcome is unknown, then `handed_over`. Parent action becomes `undone` |
| Checking the rollback | Verified while the incident stays `not_recovered`, with no `verifying` state. The result is reported to the person taking over and triggers nothing |
| Stored as | A separate conditional approval row on the rollback action |
| Guard | Refused if the deployment is no longer at the parent's produced revision |

- **Rationale:** a rollback is a production change that can fail or collide, so it needs its own audit record. Without a pre-signed approval, a failed fix waits for a person during an outage. **Owner:** Security lead. **Reversal:** Medium. **Revisit when:** pilot data shows conditional approvals going unused.
- **Resolves:** Layer 11 gap in 11.13, C2, C3, C4, Layer 8 C10.

### ADR-49: Approval rules

The audit's "15 vs 60 minutes" is not a conflict. These are two different timers, and they match in code, answers and both specs:

| Timer | Value |
| --- | --- |
| Request escalates | 15 minutes |
| Request expires | 60 minutes |
| Granted approval valid for | 15 minutes |
| Approver's sign-in age | Within 15 minutes, with two-factor |

| Question | Decision |
| --- | --- |
| Who approves | Service owner, on-call engineer or incident lead, holding the approve permission for that service |
| High-impact | Any of: confirmed severity `sev1`; kind `release_rollback`; a service marked `critical` in the catalogue; the same change proposed for more than one deployment (provisional) |
| High-impact needs | Two different people with approve rights. The incident lead may be one, but isn't required to be |
| Severity not yet confirmed | Treated as high-impact |
| Self-approval | Nobody approves their own request, and nobody may be the sole approver of a revert of their own change. The platform can never approve |
| What an approval names | The action digest, which covers the patch fingerprint, target, base revision and predicates. `patch_sha256` stays on the record for reading |
| Rejection | Any valid rejection wins, regardless of order. Requires authentication but not a recent sign-in: a safety stop should be cheap |
| Who may reject | Responder, Approver, Incident lead |
| Second approver's decision | Arrives as its own signal, carrying the decision ID. The workflow waits until `approvers_needed` valid decisions exist |
| Failed approval attempts | No approval row, but a `aisre.approval.refused.v1` timeline event and a security alert. "Nothing recorded" in Technical specification section 6 means no approval decision |
| Revocation | `POST /v1/actions/{id}/revocations`, added to the API list. Valid until the dispatcher records intent |
| Late rejection | A rejection after approval, before intent, moves the action to `withdrawn`. `approved` → `rejected` isn't a transition |
| Clearing an unknown outcome | `POST /v1/actions/{id}/outcome-resolutions`, permission `action:resolve_outcome`, incident lead only |

- **Rationale:** takes the stricter rule wherever sources differ on who may act, and the cheaper rule for stopping. **Owner:** Engineering manager (rules), Security lead (mechanism). **Reversal:** Low. **Revisit when:** approvers are unreachable at night in the pilot.
- **Resolves:** System Design open decision on approval rules, Layer 1 conflicts 2, 3, 5, Layer 5 approver, second approver, self-approval and failed-attempt rows, Layer 8 gap in 8.6, C9, C11, Layer 11 C6, C7, C10, C18, C21, gap in 11.6.

### ADR-50: One permission matrix

| Permission | Viewer | Responder | Approver | Incident lead | Monitoring | Platform admin |
| --- | --- | --- | --- | --- | --- | --- |
| Read incidents, timeline, artifacts | ✓ | ✓ | ✓ | ✓ | — | ✓ |
| Create an incident | — | ✓ | ✓ | ✓ | ✓ | — |
| Add a note | — | ✓ | ✓ | ✓ | — | — |
| Cancel an investigation | — | ✓ | ✓ | ✓ | — | — |
| Reject an action | — | ✓ | ✓ | ✓ | — | — |
| Approve or revoke an action | — | — | ✓ | ✓ | — | — |
| Request re-investigation | — | ✓ | ✓ | ✓ | — | — |
| Confirm severity, close | — | — | — | ✓ | — | — |
| Read the full audit export | — | — | — | ✓ | — | — |
| Clear an unknown outcome | — | — | — | ✓ | — | — |
| Set a manual write fence | — | — | — | ✓ | — | ✓ |
| Lift a fence, run a restore | — | — | — | — | — | ✓ |

- Platform components use service identities outside this table, each with its least privilege (ADR-51).
- Sandbox configuration and signing keys are held by the Security lead role, not Platform admin.
- Setting a fence is a safety stop, so a lead can do it directly. A person with a lead's sign-off lifts it.
- The timeline is readable by anyone with read access. The full audit export, including read logs and failed sign-ins, is restricted. Security staff get it through the Incident lead permission on the audit scope.

- **Owner:** Security lead. **Reversal:** Low.
- **Resolves:** Layer 5 responder and audit-reader rows, Layer 11 C8, C9.

### ADR-51: Process topology

| Topic | Decision |
| --- | --- |
| Processes | One codebase, four processes: API, worker, dispatcher, isolated test environment. Supersedes "one deployable" |
| API to Temporal | Outbox only (ADR-19). Diagrams redrawn |
| Signed approval route | Stored by the API. The workflow sends only the action ID on the dispatcher's task queue. The dispatcher reloads the action and signatures from trusted storage. Diagrams 6.2, 6.3, 6.7 redrawn |
| Dispatcher and database | Its own role: read actions, patches, approvals, verification results, the fence tables, stop requests and deployment blocks; write deployment blocks; write `execution_attempts`, action status for `applying` and its outcomes, and its own timeline events. Listed in the component summary |
| Temporal hosting | Self-hosted single node, in its own database on the same PostgreSQL instance. Temporal keeps running through a loss of the incidents database, but not through a loss of the instance. Closes the System Design open decision |
| Patch Proposer | A skill inside the Diagnosis Engine, running after the review accepts the diagnosis. No new component. The draft-pull-request credential is the worker's only write credential outside the platform |
| Notifier | Component 15, in the worker. Added to the Q6.6 module list |
| Context diagram | Adds the identity and review systems |
| Diagrams | Mermaid text in the artifact, per Q6.34. The screenshots are kept as the team's originals |
| Kubernetes | Not in the first version. Revisit if several environments or organisations are in scope, more processes than simple hosting can manage, more than 20 services, or sustained concurrency above 5 |
| Reference environment | The reference application and platform run on one Linux host under Docker Compose. The test environment runs on a separate host |

- **Owner:** Platform owner. **Reversal:** Medium.
- **Resolves:** Layer 2 gap in 2.1, conflicts 3 and 7, Layer 6 conflicts 1–6, 8, 9, Layer 8 C14, Layer 11 C20.

### ADR-52: The coordinator runs the investigation

The Layer 8 workflow owns durability, and the Layer 9 coordinator owns the investigation's decisions:

1. The workflow's `investigating` phase is a loop: call pure `coordinator.next_step(state, now)`, run the returned step as one activity, feed the result back. Each result is checkpointed by Temporal.
2. A patch candidate is one `draft_patch` activity and its own action (ADR-47), up to 3 per diagnosis (ADR-69). Steps are `COLLECT_BASELINE`, `RETRIEVE_KNOWLEDGE` (added), `HYPOTHESISE`, `COLLECT_TARGETED`, `DIAGNOSE`, `CRITIQUE`, then a stop verdict.
3. A skill failure returns a typed failure to the coordinator, which stops as inconclusive.
4. Crash recovery: Temporal re-runs the activity once on another worker (ADR-27). A completed model result is reused by `input_sha256` (ADR-61), so nothing is re-billed.
5. Model calls happen in hypothesise, diagnose, critique and patch proposal, always through the gateway. Technical specification section 5 is amended.
6. `ModelDiagnosisOutput` is replaced by `DiagnosisDraft`. Field names follow `agent_contracts.py`, and ADR-64 aligns storage.
7. Limits read from one configuration object. Tokens and spend are added to the termination policy.
8. Invalid output: 3 gateway attempts per step, then the step fails. A critic failing twice ends the investigation as inconclusive, so a critic can't have more than 6 failed attempts; with revisions, the worst case is 12 critique attempts in all.
9. Revision checks run before pending evidence requests, so no wasted third round. A targeted round adding nothing stops the loop whether or not a critic has run.

- **Owner:** SRE lead. **Reversal:** Medium.
- **Resolves:** Layer 9 C1, C2, C3, C7, C8, C11, the limits rows, Layer 8 C4.

### ADR-53: Evidence strength and confidence

Strength attaches to the **observation**, not the evidence kind. Code assigns a `strength_tier` when it computes facts. Q3.34's order is authoritative:

| Tier | Observation | Class |
| --- | --- | --- |
| 1 | Reproduction: fails with the change, clears without it, in the disposable environment | Mechanism |
| 2 | Version split: only instances on the new version fail | Mechanism |
| 3 | Config match: the changed value is read on the failing path | Mechanism |
| 4 | Code match: traces point at the changed code | Supporting |
| 5 | New error signature | Supporting |
| 6 | Scale match with the rollout | Supporting |
| 7 | Rollback clears it | Supporting |
| 8 | Timing alone | Weak |

**Confidence (provisional threshold):**

| Level | Requires | Result |
| --- | --- | --- |
| High | Tier 1–3, no unresolved contradicting evidence | Established |
| Medium | Tier 4, or two independent tier 5–7 observations, no unresolved contradiction | Established |
| Low | Anything else | Inconclusive, no patch |

Final confidence is the lower of the scored level and the critic's `confidence_cap`. The threshold is "medium". SRE lead recalibrates on the frozen suite before Release A. `validation.STRENGTH` is replaced. `context.EVIDENCE_PRIORITY` stays: it orders context for reading, a different job, and is renamed so the two aren't confused.

- **Owner:** SRE lead. **Reversal:** Low. **Revisit when:** false-established exceeds 2% on the suite.
- **Resolves:** Layer 3 strength, priority, System Design list and decisive-evidence rows, Layer 4 gap in 4.5 (FR-16), Layer 9 gap in 9.7, C6, C13, C15.

### ADR-54: Required evidence

For change-caused failures, the required sources are: the alert, the change history, and at least one of logs or the error tracker.

- A missing source never stops collection: record a gap, lower confidence, continue (Q3.36, System Design section 7).
- A diagnosis can't be established while a required source is missing, so it ends as inconclusive. This reconciles Q3.37.
- A sign of a security or data risk still stops at once.
- Metrics are expected. Traces, dependency health and past incidents are optional. Absence of any of these lowers confidence but doesn't block.

"Enough evidence" means every required source collected, plus the Medium bar in ADR-53. Other categories are recognised and handed over, as they're out of scope.

- **Owner:** SRE lead. **Reversal:** Low.
- **Resolves:** Layer 3 gap in 3.8, unavailable-source and per-category rows.

### ADR-55: Requirement corrections

| Item | Decision |
| --- | --- |
| NFR-08 | Retired, superseded by NFR-30 (p95 ≤ 300 ms, p99 ≤ 1 s). ID kept, never reused |
| NFR-22 | ≤ 1 hour to read-only investigation mode; ≤ 4 hours to full operation including writes |
| Must, Should | Must: its milestone can't pass without it. Should: expected at that milestone; can slip only by a recorded decision |
| Concurrency rationale | 5 is the limit. It is at least double the assumed peak of 2, not exactly double |
| NFR-14 at 100% daily spend | New investigations are evidence-only. Running ones finish their current step, then stop and hand over |
| Provisional values | Everything in a provisional table stays provisional, whether or not its row is labelled |
| New rows | Injection (0 unsafe actions, M1) and allowed list (patches only from it, M2) get register rows FR-39 and FR-40 |

- **Owner:** Platform owner. **Reversal:** Low.
- **Resolves:** Layer 4 conflicts 1–4, 7, 9, gap in 4.3.

### ADR-56: Spend and capacity values

These are **placeholders so that limits can be built and tested**, chosen to keep the code's existing per-incident cap. The Engineering manager confirms the money values before the pilot.

| Value | Provisional | Basis |
| --- | --- | --- |
| Currency | USD | Assumption |
| Per-incident model cap | $2.00 | Existing `max_cost_micros = 2_000_000`, now sourced here. At list prices the token caps bind first, so this is a backstop |
| Monthly model spend ceiling | $500 | 250 incidents at the cap. Infrastructure cost is separate and stays with the actual budget |
| Daily limit | $16.67 | Monthly ÷ 30 (System Design section 9) |
| Q2.12 spend threshold (GPUs, self-hosting, Tinker) | Model spend above 80% of ceiling for 3 straight months | New |
| Second provider trigger | Provider-caused hand-overs above 5% of incidents in 90 days, or 2 outages over 60 minutes in a quarter | New |
| Disposable copies at once | 3, queued beyond that | New |
| Sandbox host rate | `sandbox_micros_per_s = 50` kept | Existing code value |
| Evaluation spend | $300 a month, separate from the incident ceiling; a nightly run stops at $10 | New. Full development-split runs are on demand |
| Time saved, hourly cost | Measured in the pilot | Can't be set by decision |

- **Owner:** Engineering manager. **Reversal:** Low. **Revisit when:** finance confirms a budget, or after the pilot.
- **Resolves:** Layer 2 gaps in 2.4 and 2.5, Layer 4 NFR-14 gap, Layer 10 gap in 10.5, C2.

### ADR-57: Evidence storage and retention

| Data | Kept | Owner |
| --- | --- | --- |
| Evidence snapshots, all collected | 30 days | SRE lead |
| Evidence cited by a diagnosis | 1 year | SRE lead |
| Redacted model input and output | 1 year | SRE lead |
| Timeline and audit | 1 year | Security lead |
| Patches, test logs | 1 year | Security lead |
| Traces | 14 days | Platform owner |
| Platform logs | 30 days | Platform owner |
| Metrics | 13 months | Platform owner |
| Backups and write-ahead logs | 35 days | Platform owner |
| Drill and evaluation results | 2 years | SRE lead |

All are provisional. Q10.64's six-month replay is honoured by the 1-year model data, which is safe because it's redacted. Bulky items go to the object store from the start. Q2.4's "only the evidence it used" now means what is kept long-term.

- **Owner:** Security lead. **Reversal:** Low. **Revisit when:** the provider terms or a legal review require shorter periods.
- **Resolves:** Layer 2 conflicts 5, 8, Layer 10 C10, Layer 12 gaps in 12.7.

### ADR-58: Intake edge cases

| Case | Decision |
| --- | --- |
| Rejected alert | Stored raw in a dead-letter record with the reason, and the incident lead on call is notified. It gets no incident. "No alert is ever dropped" now holds |
| Unknown service, authorised monitoring caller | 422, dead-lettered |
| Unknown or forbidden service, person | The same 404 for both, so existence isn't revealed. Details go to the dead-letter record |
| Missing fingerprint | Falls back to service plus alert name (Q4.2), so the stored column is never null |
| Out-of-scope failure type | Recorded, recognised and handed over, never fixed. Evaluation categories test this |

- **Owner:** Platform owner. **Reversal:** Low.
- **Resolves:** Layer 4 conflict 5, Layer 5 unknown-service row, Layer 7 fingerprint row, Layer 12 failure types.

### ADR-59: Model output safety

| Case | Decision |
| --- | --- |
| Secret in model output | The output fails validation at once and isn't retried, since a retry would resend the context that produced it. If stored for audit, the secret is masked first. The leak procedure applies |
| Commands in model output | Rejected. Only fixed harness steps run in the sandbox. The model proposes no commands |
| Output derived from flagged evidence | Added to the validation table: a claim citing only flagged evidence is unsupported, and flagged items are shown to the approver |
| Links | Never fetched |

- **Owner:** Security lead. **Reversal:** Low.
- **Resolves:** Layer 5 secret, command and flagged-output rows.

### ADR-60: Sandbox network

No network. An approved package mirror requires a new ADR, a filtering proxy and Security lead sign-off. None is planned. Patches are limited to configuration and reverts, so nothing new needs installing.

- **Owner:** Security lead. **Reversal:** Low.
- **Resolves:** Layer 2 conflict 4, Layer 5 sandbox row, Layer 6 conflict 7.

### ADR-61: Model Gateway behaviour

| Case | Decision |
| --- | --- |
| Unsettled reservation | Sent with unknown usage: charged at the reserved maximum. Never sent: released on expiry. Q10.36 wins, and the schema keeps `expires_at` for the second case |
| Per-call input | 16,000 tokens, enforced in the gateway. Over-limit requests are refused before reservation |
| Over-limit context | Deterministic trimming by priority. No model-written summaries (Q10.60). If required items don't fit, narrow the scope. "Trim or summarise" reads as trim |
| Truncation | One retry with double the output limit, up to 16,000 |
| Context exceeded | Reassemble smaller input. Never raise the output limit |
| Timeout record | Reported as estimated, at the charged amount |
| Broken stream | Usage unknown, charged in full, reconciled later |
| Circuit breaker | In the gateway: opens at 50% failures of at least 10 calls in 60 s, for 30 s |
| Result reuse | Same `input_sha256` within an incident reuses the recorded result |
| Tool proposal with a schema | Delivered to the gate, not treated as invalid output |
| Token estimator | One shared function |

- **Owner:** SRE lead. **Reversal:** Low.
- **Resolves:** Layer 10 C1–C8, C15, C16, System Design section 9 token row.

### ADR-62: Retrieval storage

Documents, chunks and immutable index revisions live in PostgreSQL (tables in `schema.sql`). BM25 ranking runs in the worker per Q10.50. ADR-09 is amended: PostgreSQL stores the corpus, and the worker ranks it. No extra system is added. The revision ID hashes document versions, permitted groups and the full build timestamp, so identical IDs mean identical results.

- **Owner:** SRE lead. **Reversal:** Low. **Revisit when:** ADR-37 triggers, or corpus size makes in-worker ranking slow.
- **Resolves:** Layer 10 gap in 10.9, C9, C14.

### ADR-63: Prompt release

Prompts live at `prompts/<skill>/v<N>.md`, immutable once merged. A new version needs a pull request approved by the SRE lead and a passing evaluation run on the development split to merge. Activating it in production also needs the ADR-65 gates on the held-out split. Activation is a recorded configuration change. Rollback activates the previous version. Old versions stay readable for replay. Each attempt records the version and the text fingerprint.

- **Owner:** SRE lead. **Reversal:** Low.
- **Resolves:** Layer 10 gap in 10.12, C12.

### ADR-64: Schema and contract alignment

`db/schema.sql` (proposed) is the canonical schema, tested on a real PostgreSQL instance. Alembic migrations take over from it at M0 (ADR-67). The copy in `layers/layer-07-data-apis-events/files/` is the filed version.

| Topic | Decision |
| --- | --- |
| Enums | Match ADR-46 exactly. Add action kind `rollback`, decision `revoked` |
| Test outcomes | The six from Layer 11: `passed`, `failed`, `not_reproduced`, `inconclusive`, `crashed`, `timeout` |
| Verification results | `verified`, `failed`, `inconclusive` (Layer 11 names). Contracts follow |
| `org_id` | On top-level records only (Q7.2), reached by join elsewhere. Technical specification section 4 is amended |
| Keys | UUIDv7 on entity tables; `outbox`, `idempotency_keys` and join tables keep their natural keys |
| Fingerprint name | `patch_sha256` everywhere, including `patches` |
| Action digest | New `action_digest` column on actions, approval decisions and attempts |
| Hold period | `hold_seconds` column on `actions` |
| CloudEvents | Envelope names stay lower-case on the wire. Storage uses snake_case. `org_id`, `traceparent` and `dataschema` get columns or a derivation |
| Version on writes | `If-Match` on every change to a versioned record, including rejections. `expected_action_version` is removed. Missing is 428, stale is 412. Cancellation and notes are exempt: stopping or commenting shouldn't lose a race |
| Evidence | Adds `tool`, `source_as_of`, `gap_reason`, `strength_tier` |
| Diagnosis | Hypotheses and diagnoses gain `category`, claims, `ruled_out`. Contract gains `reviewed`, `review_notes`, `produced_by` |
| `model_attempts` | Adds checkpoint, adapter version, output schema fingerprint, provider request ID, stop reason, cache tokens, usage reported or estimated, prompt fingerprint. Task name `critique` |
| Undo | `is_undo` is removed (ADR-48) |
| Catalogue | `services` gains `critical` and `hold_seconds` (ADR-49, ADR-66) |
| Table count | 37 tables, not 22. Beyond the first 30: `investigation_queue`, `fence_state`, `fence_reasons`, `reservation_corrections`, `tool_calls`, `stop_requests`, `deployment_blocks`. Technical specification section 4 is amended |
| Digests | `action_digest` and `test_bundle_digest` are write-once, set at `testing` → `awaiting_approval`, because the test bundle doesn't exist when the action is created. A rollback is the exception: it copies its parent's digests at creation |
| Intake | `dead_letters` table (ADR-58) |
| Retrieval | `documents`, `chunks`, `index_revisions` (ADR-62) |

The images in 7.1 stay as the team's originals. `schema.mmd` and `schema-tables.sql` stay as the team's tabs, superseded by `schema.sql`.

- **Owner:** Platform owner. **Reversal:** Medium before M1.
- **Resolves:** Layer 7 gap in 7.2 and every conflict row, Layer 8 C2, Layer 9 C4, Layer 10 C11, Layer 11 C11.

### ADR-65: Evaluation gates

A candidate is promoted only if all of these hold on the held-out split:

| Gate | Value | Source |
| --- | --- | --- |
| Leading cause, change-caused cases only | ≥ 80%, and no more than 2 points below baseline | Spec section 11, plus the existing margin |
| Honesty, withheld evidence | 100% | Spec section 11 |
| Conflicting evidence | 100% | Spec section 11 |
| Injection followed | 0 | Spec section 11, with Q12.60's wider counting |
| Unauthorised attempts | 0 | Existing |
| Security suite | All cases due by that milestone pass; all 40 by M4 | Spec section 11 |
| False-established rate | ≤ 2% | Existing code value |
| Under-escalation | < 2% | Q12.59 |
| Citations | 100% exist; ≥ 99% support their claim | Invariant 4, plus the existing code value |
| Cost per incident | ≤ 1.2× baseline | Existing code value |
| First summary | p95 ≤ 300 s | Spec, measured to first summary |
| Investigation | ≤ 900 s, every case | Spec, measured to diagnosis |
| Unsafe proposals | 0 | Q12.60 |
| Verified repair | Not below baseline | Q12.83 |
| Case counts | 400 total, ≥ 280 held-out, ≥ 20 per category across 12 categories | Q12.54, raised from Q12.52's 300/200 because 12 categories need 240 at least |

Nightly accuracy runs use the development split. Held-out runs are for promotion only. Canary rollback: test paired by service and category at α = 0.05; cost above 1.2× or first-summary p95 above 300 s over 24 hours is a breach.

Two accuracy measures are kept, and they answer different questions: synthetic suite accuracy gates each release; confirmation by incident review is the pilot business measure (Q1.18).

- **Owner:** SRE lead. **Reversal:** Low.
- **Resolves:** Layer 1 conflicts 9, 10, Layer 4 conflict 6, Layer 12 section 11 table, gap in 12.11.

### ADR-66: Recovery verification

| Topic | Decision |
| --- | --- |
| Baseline | Median of the symptom metric over the 60 minutes before incident start, excluding the 5 minutes before start. Recorded with the action, before execution |
| Hold period | 10 minutes by default, longer per service in the catalogue. Closes the System Design open decision |
| Recovered | Every required predicate held for the full window with enough data (60 s interval, at least 50 requests) |
| Inconclusive | Too little data: hand over, no rollback (Q11.88). Workflow code is corrected. A person decides |
| Failed | Rollback path (ADR-48) |
| Accepting recovery | Incident lead, in all cases |

- **Owner:** SRE lead. **Reversal:** Low.
- **Resolves:** Layer 11 gap in 11.12, C5, Layer 8 C17, Layer 1 conflict 5.

### ADR-67: Code lives in the repo; docs are the artifact filing

The code is generated into a real package at the repo root. `docs/` is not moved or changed by this: each layer's `files/` folder stays as that artifact's filed copy.

| Path | Holds |
| --- | --- |
| `src/aisre/contracts.py` | Shared contracts, imported by every other module (Layer 7) |
| `src/aisre/workflow/` | `workflow.py`, `activities.py`, `states.py` (Layer 8) |
| `src/aisre/agent/` | `coordinator.py`, `agent_contracts.py`, `tool_registry.py`, `validation.py` (Layer 9) |
| `src/aisre/gateway/` | `gateway_contracts.py`, `adapters.py`, `gateway.py` (Layer 10) |
| `src/aisre/retrieval/` | `retrieval.py`, `context.py` (Layer 10) |
| `src/aisre/remediation/` | `patch.py`, `sandbox.py`, `action.py`, `dispatcher.py` (Layer 11) |
| `src/aisre/verification/` | `verification.py`, apart from the dispatcher's code (Layer 11) |
| `src/aisre/reliability/` | `telemetry.py`, `fence.py` (Layer 12) |
| `src/aisre/evaluation/` | `eval_harness.py` (Layer 12) |
| `src/aisre/api/`, `record/`, `config.py`, `workflow/relay.py`, `workflow/worker.py` | Planned, added at M0 (provisional): API, Incident Record, settings, outbox relay, worker entry point |
| `prompts/` | Planned, added at M1 (ADR-63) |
| `db/` | `schema.sql`, `schema-tests.sql`; `db/migrations/` added at M0 |
| `ops/` | `alerts.yml` |
| `tests/` | `tests/unit/`, `tests/security/`, `tests/fixtures/reference-incident/` |

From this point `src/` is what changes. The `files/` copies stay as filed, so a change in `src/` doesn't update them. Refile a copy only when its artifact is re-issued.

- **Rationale:** one import path and one test command, while `docs/` stays a stable record. **Owner:** Platform owner. **Reversal:** Low. **Revisit when:** the filed copies cause confusion, in which case replace them with links to a tagged release.

### ADR-68: Milestones M0 to M5

The worksheet requires M0–M5. M1–M4 keep the meaning they have in Artifacts 1–12, so no requirement, test or criterion needs renumbering. M0 and M5 are added around them.

| Milestone | Delivers | Ends with |
| --- | --- | --- |
| M0 Foundation | Repo, toolchain, CI, migrations, a thin intake path, deterministic workflow, no AI. Full intake (FR-01) stays at M1 | A scripted incident runs through the thin intake and the workflow with fake results |
| M1 Investigate | Evidence, gateway, coordinator, diagnosis, review, retrieval, evaluation seed | The reference incident gets a cited diagnosis; the honesty run says "Root cause not established" |
| M2 Patch and test | Patch drafting, isolated sandbox | No test without reproduction; end of Release A's build |
| M3 Approve, apply, verify | Approval Service, dispatcher, Verifier, rollback, on synthetic targets | A failed fix is rolled back on a synthetic target |
| M4 Harden | Crash recovery, outages, backups, telemetry, burst handling, all 40 security tests, frozen evaluation suite | Restore drill passes, the suite is frozen and its gates pass; Release A goes live as the advisory pilot |
| M5 Pilot and decide | Advisory pilot, measured baselines, pilot accuracy, approval review | A recorded decision on Release B, and the final report |

- **Rationale:** renumbering the requirements and 40 security tests has real cost and no benefit. **Owner:** Engineering manager. **Reversal:** Low. **Revisit when:** a milestone misses its gate.
- **Resolves:** the M1–M4 vs M0–M5 difference between Layers 1–12 and the worksheet.

### ADR-69: Decisions from the Layer 1–13 audit (2026-11-17)

| Topic | Decision |
| --- | --- |
| Patch candidates | One candidate is one action. Up to 3 actions per diagnosis, drafted one at a time: the next is drafted only when the previous one's test fails. Corrects ADR-52's "per action" |
| Incident version | People can change severity while the workflow runs, so the workflow never trusts its copy of the incident `version`: see "Stale version" below |
| Stop request | Cancelling during `awaiting_approval` or before the dispatcher records intent is a stop, not a revocation. Anyone with the cancel permission can raise it, and the dispatcher refuses an action whose incident has a stop. A revocation still withdraws only the caller's own approval |
| Cancel and rollback | A cancel never skips the rollback of a failed fix. Once a change is applied, the verify and rollback path always completes; the cancel takes effect after it |
| Final confidence | `diagnoses` stores `final_confidence` and `final_outcome`, written once at review. APIs show these, so a diagnosis capped to low reads as inconclusive |
| Unknown-outcome block | Held per deployment in its own table, not derived from attempt rows. A late apply after `not_applied` puts the block back, and only an outcome resolution by the incident lead clears it |
| Tool call log | `tool_calls` table: per-incident budgets, repeat detection and stored results for repeats |
| Re-investigation queue | A queue row carries its run ID. Reconciliation removes a row only when its run has ended |
| Dispatcher and fence | The dispatcher role can read `fence_state` and `fence_reasons` |
| Model per task | `diagnose` and `critique` use the `default` alias (Opus 5.5); `hypothesise` and `draft_patch` use `standard` (Sonnet 5.5). Set in code until `prompts/` exists (provisional) |
| Per-service hold | An action's `hold_seconds` can't be below its service's |
| Deadlines | Q13.51's deadlines stand over Artifact 2.7's: release process before M3, rotas before the pilot, so by the end of M4, provider terms before the first real incident data is sent, named people before M0 |
| Test host owner | Security lead owns it; Platform owner provisions it |
| Fence and restore | Operated through the CLI by a Platform admin at first. No HTTP endpoints in the first version; a lead sets a manual fence through the CLI too |
| Patch mismatch | When the dispatcher finds a mismatch, it returns a void-required result. The workflow closes the request as voided and writes `aisre.approval.voided.v1`, because the dispatcher's role can't write that event |
| Injection flagging | A deterministic flagger marks instruction-like text when evidence is normalised (provisional patterns). Flagged items are shown to the approver |
| Void record | A request already closed as `decided` keeps that close reason, since it is write-once. The void is recorded by the `aisre.approval.voided.v1` event |
| What counts as a stop | Only a person's cancel is a stop. A Temporal-level cancel before intent isn't. A stop applies to actions created before it, so a later re-investigation isn't refused by an old stop |
| Stale version | The workflow sends the last version it saw, and on a stale-version refusal reloads and retries, at most 5 times, then hands over. If the hand-over write is also refused as stale, the run fails, because nothing more can be recorded |
| Late apply resolved as applied | The action stays `not_applied`, which is final. The deployment block row and the outcome resolution are the record that the change landed, and the timeline links them |
| Release A closure | "Never close without independent verification" applies where the platform applied the change. In Release A a person applied it, and the incident lead's acceptance closes it |

- **Owner:** Platform owner; Security lead for the stop, block and fence rows. **Reversal:** Low, except the block table (Medium). **Revisit when:** team review.
- **Resolves:** the code and design defects found in the 2026-11-17 audit, and the Layer 13 conflicts.

### ADR-70: A read-only demo viewer, outside the product (2026-11-18)

Artifacts 1, 2, 5, 6 and 13 keep a product dashboard out of the first version: the API and CLI are the interface until they are shown to be insufficient (Artifact 6). Showing the investigation to people who will not run a CLI is a different need, so a demo viewer is added as a showcase tool, not as that dashboard.

| Topic | Decision |
| --- | --- |
| What it is | A static page in `demo/` with no build step, served by its own Compose service, that starts a synthetic incident and follows its timeline, evidence, hypotheses and diagnosis live |
| What it may do | Read through `/v1`, and create an incident with `POST /v1/incidents`. Nothing else: its proxy refuses every other write, so it can never approve, cancel, close or execute |
| Sign-in | The same bearer tokens and ADR-50 checks as any caller (Artifact 5: any later interface follows the same rules). It gets tokens from the stack's fake token issuer, so it works only against a local or test stack, never production |
| Untrusted text | Evidence and model output are rendered as text, never as HTML, under a strict Content-Security-Policy |
| Not in scope | It is not the product dashboard, gets no product requirements, and is not deployed. A real dashboard still needs Artifact 6's measured problem |

- **Owner:** Platform owner. **Reversal:** Low: delete `demo/` and its Compose service. **Revisit when:** someone asks for it outside a demo, which is Artifact 6's trigger for a real dashboard.
- **Resolves:** the request for a visual showcase of the investigation without breaking the first version's scope.

### ADR-71: A development sandbox runtime, labelled, never isolation (2026-11-18, Proposed)

M2 is built on a macOS host with Docker Desktop, which has `runc` and no gVisor. The test host with `runsc` (T2.1) is not available, so the flow is built and proven here and the isolation is proven later, on that host.

| Topic | Decision |
| --- | --- |
| Runtime | The launcher takes it from settings (`AISRE_SANDBOX_RUNTIME`). Production requires `runsc`. `runc` is allowed only when `AISRE_ENV` is `development` or `test`, and is refused otherwise, like scripted skills and the fake evidence adapter: by `Settings` and again by the launcher's argument check |
| Same flags | `runc` runs with every other flag unchanged: no network, read-only root, user 65534, all capabilities dropped, `no-new-privileges`, cgroup CPU, memory and PID limits, bounded tmpfs, a size-limited workspace volume, the wall clock |
| Label | Every result says what it ran under: `isolation` is `gvisor` or `development-runc` in the stored test run and in `aisre.action.tested.v1`. A `development-runc` result proves the flow, never isolation |
| Checkout | The Test Runner runs `git archive` of the exact `base_revision` commit from its own read-only mirror of the repository (`AISRE_SANDBOX_REPOS`), and pipes it on the container's standard input, as a tar with the patch's canonical bytes and the run description. The harness unpacks it into the run's tmpfs workspace volume. No host path, repository, `.git` directory or credential is ever mounted |
| Image | Built from `ops/sandbox/`, on a base pinned by digest, the harness inside it, running as user 65534 and needing no network. It is named by digest (`AISRE_SANDBOX_IMAGE`): a registry digest on the test host, the content-addressed image ID locally |
| Protected paths | The launcher refuses a patch touching a protected path (`patch.PROTECTED`: tests, test configuration, CI, policies, the harness and sandbox, deployment files) before any volume or container exists, and the harness refuses it again. One rule for the draft and the launcher, `patch.check_paths`; its one exception, an exact restoration under `deploy/`, is ADR-72's |
| Docker access | The Docker socket is root-equivalent on its host. Only the Test Runner holds it: a separate process, `python -m aisre.remediation.test_runner`, on its own task queue, `aisre-test-runner`, which serves `run_sandbox_test` and the reaper and nothing else. Never the API, and never the worker that runs model-facing code. Locally it runs on the host; in production on the test host (ADR-51). It also holds `aisre_app` and an object-store read until a Test Runner role exists (M4) |
| Three at once | Across Test Runner processes: three PostgreSQL session advisory locks, so a crashed process frees its slot. A queued run's wait and its run share one budget inside the activity's 900 s, leaving a margin for the stop and cleanup |
| Reaper | A timer workflow every 5 minutes on the Test Runner's queue, as for the budget jobs (the time-skipping test server has no Schedule API). It removes anything labelled `aisre.sandbox=1` older than its wall clock plus a margin, and sets `aisre_sandboxes_leftover` |
| Security gate | M2's security gate (ST-16, ST-25 to ST-28) stays NOT MET until the same tests pass under `runsc` on the gVisor host. `system` tests that need gVisor stay `system` and skip here with the reason |

- **Owner:** Security lead. **Reversal:** Low: remove `runc` from the allowed runtimes. **Revisit when:** the gVisor test host exists (T2.1), at which point local runs may keep `runc` but no M2 evidence may rely on it.
- **Resolves:** building and demonstrating M2's patch-and-test flow without the gVisor host, without claiming isolation it does not have.

### ADR-72: Patch and test decisions for M2 (2026-11-18, Proposed)

T2.8, T2.10 and T2.12 decisions taken while building M2's patch-and-test flow on the development host (ADR-71).

| Topic | Decision |
| --- | --- |
| One protected-path rule | `patch.check_paths` is the only rule, called by `check_boundaries` when a patch is drafted and by the Test Runner's launcher (`sandbox.refuse_protected`) before anything starts; the harness gets the same `PROTECTED` list. Pass 1's separate launcher list (`LAUNCH_PROTECTED`, without `deploy/**`) is gone |
| Deployment files | `deploy/**` stays protected, with one exception (`RESTORABLE`): a `config_revert` that names the commit it reverts (`reverted_commit`, now allowed on a config revert) may put a file that commit changed back exactly as it was before the commit, and only if nothing changed the file since. In content-hash terms: the file is in the commit, the patch's `old_sha256` is the file at the commit, and its `new_sha256` is the file at the commit's parent. Anything else under `deploy/` is refused: another key, another value, a file the commit didn't change, a file edited after it, any other kind, a commit whose changes can't be read. A path that also matches another protected pattern (`deploy/secrets.env`, `deploy/Dockerfile`) is never restorable. This is ADR-48's "a commit revert touches only what the commit changed", at file level, so a revert can't carry a second edit |
| Where the facts come from | What the commit changed (each path, its hash before and after: `RevertFacts`) is read by code from the repository, never from the patch or the model: at draft time through the review system's repository API, at launch from the Test Runner's own mirror (`sandbox.revert_facts`). The harness is told which protected files are restorations and with which hash, and refuses any other |
| Edit policy | `edit-policy@2`: `config_revert` may also edit `deploy/*.env`, which the rule above limits to restorations |
| Review system | Draft pull requests go through a review system client (`remediation/review.py`), configured by `AISRE_REVIEW_ADAPTER`, `_URL` and `_TOKEN`; locally Compose's fake (`ops/fakes/review.py`), which `Settings` refuses in production like the other fakes. The worker holds only the draft-pull-request credential (Artifact 11.7): the client has no merge, push or commit call, and the fake refuses a merge and any pull request that isn't a draft. The fake also refuses a patch that doesn't apply at its base revision, and marks one touching `deploy/`, tests or CI as needing the security lead's review (11.1's third barrier) |
| Base revision and repository | The repository is the incident's service's own, named after it (FR-48). The base revision is the head of its default branch in the review system, the revision the release process deploys; the Patch Proposer reads the recent changes and the files' content there, through the same client. A candidate reverts one of those recent changes: the model names the revision before it (`target_ref`), code finds the commit and its facts |
| When the pull request opens | Only after the fix's failure reproduced and its test passed (`open_draft_pull_request` refuses anything else, `NotTested`), with the patch, the hypothesis, evidence, expected effect, risk, undo plan and the test result with its isolation label (FR-19, ADR-71). It is recorded on the patch once (`patches.review_ref`, write-once from migration 0008: the one column of `patches` that changes, from null, because the pull request comes after the row) and as `aisre.action.review_opened.v1`. The platform never merges it (ADR-44). The action then ends `withdrawn` and the incident `handed_over` with the recommendation |
| What a drafted action holds | `expected_after_revision` is `<base>+patch:<digest prefix>`, the base with this patch applied: the pull request's head doesn't exist until the test passes, and in Release A nothing is deployed. The reviewable patch (summary, each hunk's rationale and evidence) is stored beside the canonical bytes as a second `patch_diff` artifact (`.review.json`), since the canonical form leaves them out of the digest. Two events were added to Artifact 7.5 for the timeline: `aisre.action.test_started.v1` (written by the Test Runner, with the isolation label) and `aisre.action.review_opened.v1` |
| The `not_reproduced` case | The reference repository has a second branch, `tolerant`, whose code parses `8.25` as a decimal and carries the same configuration change. With the fake review system's default branch set to it (`FAKE_REVIEW_DEFAULT_BRANCH=tolerant`), the same incident drafts the same revert against a base that doesn't fail, so the failure doesn't reproduce, the patch is never tested, and the incident is handed over saying so. It is a constructed case, labelled as one: it shows the gate, not a real incident. The fake provider answers it with its own script (`reference-draft-patch-tolerant.json`), the same revert with that branch's revision |
| Provisional output patterns: hosts with paths | Decided against the development split (`tests/fixtures/eval/dev-seed-1`: 2,076 strings, no link, command, secret or IPv4-shaped string under either rule) and the reference repository, whose `deploy/production.env` holds `http://payments.internal` and `http://inventory.internal`, so the old rule stopped the reference fix at the gateway. File content a patch proposes (`PatchFileEdit.new_content`) is scanned for secrets in the gateway; once code has diffed it, the lines it adds are scanned for links and commands too (`validation.check_added_lines`), unless the file is an exact restoration verified by `check_paths`. Model prose keeps the full check: `registry.internal/app` in a summary or rationale is still a link. Controls: `tests/security/test_provisional_patterns.py` |
| Provisional output patterns: four-part versions | The error tracker's `release` parameter takes a version (`10.2.1.3`, `v2.14.1`, `2.14.1-rc.1`): the numeric core is exempt from the IPv4 rule, and whatever follows it is checked as before, so a port, path, scheme, encoded slash or host name is still refused (ST-30). Controls in the same file |

- **Owner:** Security lead (protected paths, patterns), SRE lead (Patch Proposer, review system). **Reversal:** Low: drop `deploy/*.env` from the policy and `RESTORABLE`; the review system is one adapter behind settings. **Revisit when:** the frozen suite exists, for the patterns (ADR-65); a second repository layout, for `RESTORABLE`; the real review system is chosen, for its adapter and its branch protection.
- **Resolves:** pass 1's two protected lists (ADR-71), and the risk "Provisional patterns can reject real text" (Artifact 13.11, `docs/code-changes-required.md`).

### ADR-73: Stand-ins for M3's approval and write path (2026-11-19, Proposed)

M3 needs an identity system, a secrets manager and a confirmed release process (Artifact 13.4, T3.1, T3.3). None is chosen. M3 is built and shown against labelled stand-ins on the development host, and nothing here is a production choice.

| Topic | Decision |
| --- | --- |
| Identity groups | The fake token issuer (`ops/fakes/token_issuer.py`) stands in for the identity system. It holds personas (`ops/fakes/personas.json`), each a subject and its groups, and mints a persona's token with those groups; it also answers a directory lookup, a subject's current groups, which the dispatcher reads when it re-checks approval rights at execution (Q11.59). Groups name a role per service (`aisre:approver:orders-api`, ADR-49, ADR-50). Approve, reject, revoke and resolve rights come only from groups, through `action.MATRIX`; no persona name appears in `src/`. Refused when `AISRE_ENV` is production |
| Signing key | The Approval Service signs each decision with an Ed25519 development key read from settings (`AISRE_APPROVAL_SIGNING_KEY`, a fake value in `.env.example` only); the dispatcher holds only its public key (`AISRE_APPROVAL_VERIFY_KEY`, ADR-17). Both are refused unless `AISRE_ENV` is development or test, like scripted skills, the fake evidence adapter and `runc`. The key ID is `development-` and a fingerprint of the public key, and every decision row records it (`signing_key_id`), so a development-signed approval can never be read as a real one. Without a key, the approval routes answer 503 and the dispatcher does not start |
| Release process | A fake release process, `ops/fakes/release.py`, its own Compose service like fake-review, stands in for the real one (T3.1 stays unconfirmed). For the synthetic `orders-api` in `production` (deployment `orders-api-production`) it holds the deployed revision and configuration, starting at the reference repository's `main`, and applies, undoes and looks up a change by its key, the action ID (ADR-47). An apply carries the patch's canonical bytes, which it checks against `patch_sha256` and applies hunk by hunk; an undo restores an earlier revision it holds. Writes need its token, which only the dispatcher process holds; reading the deployed state needs none |
| Monitoring follows the deployment | The fake monitoring source serves live, per-minute metrics for a deployment, computed from the fake release process's history: a minute whose configuration the reference application can't parse (`ORDER_TAX_RATE_BPS` not an integer) has the incident's error rate, any other minute the baseline. Applying the config revert brings the error rate down; a bad change brings it up. The Verifier (T3.8) reads that, never the release process's word |
| Production write | No credential for it exists (ADR-44). The only release adapter is `fake_release`, which `Settings` refuses in production; with no adapter the dispatcher process refuses to start, so nothing can write anywhere but the fake. Tests prove both |
| When the workflow asks for approval | A tested fix asks for approval instead of handing over only when the worker runs in development or test with `AISRE_RELEASE_ADAPTER=fake_release`. Otherwise M2's Release A path is unchanged: the draft pull request and `handed_over` (ADR-44) |
| Dispatcher process | `python -m aisre.remediation.dispatcher_process`, its own Compose service on the `aisre-dispatcher` queue, as `aisre_dispatcher` (ADR-51). It holds the release token and the verify key, reads patch bytes from the object store, and never the Docker socket; the worker and the API never hold the release token. Migration 0009 lets its role read what the digest and the high-impact rule need (service and environment names, criticality, confirmed severity) and a patch artifact's storage key |
| Outcome resolution | `POST /v1/actions/{id}/outcome-resolutions` checks the Incident lead, sign-in age, two-factor and `If-Match` in the API, then starts a one-activity workflow through the outbox that the dispatcher runs: only its role writes attempts and blocks (ADR-51). It answers 202, not Artifact 7.4's 200 |
| Change author (pass 2) | ADR-49's "author of the reverted change" comes from the record: the Patch Proposer stores the reverted commit's author, as the review system reports it, in `actions.change_author` (migration 0010). Until an identity system is chosen the repository's author name is taken as the identity subject (`user:<name>`), as the fake issuer's personas are |
| Verifier and post-change watch (pass 2) | The Recovery Verifier reads the fake monitoring source's live metrics (`AISRE_VERIFIER_MONITORING_URL`, refused in production) and judges the criteria recorded on the action, one row a minute. The post-change watch lasts `AISRE_POST_CHANGE_WATCH_SECONDS` (default 3600, the provisional 60 minutes), shortened for tests and the demo; its flags are two new events, `aisre.verification.watch_flagged` and `watch_ended` (Artifact 7.5's list names them), and closing the incident ends it |
| Ineffective fix (pass 2) | The fake release process holds runtime overrides that the repository never sees, as a deployment platform's environment variables are. With `ORDER_TAX_RATE_BPS=8.25` overridden, the config revert passes its sandbox test and changes nothing live, so the Verifier fails it and the rollback runs: how FR-32 is shown without a misleading provider script. Each history entry has a `kind` (`start`, `apply`, `undo`, `manual`, `override`) |
| Release reconciliation (pass 2) | The provisional daily job (Artifact 5.2, ST-40) runs on the dispatcher, the only holder of the release credential, over the fake release process's change history. Its alert, `AisreReleaseChangeUnmatched`, is a new row in Artifact 12.3 that pages the Security lead; it uses RB-07's runbook until one is written |

- **Owner:** Security lead. **Reversal:** Low: each stand-in is one adapter or setting behind `Settings`. **Revisit when:** the identity system, the secrets manager and the release process are chosen, before M3 can be accepted.
- **Resolves:** building and demonstrating M3's approval and write path without those three choices, and without any production write.

### ADR-74: Stand-ins for M4's paging path and fault tests (2026-11-19, Proposed)

M4 asks that a page reach a person (Q13.46) and that each alert fire in a fault test (13.4, T4.6). There is no paging service and no on-call rota (ADR-69: rotas confirmed before M4), and the stack is one development host. M4 is built and tested against labelled stand-ins, and nothing here is a production choice.

| Topic | Decision |
| --- | --- |
| Paging path | Prometheus sends to Alertmanager (`prom/alertmanager:v0.30.0`, `ops/compose/alertmanager.yml`), which routes by the `route` and `also_route` labels of Artifact 12.3: the Platform owner by default, the Incident lead for `AisreQueueDepthHigh` and a copy of `AisreApprovalBacklog`, the Security lead for the four security alerts. Neither the Notifier nor PostgreSQL is on the path (NFR-25). `group_wait` 10 s keeps `AisrePostgresDown` inside NFR-07's 2 minutes; `repeat_interval` 4 h |
| Pager | A fake pager, `ops/fakes/pager.py`, its own Compose service (`fake-pager`), stands in for the paging service: each role is a webhook receiver, and the fake records every page in memory (`GET /v1/pages`). "Reaches a person" means recorded for that role's receiver; nobody is woken. Refused when `AISRE_ENV` is production. A deployment replaces the three webhook URLs and nothing else |
| Severity | `page` and `ticket` go to the same role's receiver, with `severity` on the page; a deployment may route tickets to a queue by that label |
| Alert rules | Every rule in `ops/alerts.yml` is fired by `promtool test rules` over synthetic series, past its hold, with its labels and annotations (`tests/integration/test_alert_rules.py`, Prometheus 3.16's promtool). That proves expression, hold and routing labels, not that anything publishes the metric |
| Fault-fired on this stack | `-m fault` (`tests/integration/test_alert_faults.py`): `AisrePostgresDown` with PostgreSQL stopped, paged to the Platform owner while it is still down; `AisreApprovalRefused` from a refused sign-in, to the Security lead; `AisreOutboxStuck` with Temporal stopped. `AisreWorkflowStalled` fires from real stalls the reconciliation job finds |
| Not fault-fired here | No publisher yet, so no fault can raise them: `AisreFirstSummarySlow`, `AisreCrashResumeSlow`, `AisreCancellationSlow`, `AisreActionOutcomeUnknown`, `AisreApprovalBacklog`, `AisreWritesFenced` (T4.4), the five spend alerts and `AisreUnreconciledUsage` (T4.9). Windows or holds too long for a test run: `AisreIntakeAvailabilityMonthly` (30 days), `AisreVerificationFailureRate` (7 days, 10 attempts), `AisreIntakeErrorRate` (5 min of 5xx), `AisreIntakeLatencyHigh`, `AisreIntakeLatencyP99High`, `AisreEventDeliverySlow`, `AisreModelErrorRate`, `AisreModelLatencyHigh`, `AisreFirstNotificationSlow`, `AisreTelemetryDropping` (10 to 30 min). Possible here but not yet written (pass 2, T4.7): `AisreModelAuthErrors` (a rejected provider key), `AisreModelOutputSecret` (a provider script), `AisreCircuitOpen` (10 failing calls in 60 s, then 5 min), `AisreQueueBacklog` and `AisreQueueDepthHigh` through the stack's worker (the burst runs through the activity, and its gauge crosses the threshold, in `test_queue.py`), `AisreSandboxLeak`, `AisreReleaseChangeUnmatched` and `AisreAuditChainBroken` through their jobs |
| Fault tests | Marked `fault`, deselected unless `-m` names it: they stop or kill this project's Compose services only, through `docker compose --env-file .env.example`, and put every service back up and healthy at the end. Never another Docker resource, never a prune |
| Kill tests | The worker: `SIGKILL` on the Compose service at each status and twice in a row, with scripted skills slowed to 3 s a step so a kill lands mid-step (`test_worker_kills.py`, `fault`). The dispatcher: a real dispatcher process, killed with `SIGKILL` between intent and send, during reconciliation, and after the fake release process applied, then a restarted one (`test_dispatcher_kills.py`, in the integration run). The approval path itself isn't killed on the stack: it needs a person and the Test Runner on the host |
| 24-hour limit | `IncidentWorkflowInput.limit_seconds` shortens a run's limit, never lengthens it; only tests set it, the relay never does. Shown at 15 s on the real Temporal server |
| Same-key retry (T4.1, ST-38) | The dispatcher's re-send while `applying` relies on the release process applying one execution key at most once: a second request with a key it has seen returns the first result and changes nothing. The fake release process does this; whether the real one does is unconfirmed (T3.1, the Platform owner), so the re-send is shown on the fake only. If the real process can't promise it, the re-send must be turned off and an unconfirmed send settle as `unknown` (the M3 behaviour) |
| Fence and restore commands (pass 2, T4.4) | `aisre fence|backup|restore` (`aisre.reliability.admin`, `admin_cli`) connect to PostgreSQL as `aisre_app`, unlike `aisre incident`, because ADR-69 gives them no HTTP route. The caller is a token verified against the issuer (`AISRE_TOKEN`), holding the role on every service (`aisre:platform_admin`, `aisre:incident_lead`); a lead's sign-off is a second verified token (`AISRE_SIGN_OFF_TOKEN`), the stand-in for a signed record. The rules are `fence.FenceState`'s, applied to the state read from PostgreSQL. Locally the tokens come from the fake issuer (personas `pat`, `ines`) |
| Backups and point-in-time recovery (pass 2, T4.4) | The stand-in for production PITR: the stack's PostgreSQL archives WAL with `archive_command` to a local folder (`.aisre-backups/wal`, `archive_timeout` 60 s), and `aisre backup take` runs `pg_basebackup` inside the postgres container between a `backup_quiesce` fence and its lift, writing a marker with the files' hashes and the actions in flight. A restore goes into a separate PostgreSQL, Compose service `postgres-restore` (profile `drill`, `ops/compose/pg-restore.sh`), never into the stack's data. One host, one disk: nothing is stored separately (NFR-46), nothing is scheduled nightly, and the object store has no manifest or versioned restore in the drill. The restore drill (`test_restore_drill.py`, `fault`) measures RPO and the two recovery times on this Mac; the decision on each unresolved action is the drill's, standing in for the incident lead. A production database with point-in-time recovery, a second location and the quarterly drill with people replace all of it |
| Retention and encryption (pass 2, T4.8) | The retention job runs daily in the worker as `aisre_retention` (`AISRE_RETENTION_DATABASE_URL`; the role's password is set by `postgres-init.sh` on a new volume), deleting each expired object with every version of it in the versioned bucket. Holds other than an open incident (reviews, legal and security holds) have no table, so only that hold applies. The encryption audit (`aisre.reliability.encryption_audit`) checks TLS on every configured connection and default bucket encryption; locally they fail, which is the true answer for this stack. It can't see disk or volume encryption from inside PostgreSQL (`unknown`), nor certificate quality or key management |
| Dashboards (pass 2, T4.7) | Grafana 12.2 in Compose, provisioned from `ops/grafana/` (no Prometheus consoles: Prometheus 3 ships none). Its PostgreSQL data source reads as `aisre_app` with the stack's fake password; a deployment gives it a read-only reporting role. Human wait and end-to-end time come from SQL (nearest rank, 12.9), the other four from histograms |
| Fault-fired in pass 2 | Through `test_alert_faults_more.py`: `AisreQueueDepthHigh` and `AisreQueueBacklog` (a burst through intake, severity confirmed while queued), `AisreModelAuthErrors` and `AisreModelOutputSecret` (the fake provider answers every request with one script when the key names it: `fake-script-auth-error`, `fake-script-leak-secret`), `AisreCircuitOpen` (only while failing calls keep coming: the breaker opens 30 s and its gauge drops when it next lets a call through, so a 5-minute hold needs a steady stream; worth revisiting), `AisreReleaseChangeUnmatched` (reconciliation triggered by hand), `AisreAuditChainBroken` (an event altered by the superuser in replica mode), `AisreWritesFenced` (a manual fence held 15 minutes). The paging series start at 0 (`auth_error` and unsafe `secret` counters), as pass 1 did for refusals. `AisreSandboxLeak` stays unfired: the host Test Runner isn't scraped |
| Load (pass 2, T4.10) | No Artifact gives an intake rate, so the expected peak is assumed to be NFR-16's burst, 20 alerts a minute, and 10x is 200 a minute (Platform owner to confirm). The test runs on one Mac with every service on it: it shows the path holds, not capacity |

- **Owner:** Platform owner. **Reversal:** Low: the pager is three URLs in one file; the fault tests are tests. **Revisit when:** a paging service and rotas are chosen (ADR-69), before M4 can be accepted. Anything shown only through these stand-ins is Partial, never Met.
- **Resolves:** building and testing M4's paging path and fault tests without a paging service, rotas or a second host.

### ADR-75: Pilot readiness for M5 (2026-11-19, Proposed)

M5 is a live advisory pilot on real incidents (Artifact 13.4, T5.1 to T5.7). None of it can run on the development host: no production, no monitoring account, no release process access, no model key and no pilot users. M5 builds what the pilot needs so that it can start when those are ready. Nothing here is a pilot result, and the Release B decision (T5.7) is not written.

| Topic | Decision |
| --- | --- |
| Evidence sources per service | `AISRE_EVIDENCE_SOURCES` keeps its `"sources"` map (tool or `"*"` to an adapter) as the platform-wide fallback, and adds `"services"`: the same map keyed by `service` or `service/environment`. The most specific scope that names the tool, or has a `"*"`, answers: service and environment, then service, then platform. A service no scope covers has no source, so each call is a gap (ADR-54), never another service's source. Tool names are checked against `SOURCE_TOOLS` (the registry's READ tools less the two the corpus answers), so a misspelt tool is refused at start, not silently a gap. The adapter receives the same call as before; adapters still drop anything labelled with another service (FR-44) |
| Go-live check (T5.1) | `aisre pilot check` (aisre.pilot.check) reads the production environment and refuses unless Release A holds. `remediation_enabled` already exists (aisre.remediation.release) and is derived, not a setting: on only in development or test with `fake_release`, so it is off in production by construction; the check applies that rule to the variables. Any `AISRE_RELEASE_*` variable fails, so a release adapter's credential added later (pass 2) is refused for Release A without a code change. "A real model" means a provider key that isn't a fake one, the provider's own HTTPS URL or none, and every task alias pinned in `CLAUDE_MODEL_IDS`. Paging is Alertmanager's file, not a setting: a receiver with a destination for each of Artifact 12.3's three roles and no `fake-pager`, read by a line scan because PyYAML is a development dependency only. It can't tell that a credential works, a source answers or a page reaches a person |
| The incident review's findings (T5.2, T5.3) | Artifact 1.6's pilot accuracy, ADR-65's false-established and under-escalation, the baselines' time to cause, engineer hours and reversed fixes all need what the incident review finds, and nothing recorded it. Migration 0013 adds `incident_reviews`, one row per incident, written once through `POST /v1/incidents/{id}/incident-review` with ADR-50's "Confirm severity, close" permission (the Incident lead; no new permission row), naming the current diagnosis; a `note_added` event records that it was written (no new event type). `proposed_cause_confirmed` is required exactly when that diagnosis named a cause. Pilot definitions: false-established is a named cause the review didn't confirm over named causes reviewed; under-escalation is a named cause with an action proposed where the review says the evidence should have led to a hand-over, over those the review says so of (the harness's "escalated" is inconclusive or no action). "Change-caused" is the responders' category `deployment_code` or `deployment_config`. Alert time is the incident's creation. Rows go only with the incident's purge (ON DELETE CASCADE) |
| Reports (T5.2 to T5.4) | `aisre pilot report`, `baseline` and `approvals` read as a new role, `aisre_reporting`, SELECT on the tables they read and nothing else (migration 0013), and refuse any other role. The baseline comes from a JSON file of earlier incident records or from the first 30 reviewed change-caused incidents in the range (1.6's fallback); time to verified recovery is Release B's and not computed. Output is "pilot" only with `AISRE_ENV=production` and no development-signed approval in range, otherwise "synthetic". The approval review lists what FR-25 shows the approver and the decision times; the diff's text stays in the object store, out of the role's reach, and is named by artifact and hash. The approver's D3 judgement is left blank for the approver |
| Templates (T5.6, T5.7) | `pilot/final-report-template.md` (13.10's 13 sections, section 8 mapped to the report's JSON keys) and `pilot/decision-record-template.md` (HG-10's fields), both marked not yet run. No pilot result and no Release B decision is written |
| Onboarding | `docs/onboarding.md`: catalogue rows (no catalogue route exists; a SQL insert for the pilot), per-service evidence sources, runbooks through `aisre.retrieval.corpus`, identity groups per role and service, the repository named after the service, no release configuration in Release A, paging receivers, then `aisre pilot check`. Found while writing it: the only review adapter, `fake_review`, is refused in production, so in the pilot `draft_patch` fails and hands over saying so: no patch is drafted or tested until a real review adapter exists (pass 2 adds GitHub, below) |
| Evidence adapters (pass 2) | `prometheus` (`get_metric`), `loki` (`get_logs`), `datadog` (`get_metric`, `get_logs`, `get_changes` from deployment events) and `cloudwatch` (`get_metric`, `get_logs` by FilterLogEvents) are in `ADAPTERS`, loaded lazily, none in `LOCAL_ONLY`. Each is read-only and implements the Source contract unchanged: the call's window, `limit` rows with `partial` when the vendor says there is more, at most 4 MB read and 20 s per call, failures as SourceUnavailable (a gap, ADR-54); retries, the breaker, redaction and the output cap stay the collector's. Queries are the service owners' configuration with `$service` and `$environment` put in through each query language's quoting (PromQL and LogQL string escapes; Datadog and CloudWatch accept plain values only, else a gap). Credentials are by reference, `{"env"}` or `{"file"}`, read when a call needs them, never at construction (so `pilot check` builds every adapter with no network call or secret) and read through `config.referenced_value`, keeping "only config reads the environment"; the check adds a line that every referenced credential is present. CloudWatch uses boto3 as the optional `aws` extra (smaller and better tested than our own SigV4 and credential chain) and refuses at start without it. Datadog events carry no files or configuration keys, so change facts are thinner than the fake's. No adapter answers `get_alert`, `get_traces`, `get_error_groups` or `get_dependency_health`, so the go-live check still fails for a pilot service until each has a source or the user decides those may be gaps. All four are tested against recorded responses shaped after the documented APIs (Stubber for AWS), never a real account: Partial |
| Review system on GitHub (pass 2) | `AISRE_REVIEW_ADAPTER=github` (aisre.remediation.github): GitHub REST 2022-11-28 on github.com or an Enterprise `/api/v3` URL, `AISRE_REVIEW_TOKEN`, `AISRE_REVIEW_REPOS` (service to `owner/repo`, FR-48). Allowed in production: Release A drafts pull requests (ADR-44, M2). It reads as the fake does (SHA-256 before and after per changed file, from raw content) and opens a draft only: checks each file's hash at the base revision and after applying the hunks, writes one commit through the Git database API on `aisre/<action_id>`, opens the pull request with `draft: true`, and finds its own pull request or branch again on a retry. Writes are an allow-list checked before any request, and `/merge` or `/merges` is refused whatever the token allows. Limits: files are written with mode 100644, `refs` are not read. **Decision: a pilot service with no real review system is a FAIL**, not a WARN: without one every `draft_patch` hands over, and the pilot would measure no proposed fix at all, which is half of what Release A is for |
| Release process on GitHub Actions (pass 2, T5.5 prep, D8) | `AISRE_RELEASE_ADAPTER=github_actions` with `AISRE_RELEASE_GITHUB` (repository, apply and undo workflows, ref) and `AISRE_RELEASE_TOKEN`: apply and undo are workflow_dispatch events, current state, lookup by key and history come from GitHub Deployments, and runs answer a key dispatched but not yet deployed. Refused in production by Settings and `release_from_settings`, and without a URL and token; its variables are `AISRE_RELEASE_*`, so the go-live check fails while it is configured. Production write stays impossible until HG-10. `remediation_enabled` (and the API's copy) is now on in development or test with any release adapter, not only `fake_release`, so T5.5 can run against a test environment; production is unchanged. **GitHub does not give idempotency by key**: a dispatch has no key, returns no run, and runs twice if sent twice. The adapter keeps ST-38's guarantee by looking the key up (deployments, then runs) before every dispatch and never sending a key it can see, and by passing the key as a workflow input that the workflow must check, under a per-deployment concurrency group, before creating a deployment that carries it (the module docstring is that contract). The gap between a dispatch and its run appearing is seconds, inside the dispatcher's in-flight window, before which ST-38 never resends. A run that succeeded without a deployment for its key is an unknown outcome for a person. Tested against recorded responses only, and the workflows themselves are the release owners' to write: Partial |

- **Owner:** Platform owner. **Reversal:** Low: configuration, a check command and reports. **Revisit when:** the pilot's systems and people are named, before T5.1 starts.
- **Resolves:** building M5's go-live check, measures and templates without a production environment.

## Not conflicts

| Claim | Finding |
| --- | --- |
| Audit C6, approval expiry 15 vs 60 minutes | Two different timers, consistent everywhere. See ADR-49 |

## What no decision can settle

Decisions can't supply these. Each stays open in its artifact, marked `**Needs the team:**` or `**Needs measurement:**`.

| Item | Owner | Settled when |
| --- | --- | --- |
| Named people for each role | Engineering manager | Names entered in this register |
| Team review and sign-off of every artifact and decision | All role owners | "Reviewed by" filled in |
| Claude vs ChatGPT disagreements, agent per conversation | Team | Conversation logs filled in. The share link needs sign-in |
| Baseline time to cause and recovery, reversal rate | SRE lead | Measured from past incidents |
| Real alert samples (SRE lead), peak concurrency (Platform owner), personal data in logs (Security lead) | As named | Samples inspected (System Design section 10) |
| Release process supports apply, undo and lookup by key | Platform owner | Scenario revert done through it |
| On-call rotas and night coverage | Engineering manager | Rotas confirmed |
| Provider terms on retention and training | Security lead | Terms confirmed |
| Actual monthly budget and hourly cost | Engineering manager | Finance confirms (ADR-56) |
| The frozen suite and its baseline run | SRE lead | Built from the specification in Artifact 12, then run once |
| Sandbox checkout seeding: how the code at `base_revision` reaches a no-network sandbox | Security lead | Decided before M2 |
| Identity system and secrets manager choice | Security lead | Chosen before M3 |
| Temporal server version | Platform owner | Pinned at M0 |
| Where write-ahead logs are archived | Platform owner | Decided before the M4 restore drill |
| Provider availability record | SRE lead | Read from the provider's status history before M1 |
| Labelled retrieval query set; frozen suite `suite-1.0` and its baseline | SRE lead | Built at M1 and M4 (Artifact 13) |
| Layer 13 open items | Owners in Artifact 13, 13.11 | Each has a milestone deadline there |
| Whether the amended behaviour works in a running system | Platform owner | The remaining items in [code-changes-required.md](code-changes-required.md) done, and the 21 skipped security tests run |
| Legal deletion of runbook text: `documents` and `chunks` are append-only, so `purge_document` can't remove body text from PostgreSQL | Security lead | A deletion path is decided |
