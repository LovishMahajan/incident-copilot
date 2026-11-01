# Artifact 11: Safe Remediation Specification

- **Layer:** 11, Patch Generation, Sandboxing, Approval and Execution
- **Built from:** Artifacts 5, 8 and 9
- **Status:** Draft compiled from the team's answers; pending team review
- **Sources:** [conversation.md](conversation.md), [files/patch.py](files/patch.py), [files/sandbox.py](files/sandbox.py), [files/action.py](files/action.py), [files/dispatcher.py](files/dispatcher.py), [files/verification.py](files/verification.py), [files/test_remediation.py](files/test_remediation.py), [System Design](../../reference/system-design.md), [Technical specification](../../reference/technical-specification.md), [Layer 8 workflow.py](../layer-08-workflows-state-machines/files/workflow.py), [Layer 7 schema-tables.sql](../layer-07-data-apis-events/files/schema-tables.sql)

## Required sections

| Required section | Questions this section answers | Done |
| --- | --- | --- |
| 11.1 Patch schema | Q11.1, Q11.4–Q11.7, Q11.10–Q11.12 | Yes |
| 11.2 Diff and hashing strategy | Q11.2, Q11.3, Q11.8, Q11.9 | Yes |
| 11.3 Sandbox threat model | Q11.13, Q11.14, Q11.22, Q11.23, Q11.31–Q11.34 | Yes |
| 11.4 Resource isolation | Q11.15–Q11.21, Q11.24–Q11.30 | Yes |
| 11.5 Trusted test harness | Q11.35–Q11.48 | Yes |
| 11.6 Approval API | Q11.50, Q11.53, Q11.54, Q11.58, Q11.60–Q11.64 | Partial |
| 11.7 Authorization matrix | Q11.49, Q11.51, Q11.52, Q11.59, Q11.65, Q11.66 | Yes |
| 11.8 Immutable action schema | Q11.54–Q11.57, Q11.70 | Yes |
| 11.9 Dispatcher contract | Q11.65, Q11.67–Q11.69, Q11.73 | Yes |
| 11.10 Idempotency ledger | Q11.70–Q11.72, Q11.77, Q11.78 | Yes |
| 11.11 Reconciliation strategy | Q11.74–Q11.78 | Yes |
| 11.12 Verification predicates | Q11.79–Q11.88 | Partial |
| 11.13 Rollback strategy | Q11.89–Q11.92 | Partial |

## 11.1 Patch schema

*Answers Q11.1, Q11.4–Q11.7, Q11.10–Q11.12.*

**Owner:** the Diagnosis Engine drafts the patch (SRE lead). The edit policy is owned by the security lead. Code, not the model, computes hunks and hashes.

A proposed change is a `Patch` record ([Q11.1](conversation.md); [patch.py](files/patch.py)):

| Level | Fields |
| --- | --- |
| Patch | `incident_id`, `diagnosis_id`, `candidate_no` (1–3), `action_kind`, `repository`, `base_revision`, `target_service`, `target_environment`, `files`, `target_release` (release rollback only), `summary` (10–1000 characters) |
| File | `path`, `op` (`modify` only in the first version), `old_sha256`, `new_sha256`, `hunks` (at least one) |
| Hunk | `old_start`, `old_lines`, `new_start`, `new_lines`, `lines`, `rationale` (10–600 characters), `evidence_ids` (at least one) |

The model proposes the new content; `build_patch_from_contents` computes the hunks and hashes, so the model can't misstate what changes.

**Editable boundaries** come from a versioned `EditPolicy` ([Q11.4](conversation.md)):

| Action kind | May edit |
| --- | --- |
| Configuration revert | `config/**` YAML, TOML and JSON only |
| Commit revert | Application source and config, as in the reverted commit |
| Release rollback | No files: selects a previous release |

- **Always protected, whatever the policy says:** tests and test configuration; CI files; security policies, CODEOWNERS; the sandbox launcher and harness; deployment files and Dockerfiles; lock files, secrets and keys.
- **Refused:** paths with `..`, absolute paths, and a patch with any one file outside its kind's list.

**Revision pinning** ([Q11.5](conversation.md)): `base_revision` is part of the patch, its digest, the immutable action and the approval. Each file records `old_sha256`, checked when the patch is applied. A newer deployment means a new patch, a new test and a new approval.

**Test and policy protection** has three independent barriers ([Q11.6](conversation.md)): the boundary check rejects protected paths before testing; tests run from the separately built and pinned harness image; and the review system requires the security lead's approval for protected paths, which the platform's own identity doesn't have.

**Rationale** ([Q11.7](conversation.md)): per hunk, `rationale` and `evidence_ids`; per patch, a `summary` and the diagnosis it implements. Both are excluded from the digest and stored permanently.

**Candidates** ([Q11.10](conversation.md), [Q11.11](conversation.md)):

- Each candidate is its own patch and its own action, with the same `diagnosis_id` and its own `candidate_no`.
- At most 3 per diagnosis, drafted in order: configuration revert, commit revert, release rollback.
- Tested one at a time, stopping at the first that passes. Identical digests are tested once.
- Each draft counts against the model-call budget, and each test against a sandbox budget (provisional: 3 runs per incident, 45 minutes of sandbox time in total).
- The approval screen shows candidates side by side, smallest reversal first. Approving one approves only that one; the others are withdrawn when it executes.

**When nothing passes** ([Q11.12](conversation.md)): nothing is deployed. The action becomes `test_failed` and the incident goes to `handed_over`, with the diagnosis and evidence, each candidate's diff and test results, the reproduction test, and a suggestion of the safest manual option, labelled as a suggestion. There is no "best effort" deployment and no relaxing of tests.

## 11.2 Diff and hashing strategy

*Answers Q11.2, Q11.3, Q11.8, Q11.9.*

**Owner:** code in [patch.py](files/patch.py) computes diffs and digests. No separate owner is named beyond the Diagnosis Engine's SRE lead.

