# Decision Register

The one decision log the worksheet asks for (Q2.23). It settles the cross-layer gaps and conflicts found in Artifacts 1–12.

**Status of everything here: Proposed (2026-11-02), pending team sign-off.** Owners are roles, because the sources name no people. Each decision can be overturned in review; change it here first, then in the artifacts it lists.

## How to read the docs

When two documents disagree, the higher one wins:

1. This register
2. The layer artifacts (`artifact-*.md`)
3. The [System Design](reference/system-design.md) and [Technical specification](reference/technical-specification.md), as amended by the notes at their top
4. `conversation.md`: the team's original answers, kept unchanged as the record of how each decision was reached
5. `files/`: reference code, which now implements these decisions. The Room 7 originals are in git commit 013bcf4

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
| Incident | `handed_over` → `investigating` | A person requests re-investigation. New workflow run `incident-<id>-r2`. No automatic path exists |
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
| Dispatcher and database | Its own role: read actions, patches, approvals and verification results; write `execution_attempts`, action status for `applying` and its outcomes, and its own timeline events. Listed in the component summary |
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
2. A patch candidate is one `draft_patch` activity, up to 3 per action. Steps are `COLLECT_BASELINE`, `RETRIEVE_KNOWLEDGE` (added), `HYPOTHESISE`, `COLLECT_TARGETED`, `DIAGNOSE`, `CRITIQUE`, then a stop verdict.
3. A skill failure returns a typed failure to the coordinator, which stops as inconclusive.
4. Crash recovery: Temporal re-runs the activity once on another worker (ADR-27). A completed model result is reused by `input_sha256` (ADR-61), so nothing is re-billed.
5. Model calls happen in hypothesise, diagnose, critique and patch proposal, always through the gateway. Technical specification section 5 is amended.
6. `ModelDiagnosisOutput` is replaced by `DiagnosisDraft`. Field names follow `agent_contracts.py`, and ADR-64 aligns storage.
7. Limits read from one configuration object. Tokens and spend are added to the termination policy.
8. Invalid output: 3 gateway attempts per step, then the step fails. A critic failing twice ends the investigation as inconclusive, so a critic can't exceed 6 attempts total.
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
| Secret in model output | Both: the output fails validation and is retried. If stored for audit, the secret is masked first. The leak procedure applies |
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

`layers/layer-07-data-apis-events/files/schema.sql` (proposed) is the canonical schema, tested on a real PostgreSQL instance.

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
| Table count | 34 tables, not 22, including `investigation_queue`, `fence_state` and `fence_reasons` for Layers 8 and 12, and `reservation_corrections` for ADR-61. Technical specification section 4 is amended |
| Digests | `action_digest` and `test_bundle_digest` are write-once, set at `testing` → `awaiting_approval`, because the test bundle doesn't exist when the action is created |
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
| Real alert samples, peak concurrency, personal data in logs | SRE lead, Platform owner, Security lead | Samples inspected (System Design section 10) |
| Release process supports apply, undo and lookup by key | Platform owner | Scenario revert done through it |
| On-call rotas and night coverage | Engineering manager | Rotas confirmed |
| Provider terms on retention and training | Security lead | Terms confirmed |
| Actual monthly budget and hourly cost | Engineering manager | Finance confirms (ADR-56) |
| The frozen suite and its baseline run | SRE lead | Built from the specification in Artifact 12, then run once |
| Whether the amended behaviour works in a running system | Platform owner | The remaining items in [code-changes-required.md](code-changes-required.md) done, and the 21 skipped security tests run |
| Legal deletion of runbook text: `documents` and `chunks` are append-only, so `purge_document` can't remove body text from PostgreSQL | Security lead | A deletion path is decided |