**Display** ([Q11.2](conversation.md)): a standard unified diff from `Patch.unified_diff()`, with 3 lines of context, `# why:` with rationale and evidence IDs above each hunk, and the patch digest and base revision in the header. The same diff is opened as a pull request in the review system.

**Structured diff** ([Q11.3](conversation.md)):

- A replacement file is never accepted as the patch. Code diffs new content against the file at the base revision (Python `difflib`), giving minimal hunks with context.
- At most 3 files and 40 changed lines per patch (configurable). More is refused as `too_large`, so a near-total rewrite can't hide a small malicious edit.
- Hunks are typed, so the review system, the sandbox and the digest all use the same structure.

**Identity** ([Q11.8](conversation.md)): `Patch.digest` is SHA-256 over canonical JSON (sorted keys, no whitespace) of:

- repository and base revision
- target service and environment
- action kind, target release
- each file, sorted by path, with its path, before and after content hashes, and every hunk

The same edit against a different revision gives a different digest. A reworded summary gives the same digest.

**Digest binding at every step** ([Q11.9](conversation.md)):

| Step | Binding |
| --- | --- |
| Test | The result bundle records the patch digest it ran; `classify` treats a mismatched bundle as crashed |
| Immutable action | Records the patch digest and the test bundle digest |
| Approval | Signs the action digest |
| Dispatcher | Re-checks all of it |

A new version from the model is a new patch with a new digest, needing a new test and a new approval. Patches are append-only.

## 11.3 Sandbox threat model

*Answers Q11.13, Q11.14, Q11.22, Q11.23, Q11.31–Q11.34.*

**Owner:** the Test Runner and `SandboxSpec` are owned by the security lead. Never the model, never the patch ([Q11.33](conversation.md)).

| Threat | Why it matters | Control |
| --- | --- | --- |
| Malicious patch run on the production server ([Q11.13](conversation.md)) | It would run with the application's database credentials and customer data, network routes, secrets and cloud metadata, before any test or person could intervene | Generated code runs only in the isolated environment |
| Escape to the host ([Q11.14](conversation.md)) | Ordinary containers share the host kernel | Container under gVisor (`runsc`; ADR-10), on a separate host from production and the platform's own processes |
| Credential theft ([Q11.22](conversation.md)) | Anything in the sandbox is readable by the untrusted code | Fake, test-only values and local stand-ins. The launcher passes only `AISRE_SANDBOX=1`, and refuses variables that look like secrets |
| Docker socket ([Q11.23](conversation.md)) | Equivalent to root on the host | Launcher refuses any mount of `docker.sock`, `containerd`, `/proc`, `/sys`, `/dev`, `/etc` or the Docker data directory. The test host runs only sandboxes |
| Patch changes the sandbox configuration ([Q11.33](conversation.md)) | It could add mounts, devices, capabilities, network or variables | `SandboxSpec` is versioned platform code; arguments come only from `launch_args` and are checked by `validate_args` |
| Patch changes the launcher ([Q11.34](conversation.md)) | It could remove the isolation | Launcher is in the platform's own codebase; `sandbox/**` and `harness/**` are protected; the harness runs from the pinned image; platform changes need security-lead code-owner review through a separate pipeline |

**Limits of ordinary containers** ([Q11.31](conversation.md)): a shared kernel; easy misconfiguration; runtime escape flaws; side channels and resource contention; default settings that vary. Hence gVisor, a dedicated host, validated launch arguments, and nothing valuable in reach.

**When to move to stronger isolation** ([Q11.32](conversation.md)). Firecracker microVMs are the next step if:

- an escape test fails under gVisor
- tests need system calls gVisor doesn't support
- the platform starts running code with network access or real data
- compliance requires virtual-machine isolation

Re-evaluate when the sandbox's purpose widens, or after any security incident.

**Remaining risk** ([System Design §11](../../reference/system-design.md)): unknown escape flaws; nothing valuable inside limits the impact.

## 11.4 Resource isolation

*Answers Q11.15–Q11.21, Q11.24–Q11.30.*

**Owner:** the security lead owns the values, which are provisional ([sandbox.py](files/sandbox.py)). The launcher applies them.

| Resource | Limit | Enforced by | On breach |
| --- | --- | --- | --- |
| CPU | 2 CPUs (`--cpus=2.0`) | cgroup quota | Throttled; too slow means timeout ([Q11.24](conversation.md)) |
| Memory | 2 GB, no extra swap (`--memory=2g`, `--memory-swap=2g`) | cgroup limit | Out-of-memory kill; failed or crashed, never passed ([Q11.25](conversation.md)) |
| Processes | 256 (`--pids-limit=256`) | cgroup limit | Creation fails; run ends in failure or timeout ([Q11.26](conversation.md)) |
| `/tmp` | 1 GB, in memory, no-exec | tmpfs | "No space"; run fails ([Q11.27](conversation.md)) |
| Workspace | 2 GB | Size-limited volume | "No space"; run fails ([Q11.27](conversation.md)) |
| Image filesystem | Read-only | `--read-only` | Nothing can be written |
| Returned logs | 50 MB, stored as an artifact | Harness | Capped ([Q11.27](conversation.md)) |
| Each test | 120 s | Harness | Timeout ([Q11.28](conversation.md)) |
| Whole run | 900 s wall clock; stop, then kill after 10 s | Launcher | Timeout ([Q11.28](conversation.md)) |
| Workflow | Activity timeout and heartbeat | Artifact 8 | Timeout ([Q11.28](conversation.md)) |

Each sandbox gets its own cgroup, so one run's runaway process can't starve another run or the host's own services ([Q11.17](conversation.md), [Q11.19](conversation.md)).

**Namespaces** ([Q11.15](conversation.md), [Q11.16](conversation.md), [Q11.18](conversation.md)):

| Namespace | Isolates | Setting |
| --- | --- | --- |
| Mount | Filesystem view | Read-only image, workspace volume, bounded tmpfs; no host paths |
| Network | Interfaces and routes | `--network=none`: loopback only |
| PID | Process tree | Private |
| IPC | Shared memory and queues | Private |
| UTS | Host name | Private |
| User | User IDs | Runs as user 65534, never root |

**Privileges** ([Q11.20](conversation.md)): `--user=65534:65534`, with user 0 or root refused; all Linux capabilities dropped; `no-new-privileges`; never `--privileged`, added capabilities or host devices; disabling default system-call filtering is refused.

**Network** ([Q11.21](conversation.md)): no network by default. Dependencies come pre-installed in the pinned image, and a patch can't add them. If a package mirror is ever needed, it is the only reachable destination, through a filtering proxy that logs every request. Attempts to reach out are recorded as an injection or abuse signal.

**Clean runs** ([Q11.29](conversation.md)): a fresh container from the pinned image, a new workspace volume checked out at `base_revision`, and the patch applied by the harness. No reused volumes, caches or databases. Each run has its own ID.

**Cleanup** ([Q11.30](conversation.md)):

- `--rm` deletes the container on exit; the launcher deletes the workspace volume.
- A reaper runs every 5 minutes, removing anything labelled `aisre.sandbox=1` older than 60 minutes (FR-23 target: none left after 60 minutes).
- Cleanup is part of the Test Runner activity's `finally`; cancellation also triggers it.
- Leftover counts are a platform metric, alerted above zero.

## 11.5 Trusted test harness

*Answers Q11.35–Q11.48.*

**Owner:** the Test Runner (security lead). The harness, not the patch or the model, produces the result.

**Suites, all required** ([Q11.35](conversation.md)–[Q11.37](conversation.md), [Q11.40](conversation.md)):

| Suite | Proves |
| --- | --- |
| Reproduction, before and after | The original symptom existed, and is gone |
| Existing unit suite | Nothing else broke at function level |
| Existing integration suite (real PostgreSQL, same version as production, started fresh in the sandbox) | Nothing broke at the service boundaries |
| Smoke suite | Key user paths still work |
| Error-signature comparison | No new error types |

A test the model wrote can be added as extra evidence, but never replaces these and never counts toward "passed" on its own.

**Reproduction test** ([Q11.38](conversation.md), [Q11.39](conversation.md)): built by the harness from the evidence, not by the model.

- **Configuration:** production values at the failing revision, with secrets replaced by fakes.
- **Request:** the failing path, method and payload shape from the traces, with personal data removed.
- **Assertion:** the original symptom. In the reference case: start with `payments.timeout_ms: 0`, call `/api/pay`, expect a 500 with the timeout error.
- **Before:** it must fail on the unpatched base revision, or the outcome is `not_reproduced` and testing stops.
- **After:** it must pass, in the same environment.

**Regressions** ([Q11.37](conversation.md), [Q11.41](conversation.md)): the full suite runs at the base revision and with the patch. A test that passed before and fails after is a regression. Any error signature present only after the patch is a `new_error_signature`. Either gives `failed`.

**Harness independence** ([Q11.42](conversation.md), [Q11.43](conversation.md)):

- Tests come from the base revision, never from the patch.
- The harness lives in the pinned image, on the read-only filesystem, outside the workspace.
- The patch is applied only to `/workspace`.
- `conftest.py`, `pytest.ini` and `pyproject.toml` are protected.
- The harness reads results from its own test runner, not from files in the workspace.

**`ResultBundle`** ([Q11.44](conversation.md), [Q11.45](conversation.md)): run ID, patch digest (computed by the harness from the applied content), base revision, image digest, sandbox spec version, harness version, reproduction before and after, each suite (required, ran, passed, failed, errors), new error signatures, exit code, duration, timed-out flag, and the fingerprint of the full log (secret-scanned, capped at 50 MB). Nothing else leaves: no workspace files, no binaries. The bundle's own digest goes into the action as `test_bundle_digest`.

**Outcomes** (`classify`):

| Outcome | Meaning | What happens next |
| --- | --- | --- |
| `passed` | Reproduced before, gone after, no regressions, all suites ran | May go to approval |
| `failed` | Failure remains, or a regression appeared | Next candidate, or hand over |
| `not_reproduced` | Original failure never appeared | Testing stops |
| `inconclusive` | A required suite has `ran = false` ([Q11.48](conversation.md)) | Never reaches approval; one retry if the cause is infrastructure, otherwise hand over |
| `timeout` | Wall clock or per-test limit hit ([Q11.47](conversation.md)) | Not rerun automatically; hand over with the hung test's name |
| `crashed` | No valid bundle, no exit code, or a bundle for a different patch ([Q11.46](conversation.md)) | One rerun as a new run ID, within the sandbox budget; a second crash hands over with the logs |

See conflict C13: the code's `classify` doesn't yet match the "never passed" rule for a non-zero exit.

## 11.6 Approval API

*Answers Q11.50, Q11.53, Q11.54, Q11.58, Q11.60–Q11.64.*

**Owner:** the Approval Service (security lead). It holds the signing key, in the API process. It decides nothing itself: a named person does.

**Endpoints:**

| Endpoint | Purpose | Source |
| --- | --- | --- |
| `POST /v1/actions/{id}/approvals` | Approve one exact action | [Technical specification §8](../../reference/technical-specification.md) |
| `POST /v1/actions/{id}/rejections` | Reject, with a reason | [Technical specification §8](../../reference/technical-specification.md) |
| `POST /v1/actions/{id}/revocations` | Withdraw an approval, with the approval ID, the action digest and a reason | [Q11.63](conversation.md) |

**The API accepts only** ([Q11.61](conversation.md)): the action ID (in the path), the action digest the approver saw, the decision, and a reason. Everything else is read from trusted storage. If the call carried the patch or target, the screen could show patch A while the request approves patch B.

**Identity** ([Q11.50](conversation.md)): single sign-on with two-factor. The API receives a short-lived signed token with the subject, authentication time and two-factor claim. Approval needs a sign-in within the last 15 minutes, with two-factor.

**Refusals** in `record_decision` ([action.py](files/action.py)):

| Code | Cause |
| --- | --- |
| `digest_mismatch` | The presented digest differs from the current action |
| `self_approval` | The subject is a proposer or a `system:` identity |
| `reauth_required` | Sign-in too old, or no two-factor |
| `forbidden` | No permission on the action's target service |
| `reason_required` | A rejection without a reason |
| `not_own_approval` | Revoking someone else's approval |
| `duplicate` | The same person approving twice |

**Gap:** the sources give only the group "401, 403 or 409; nothing recorded" (Technical specification §6). No source maps each refusal code to its status.

**What the approver reviews** ([Q11.53](conversation.md)), in this order. A notification is only a link; decisions are made on this screen, in the CLI or API.

1. The action: kind, exact target, base revision and expected revision after, and the action digest.
2. The diff, with each hunk's rationale and evidence links.
3. The diagnosis: leading cause and confidence, evidence for and against, and alternatives with why they were ruled out.
4. Test results: reproduction before and after, each suite, new error signatures, bundle digest.
5. Risk and the rollback action, with its own digest.
6. Recovery predicates.
7. Flagged items: gaps, stale evidence, text flagged as possible injection.
8. Expiry of the request and of any approval.

**Binding** ([Q11.54](conversation.md)): the decision records `action_digest` and is signed with Ed25519. Any edit to the decision fails verification. A changed patch is a new action with a new digest that no one has approved.

**Expiry** ([Q11.58](conversation.md)):

| Clock | Value | Enforced by |
| --- | --- | --- |
| Request | Escalates at 15 minutes, expires at 60 | Workflow timer; `evaluate` returns `EXPIRED` |
| Granted approval | Valid 15 minutes (`valid_until`) | `evaluate` stops counting it |

Expired requests are closed, the action becomes `expired`, and nothing executes.

**Conflicting decisions** ([Q11.60](conversation.md)): any valid rejection wins, regardless of order or number of approvals. `rejected` is final and the incident hands over. Both decisions are kept. Changing course needs a new action.

**Rejection is enforced in three places** ([Q11.62](conversation.md)): the workflow (`rejected` has no outgoing transitions), the database (an immutable `approval_decisions` row), and the dispatcher (`evaluate` returns `REJECTED`, and it refuses even if a stale workflow asked).

**Revocation** ([Q11.63](conversation.md)):

- Recent sign-in with two-factor is required.
- Only the person who approved can revoke. Anyone with reject rights can instead reject.
- It is a new signed decision; the original stays in the record.
- It is effective until execution starts. After that, undoing the change is a rollback action.

**Audit record** ([Q11.64](conversation.md)): approval ID, action ID, action digest; decision; subject and roles held at the time; authentication time and two-factor claim; policy version, decision time, valid until, reason; signing key ID and signature. It is linked to the action, the execution ledger entry and the verification result. The timeline is hash-chained and copied to a separate store.

## 11.7 Authorization matrix

*Answers Q11.49, Q11.51, Q11.52, Q11.59, Q11.65, Q11.66.*

**Owner:** the Approval Service performs rights checks (security lead). Security administrators own policies and keys. Approval rules by severity are an open decision for the engineering manager, before M3 ([System Design §12](../../reference/system-design.md)).

**Separation of concerns.** No component both proposes and authorises a change:

| Concern | Component | Owner | Holds | Can't do |
| --- | --- | --- | --- | --- |
| Propose | Diagnosis Engine (patch draft) | SRE lead | Create a draft pull request (Q11.66, as "Patch Proposer") | Merge, deploy |
| Test | Test Runner | Security lead | Create sandboxes on the test host | Reach production |
| Authorise | Approval Service, on a named person's decision | Security lead | Sign decisions | Deploy |
| Execute | Trusted Dispatcher | Security lead | `apply`, `lookup` and `current_state` on named deployments | Approve, sign, run arbitrary commands |
| Verify | Recovery Verifier | SRE lead | Read monitoring | Write anything |
| Investigate | Evidence Collector | SRE lead | Read, per tool, per service | Write |
| Reason | Agent and model | SRE lead (Diagnosis Engine, Model Gateway) | Nothing | Anything external |

Holding the dispatcher's credential doesn't let anyone approve. Holding the signing key doesn't let anyone deploy ([Q11.66](conversation.md)).

**Role matrix** (`MATRIX` in [action.py](files/action.py)):

| Permission | Viewer | Responder | Approver | Incident lead | Platform admin | Security admin | `system:workflow` | `system:dispatcher` |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `incident:read` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `incident:create` | — | ✓ | ✓ | ✓ | — | — | ✓ | — |
| `incident:cancel` | — | ✓ | ✓ | ✓ | — | — | — | — |
| `action:approve` | — | — | ✓ | ✓ | — | — | — | — |
| `action:reject` | — | ✓ | ✓ | ✓ | — | — | — | — |
| `approval:revoke_own` | — | — | ✓ | ✓ | — | — | — | — |
| `incident:close` | — | — | — | ✓ | — | — | — | — |
| `action:execute` | — | — | — | — | — | — | — | ✓ |
| `policy:edit` | — | — | — | — | — | ✓ | — | — |
| `sandbox:configure` | — | — | — | — | ✓ | — | — | — |
| `keys:manage` | — | — | — | — | — | ✓ | — | — |

**Key separations** ([Q11.52](conversation.md)):

- administrators can't approve changes
- approvers can't change policy or keys
- the platform's own identities can't approve: the workflow proposes, and the dispatcher executes only
- responders can reject, but not approve, so anyone on the incident can stop a bad change

**Why the proposer can't approve** ([Q11.49](conversation.md)): no independent check; no accountability; injected text in a log could drive diagnosis, patch and approval end to end; and it breaks separation of duties. So `record_decision` refuses the proposer's identities and any `system:` identity.

**Per-service rights** ([Q11.51](conversation.md)): the directory maps (person, service) to roles. `record_decision` checks `action:approve` on the action's target service, and the dispatcher re-checks at execution time.

**Lost permissions** ([Q11.59](conversation.md)): an approval doesn't survive. The dispatcher re-evaluates against the current directory, and an approver who no longer holds rights no longer counts. Already-executed changes keep their record.

**Model tool calls** ([Q11.65](conversation.md)): no write tools are offered to the model (a write tool name is refused as `write_forbidden`), tool proposals are data, and the gateway never runs them. The only write path is the dispatcher, a separate process holding the only production write credential.

## 11.8 Immutable action schema

*Answers Q11.54–Q11.57, Q11.70.*

**Owner:** the workflow proposes actions (`system:workflow`; Workflow Orchestrator, platform owner). No source names an owner for the schema itself.

`ImmutableAction` ([action.py](files/action.py)) is created once, from a tested patch, and never changes.

| Field | Meaning |
| --- | --- |
| `action_id` | Stable identity, a UUID; also the execution key |
| `incident_id`, `diagnosis_id` | What it belongs to |
| `kind` | `apply_patch`, `release_rollback` or `rollback` |
| `patch_digest` | None only for a release rollback without files |
| `base_revision` | The revision the change was generated and tested against |
| `expected_after_revision` | What the target should run afterwards |
| `target` | `service`, `environment` and `deployment` |
| `policy_version` | Edit policy and approval policy in force at creation |
| `test_bundle_digest` | The passing harness result for this exact patch |
| `recovery_predicates_digest` | What "recovered" means, fixed before execution |
| `high_impact` | Whether two approvers are needed |
| `parent_action_id` | For a rollback, the action it reverses |
| `created_at` | Creation time |

`digest` is SHA-256 over canonical JSON of all fields.

- **Exactly one target** ([Q11.55](conversation.md)): service, environment and deployment name (`checkout-prod-eu1`), all in the digest. Several environments mean several actions, each tested and approved on its own.
- **Revision** ([Q11.56](conversation.md)): `base_revision` is in the digest the approval signs. Approving against an old version can never apply to a new one.
- **Policy** ([Q11.57](conversation.md)): every action and decision records the policy versions in force. If the approval policy changed, earlier approvals are not counted and a fresh approval is needed. Old versions are kept for audit. A policy change can only make execution stricter, never looser, for decisions already given.
- **Identity** ([Q11.70](conversation.md)): two actions on the same service and revision have different IDs and different digests. A rollback is a separate action, linked by `parent_action_id`.

## 11.9 Dispatcher contract

*Answers Q11.65, Q11.67–Q11.69, Q11.73.*

**Owner:** the Trusted Dispatcher (security lead). It holds the only production write access and only the public approval key. The release process performs the deployment through its normal, audited pipeline.

**Input:** `Dispatcher.execute(action_id)` takes only the ID. It's triggered by the workflow after a signed approval ([Q11.65](conversation.md)). The request isn't trusted: it could come from a compromised worker, a bug, or arguments shaped by injected text. So the dispatcher loads the action and decisions from trusted storage, recomputes the action digest, and verifies signatures against that digest ([Q11.68](conversation.md)).

**The `ReleaseProcess` interface** ([Q11.67](conversation.md)). There is no shell, no free-form command and no general API passthrough.

| Operation | Returns | Kind |
| --- | --- | --- |
| `current_state(deployment)` | Revision, and any in-progress key | Read |
| `apply(deployment, execution_key, base_revision, patch_digest, to_revision)` | Applied or rejected | Write |
| `lookup(deployment, execution_key)` | None, applied, in progress or failed | Read |

**Revalidated immediately before the write** ([Q11.69](conversation.md), `_prechecks`):

1. Signatures valid, and decisions bound to this action digest.
2. No rejection; revocations applied.
3. Approvals unexpired (within 15 minutes).
4. Approvers still authorised for the service.
5. Enough distinct approvers (two if high-impact).
6. The policy version is unchanged.
7. The target runs exactly `base_revision`.
8. No other deployment is in progress on the target.

Any failure means `REFUSED`, nothing sent, with the reason recorded.

**Wrong version** ([Q11.73](conversation.md)) has two checks: the dispatcher reads `current_state` and refuses unless the revision equals `base_revision`; and the release process receives `base_revision` with `apply` and rejects a mismatch, a compare-and-set covering a deployment that lands between the check and the write. Either way, the result is a new patch, tested and approved again.

**Outcomes:** `applied`, `not_applied`, `refused` (pre-checks failed; nothing sent), `unknown`.

## 11.10 Idempotency ledger

*Answers Q11.70–Q11.72, Q11.77, Q11.78.*

**Owner:** the Trusted Dispatcher (security lead) owns the platform's ledger. Confirming the release process's support is the platform owner's unknown ([System Design §10](../../reference/system-design.md)).

**Key:** the execution key is the action ID ([Q11.70](conversation.md)). See conflict C1.

**Three layers** ([Q11.71](conversation.md)):

| Where | Mechanism | A repeat gets |
| --- | --- | --- |
| Dispatcher's ledger | Keyed by the action ID | `applied`: "already applied", with no second write. `unknown` or intent-only: reconciliation, never a resend |
| Release process | Ledger keyed by execution key | Its recorded result |
| Database | At most one `applied` attempt per execution key (`execution_attempts`) | Refused |

**Ledger states** ([dispatcher.py](files/dispatcher.py)): `intent_recorded` (written before the write), `applied`, `not_applied`, `unknown`. An `applied` entry can never change state.

**Target's ledger** ([Q11.72](conversation.md)): key, received time, status, resulting revision. Keys are kept for at least 30 days.

| On `apply` | Result |
| --- | --- |
| Unknown key | Run it, and record it |
| Key in progress | Return "in progress" |
| Key finished | Return the recorded result without running again |

**Exactly once** ([Q11.77](conversation.md)): from the outside, "not received" and "received and done" look the same when the answer is lost. Exactly-once effect needs the target to deduplicate by key, let us look up a key's status, and ideally compare-and-set on the base revision. With those, the platform achieves effectively once. Without them, the honest guarantee is "at most once, plus a person when unsure".

**Retries** ([Q11.78](conversation.md)) are prohibited while the outcome is unknown:

- the ledger shows `intent_recorded` or `unknown` for the key: reconcile only
- reconciliation is still unclear: hand over, with no further production action on that deployment until a person records the actual state
- the target lacks key lookup or deduplication: never retry a write
- the approval has expired, or the target revision changed: a new action is needed anyway

A retry is allowed only when reconciliation confirms `not_applied`, the approval is still valid, and the target is still at the base revision. In the first version even that goes back to a person (ADR-29).

## 11.11 Reconciliation strategy

*Answers Q11.74–Q11.78.*

**Owner:** the Trusted Dispatcher (security lead). A person resolves anything that stays `unknown`.

A timeout only means we didn't hear back: the request may never have arrived, may have been applied, or may still be running. So it's recorded as `unknown`, and reconciliation decides ([Q11.75](conversation.md)).

**Two independent sources must agree** ([Q11.76](conversation.md)): the release process's own record for the execution key, and the observed state of the target (its current revision, or the configuration value in the reference case).

`Dispatcher.reconcile` ([Q11.74](conversation.md)) asks `lookup`, reads the current revision, then:

| Release record | Target revision | Result |
| --- | --- | --- |
| Applied | Equals the expected revision after | `applied` |
| Not found or failed | Equals the base | `not_applied` |
| In progress | Any | Wait, and ask again (up to 10 checks) |
| Disagreement, or still unclear | Any | `unknown`; hand over; a person checks |

Tested: a lost response reconciles to applied with one `apply` call; a lost request reconciles to not applied; the release saying applied while the revision is still the base stays unknown, with no resend. Retry rules are in 11.10 ([Q11.77](conversation.md), [Q11.78](conversation.md)).

## 11.12 Verification predicates

*Answers Q11.79–Q11.88.*

**Owner:** the Recovery Verifier (SRE lead), with read-only monitoring credentials. The hold period is the SRE lead's open decision, before M3.

"Applied" means the release process changed what runs. It doesn't show that the diagnosis was right, the configuration took effect on every instance, users' requests succeed, or nothing else broke ([Q11.79](conversation.md)).

**Independence** ([Q11.80](conversation.md)): the verifier reads metrics and runs probes directly, and never takes the model's or the dispatcher's word. It evaluates the plan fixed before execution, whose digest is in the immutable action. The model's statement isn't an input at all.

**Reference plan** ([Q11.81](conversation.md), `reference_plan`). All conditions are provisional.

| Predicate | Condition |
| --- | --- |
| Error rate on the failing path | ≤ 1.1 × pre-incident baseline |
| Error rate for the service | ≤ 1.1 × baseline |
| Latency p95 | ≤ 1.2 × baseline |
| Synthetic probe of the failing request | ≥ 99% success |
| Availability (success ratio) | ≥ 99.5% |
| New error groups since the change | 0 |

Each must hold at every 60-second interval for 10 minutes, with at least 50 requests per interval.

**Gap:** no source defines how the pre-incident baseline is computed or over what period. `verify` takes it as an input.

**Checks** ([Q11.82](conversation.md), [Q11.83](conversation.md)):

- **Real-user signals:** success ratio and error rate, from monitoring.
- **Synthetic probes:** the originally failing request and a short list of key user paths, from outside the service, every interval.
- **Reproduction request as a probe:** same path and method, safe synthetic payload, marked as synthetic. A probe that can't be made safe for production isn't used; the real-user error rate on that path is used alone.
- **Service health endpoint:** necessary but not sufficient.

**Evaluation rules:**

- Rates over many requests, at every interval, never single responses. One bad interval fails verification ([Q11.84](conversation.md)).
- All required predicates must hold together. Fixing errors while making the service much slower is `failed`; the trade-off is a person's call ([Q11.85](conversation.md)).
- The window is per service in the plan, for example 30 minutes where failures appear only under peak load or after caches expire ([Q11.86](conversation.md)).
- After the window, a post-change watch of 60 minutes (provisional) flags new error groups or alert breaches and links them to the action, for a person to judge. It doesn't trigger automatic rollback. The change is marked on dashboards ([Q11.87](conversation.md)).

**Verdicts** ([Q11.88](conversation.md)):

| Verdict | Condition |
| --- | --- |
| `failed` | Any required predicate violated in any interval with enough data. Failure takes precedence |
| `inconclusive` | No violation, but a required predicate lacks data, or the window wasn't fully observed |
| `verified` | Every required predicate held in every interval, for the full window, with enough data |

Inconclusive is not verified: the incident hands over, with what was and wasn't measured.

## 11.13 Rollback strategy

*Answers Q11.89–Q11.92.*

**Owner:** the Trusted Dispatcher applies a rollback (security lead), under its own approval decision. The on-call engineer decides the next step after a failed fix, and the incident lead accepts recovery ([System Design §8](../../reference/system-design.md)).

**A rollback is a production change** ([Q11.90](conversation.md), ADR-43). It can fail, be wrong, or collide with another deployment, so it isn't implied by the fix's approval.

**Representation** ([Q11.91](conversation.md), `rollback_action_fields`):

| Field | Value |
| --- | --- |
| `kind` | `rollback` |
| `parent_action_id` | The original action |
| `base_revision` | The revision the original produced |
| `expected_after_revision` | The original's base revision |

It goes through the same approval, dispatch, ledger, reconciliation and verification. The timeline links the two as a pair. A rollback of a rollback is simply another action.

**Conditional approval** ([Q11.90](conversation.md)): the approver is shown the rollback alongside the fix, and signs it as a conditional approval, valid only if verification fails within the window, and only while the target runs the fix's revision. The dispatcher re-checks it like any action. If the conditions don't hold, a fresh approval is needed.

**Gap:** no source defines how a conditional approval is recorded or how long it lasts. `Decision` has no condition field, and `record_decision` gives every approval the same 15-minute `valid_until` (conflict C3). No source says what a rollback carries for `test_bundle_digest` and `recovery_predicates_digest`, or whether it is tested first.

**After a failed verification** ([Q11.89](conversation.md)):

1. The incident goes to `not_recovered`.
2. The rollback action is dispatched, if its conditional approval holds and the target still runs the revision the fix produced. It is reconciled if its outcome is unknown.
3. The incident goes to `handed_over`; the approver and on-call are notified.

**Escalate to the incident lead at once if:**

- the rollback can't run (approval invalid, or the target changed)
- the rollback's outcome is unknown after reconciliation
- the service is worse than before the change
- verification is inconclusive and impact continues

The platform proposes no further fix by itself.

**Limit:** rollback across a schema change is prohibited for the platform, always; it needs a person ([System Design §8](../../reference/system-design.md)).

**Resolution** ([Q11.92](conversation.md), `resolution_allowed`) needs all of:

1. The verdict is `verified`. One unsatisfied predicate means failed or inconclusive, not resolved.
2. No action is still open: nothing executing, reconciling or awaiting rollback.
3. The incident lead accepts.

Then recovered → closed. "Appears healthy" is never enough.

## Assumptions

The answers in this layer have no labelled assumptions list, so these are the values and dependencies they mark as provisional or unconfirmed. Owners and conditions come from the cited source; `TBD` means no source gives one.

| Assumption | Status | Owner | Validated when |
| --- | --- | --- | --- |
| Sandbox budget of 3 runs per incident and 45 minutes in total is enough ([Q11.11](conversation.md)) | Provisional | TBD | TBD |
| `SandboxSpec` values (CPU, memory, processes, disk, timeouts) suit the test workloads ([sandbox.py](files/sandbox.py)) | Provisional | Security lead | TBD |
| Recovery predicate conditions in the reference plan ([Q11.81](conversation.md)) | Provisional | TBD | TBD |
| A 10-minute hold period is enough for the reference service ([Q11.86](conversation.md), [Q4.13](../layer-04-requirements/conversation.md)) | Provisional | SRE lead | Past recoveries show no relapse after 10 minutes |
| A 60-minute post-change watch is long enough ([Q11.87](conversation.md)) | Provisional | TBD | TBD |
| The real release process can apply, undo and look up one change by key, deduplicates by key, and keeps keys for at least 30 days, or a thin adapter can add this ([Q11.72](conversation.md)) | Provisional | Platform owner | Scenario revert done through it |

## Decisions and conflicts

**Decisions in this layer:** all are recorded in the sections above (ADR-10 in 11.3, ADR-29 in 11.10, ADR-43 in 11.13).

**Values compared across code, answers, System Design (SD) and Technical specification (TS):**

| Value | Code | Answers | SD | TS | Result |
| --- | --- | --- | --- | --- | --- |
| Approval validity | 15 min (`valid_for`) | 15 min, Q11.58 | 15 min, §8 | 15 min, §5.5 | Match |
| Request expiry | 60 min (`request_expires`) | 60 min, Q11.58 | 60 min, §7 | 60 min, §2 | Match |
| Request escalation | Not in `ApprovalPolicy`; 15 min in Layer 8 `workflow.py` | 15 min, Q11.58 | 15 min, §7 | 15 min, risks table | Match |
| Recent sign-in | 15 min, two-factor (`max_auth_age`) | Same, Q11.50 | Same, §8 | 15 min, §6 | Match |
| Approver count | 1, or 2 if high-impact | Same, Q11.60, Q11.69 | Same; open decision, §12 | Not stated | Match; who the second approver is differs (C6) |
| Hold period | 600 s (`window_s`) | 10 min, longer per service, Q11.86 | Same; open decision, §12 | 10 min, provisional, §5.5 | Match |
| Interval and minimum requests | 60 s, 50 | Same, Q11.81 | Not stated | Not stated | Match |
| Reconciliation checks | 10 (`max_checks`) | 10, Q11.74 | Not stated | Not stated | Match |
| Dispatcher call timeout | 30 s (`call_timeout_s`) | Not stated | Not stated | Not stated | Code only |
| Wait between reconciliation checks | 0 s (`wait_s` default) | Not stated | Not stated | Not stated | Code only |
| Sandbox limits and patch limits | As in 11.2, 11.4 | Same | Not stated | Not stated | Match, except enforcement (C14) |

**Conflicts.** None of these is resolved by the sources unless the last column says so. They need a team decision.

| # | Topic | Layer 11 answers and code | Other sources | Used in this artifact |
| --- | --- | --- | --- | --- |
| C1 | Execution key | The action ID (Q11.70; `dispatcher.py`) | One key per approval (SD §11, §13; TS §7; Q5.10, Q7.11; Layer 7 schema); `exec:<approval_id>` and `undo:<approval_id>` (Layer 8 `workflow.py`) | Layer 11's value, shown as the layer's answer. Unresolved |
| C2 | Undo model | Rollback is its own immutable action with its own approval (Q11.90, ADR-43) | Undo runs under the same action and approval, with `is_undo` (Q7.11, Q8.35, Layer 8 `workflow.py`, Layer 7 schema). TS §5.5 says "pre-approved undo" | Layer 11's answer. Unresolved |
| C3 | Conditional approval lifetime | Valid if verification fails within the window (Q11.90). In code, every approval has `valid_until` of 15 minutes. A fix may execute up to 15 minutes after approval and then hold for 10 or more, so a rollback approval signed alongside it can expire first | SD §13: approval lasts 15 minutes | Gap in 11.13 |
| C4 | Who triggers the undo | Verifier writes nothing (Q11.66); the workflow triggers the dispatcher (Q11.65) | TS §5.5: the Verifier "dispatches" the undo; Q6.20: it "asks the dispatcher". Layer 8 `workflow.py`: the workflow dispatches | Workflow triggers the dispatcher. Unresolved in TS |
| C5 | Inconclusive verification | Hands over (Q11.88); no rollback stated | Layer 8 `workflow.py` treats anything but `recovered` as `not_recovered`, and undoes. TS §2 has no verifying → handed_over transition, though Layer 8 `states.py` does | Layer 11's answer. Unresolved |
| C6 | Second approver | Two different people holding approve rights (Q11.60; `evaluate`) | Incident lead approves as well (Q1.11); "a second approver" (SD §8) | Two distinct approvers, matching SD |
| C7 | Who may reject | Responders can reject (Q11.52; `MATRIX`) | Responders can't (TS §3; Q7.39) | Layer 11's answer. Unresolved |
| C8 | Sandbox configuration rights | `PLATFORM_ADMIN` holds `sandbox:configure` | Security lead owns the sandbox, and changes need security review (Q11.33) | Matrix shown as coded. Unresolved |
| C9 | Roles and permissions | `MATRIX` has admin and system roles; no monitoring role and no confirm-severity permission | TS §3 has a Monitoring role and "Confirm severity"; no admin or system roles | Matrix shown as coded. Unresolved |
| C10 | What an approval names | The action digest, which covers the patch digest (Q11.54). `Patch.digest` is over canonical JSON (Q11.8) | The patch fingerprint (SD §8; TS invariant 1); `diff_sha256` (TS §4); `patch_sha256` (Layer 7 schema) | Layer 11's answer. Unresolved |
| C11 | Names and enums | Decision `approve`, `reject`, `revoke`; six test outcomes; verdicts `verified`, `failed`, `inconclusive`; action kinds `apply_patch`, `release_rollback`, `rollback`; ledger `intent_recorded`. Q11.1 itself uses `config_revert`, `commit_revert`, `release_rollback` for patches | Layer 7 schema: decisions `approved`, `rejected` only, with no action digest column; four test outcomes; `recovered`, `not_recovered`, `inconclusive`; three action kinds with no `rollback`; attempt outcome `started` (also Q8) | Layer 11's names. Schema needs aligning |
| C12 | Release interface | Three operations, "all keyed", no undo (Q11.67). `current_state` takes no key. The `ReleaseProcess` docstring says "Two operations, both keyed; one read"; `dispatcher.py`'s module docstring says it accepts `(action_id, approval_ids)` while `execute` and Q11.68 take only the ID | "apply, undo, look up by key" (SD §10, §12; TS §8) | Interface from Q11.67; `execute(action_id)` |
| C13 | `classify` and "never passed" | Q11.17, Q11.25: a limit hit is failed or crashed, never passed; Q11.39: the after run must pass. `classify` never reads a non-zero exit code: a bundle with `exit_code=137`, or with `reproduction_after=None`, otherwise clean, returns `passed` (checked by running `classify` on a scratch copy). The tests don't cover this | None | Answer's rule in 11.5. Code needs a fix |
| C14 | Limits not applied | Q11.27, Q11.28 say the workspace is capped at 2 GB and the launcher enforces 900 s. `workspace_mb` and `wall_clock_s` exist in `SandboxSpec`, but `launch_args` doesn't use them. The reaper, volume deletion and log cap aren't in `files/` | None | Answers' values in 11.4. Enforcement not shown in code |
| C15 | Editable paths | Q11.4: `config/**`, and commit reverts "as in the reverted commit". `config/**/*.yaml` doesn't match `config/app.yaml` (checked), and nothing compares a commit revert with the reverted commit. Code allows `src/**/*.py` and `app/**/*.py` | None | Answer's rule in 11.1 |
| C16 | Block after unknown outcome | No further action on that deployment (Q11.78). The ledger blocks per key, so another action on the same deployment isn't stopped | None | Answer's rule in 11.10 |
| C17 | Two sources must agree | Q11.76 requires both. `reconcile` accepts the release record alone when `expected_after_revision` is None | None | Answer's rule in 11.11 |
| C18 | Sign-in for rejection | "A safety stop should be cheap" (Q11.60); Q11.50 requires recent sign-in for approval. `record_decision` requires it for rejections too | TS §6 lists it under Approval | Not resolved |
| C19 | Policy recorded on a decision | Q11.57: edit and approval policy on every action and decision. `Decision.policy_version` holds only the approval policy | None | Answer's rule in 11.8 |
| C20 | Proposer component and credential | Q11.66 names a "Patch Proposer" holding draft pull-request rights | SD §4 has no such component: the Diagnosis Engine drafts the patch and holds nothing. The worker's listed credentials don't include the review system | SD's component and owner, Q11.66's capability |
| C21 | Revocation endpoint | `POST /v1/actions/{id}/revocations` (Q11.63) | Not in TS §8 | Layer 11's answer |

**Not checked:** `test_remediation.py` wasn't run, because its dependencies aren't installed here. "Tested" in this artifact repeats the answers' claims.

## Acceptance checks

- [ ] Every required section above is present and complete.
- [x] Every section lists the questions it answers, and every question in this layer is answered by at least one section.
- [ ] Every assumption is marked provisional and has a named owner and a condition under which it will be validated.
- [ ] Every disagreement between Claude and ChatGPT that affected a decision is recorded with the team's resolution.
- [ ] The artifact is consistent with every artifact it is built from; any conflict is recorded as an explicit decision.
- [x] The artifact was produced with the team's coding agent and is saved in the team repository.

Reviewed by: TBD
Date: TBD
