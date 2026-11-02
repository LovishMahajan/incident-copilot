# Artifact 11: Safe Remediation Specification

- **Layer:** 11, Patch Generation, Sandboxing, Approval and Execution
- **Built from:** Artifacts 5, 8 and 9
- **Status:** Proposed: gaps resolved and reference code aligned 2026-11-02 per [decisions.md](../../decisions.md); pending team sign-off
- **Sources:** [decisions.md](../../decisions.md), [conversation.md](conversation.md), [files/patch.py](files/patch.py), [files/sandbox.py](files/sandbox.py), [files/action.py](files/action.py), [files/dispatcher.py](files/dispatcher.py), [files/verification.py](files/verification.py), [files/test_remediation.py](files/test_remediation.py), [System Design](../../reference/system-design.md), [Technical specification](../../reference/technical-specification.md), [Layer 8 workflow.py](../layer-08-workflows-state-machines/files/workflow.py), [Layer 7 schema-tables.sql](../layer-07-data-apis-events/files/schema-tables.sql)

## Required sections

| Required section | Questions this section answers | Done |
| --- | --- | --- |
| 11.1 Patch schema | Q11.1, Q11.4–Q11.7, Q11.10–Q11.12 | Yes |
| 11.2 Diff and hashing strategy | Q11.2, Q11.3, Q11.8, Q11.9 | Yes |
| 11.3 Sandbox threat model | Q11.13, Q11.14, Q11.22, Q11.23, Q11.31–Q11.34 | Yes |
| 11.4 Resource isolation | Q11.15–Q11.21, Q11.24–Q11.30 | Yes |
| 11.5 Trusted test harness | Q11.35–Q11.48 | Yes |
| 11.6 Approval API | Q11.50, Q11.53, Q11.54, Q11.58, Q11.60–Q11.64 | Yes |
| 11.7 Authorization matrix | Q11.49, Q11.51, Q11.52, Q11.59, Q11.65, Q11.66 | Yes |
| 11.8 Immutable action schema | Q11.54–Q11.57, Q11.70 | Yes |
| 11.9 Dispatcher contract | Q11.65, Q11.67–Q11.69, Q11.73 | Yes |
| 11.10 Idempotency ledger | Q11.70–Q11.72, Q11.77, Q11.78 | Partial |
| 11.11 Reconciliation strategy | Q11.74–Q11.78 | Yes |
| 11.12 Verification predicates | Q11.79–Q11.88 | Yes |
| 11.13 Rollback strategy | Q11.89–Q11.92 | Yes |

## 11.1 Patch schema

*Answers Q11.1, Q11.4–Q11.7, Q11.10–Q11.12.*

**Owner:** the Diagnosis Engine drafts the patch (SRE lead), through its Patch Proposer skill, which runs after the review accepts the diagnosis (ADR-51). The edit policy is owned by the security lead. Code, not the model, computes hunks and hashes.

A proposed change is a `Patch` record ([Q11.1](conversation.md); [patch.py](files/patch.py)):

| Level | Fields |
| --- | --- |
| Patch | `incident_id`, `diagnosis_id`, `candidate_no` (1–3), `action_kind`, `repository`, `base_revision`, `target_service`, `target_environment`, `files`, `target_release` (release rollback only), `reverted_commit` (commit revert only), `summary` (10–1000 characters) |
| File | `path`, `op` (`modify` only in the first version), `old_sha256`, `new_sha256`, `hunks` (at least one) |
| Hunk | `old_start`, `old_lines`, `new_start`, `new_lines`, `lines`, `rationale` (10–600 characters), `evidence_ids` (at least one) |

The model proposes the new content; `build_patch_from_contents` computes the hunks and hashes, so the model can't misstate what changes.

**Editable boundaries** come from a versioned `EditPolicy` ([Q11.4](conversation.md)):

| Action kind | May edit | Globs |
| --- | --- | --- |
| Configuration revert | YAML, TOML and JSON anywhere under `config/` | `config/*.yaml`, `config/*.yml`, `config/*.toml`, `config/*.json` |
| Commit revert | Application source and config, and only files the reverted commit changed | `src/*.py`, `app/*.py`, `config/*`, then the commit check below |
| Release rollback | No files: selects a previous release | None |

- **Glob rule:** the matcher is Python `fnmatch`, where `*` crosses `/` and `**` means nothing special. So `config/*.yaml` matches `config/app.yaml` and `config/checkout/payments.yaml`, while `config/**/*.yaml` misses `config/app.yaml`. Editable globs are anchored at the repository root.
- **Commit check:** `check_boundaries` compares each path with the files `reverted_commit` changed, read by code from the repository at the base revision. A path outside that list, or a list that can't be read, is refused.
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
- action kind, target release, reverted commit
- each file, sorted by path, with its path, before and after content hashes, and every hunk

The same edit against a different revision gives a different digest. A reworded summary gives the same digest. The stored name is `patch_sha256` everywhere (ADR-64).

**Digest binding at every step** ([Q11.9](conversation.md)):

| Step | Binding |
| --- | --- |
| Test | The result bundle records the `patch_sha256` it ran; `classify` treats a mismatched bundle as crashed |
| Immutable action | Records `patch_sha256` and the test bundle digest |
| Approval | Signs the action digest, which covers the patch fingerprint, target, base revision and predicates (ADR-49) |
| Dispatcher | Re-checks all of it |

A new version from the model is a new patch with a new digest, needing a new test and a new approval. Patches are append-only.

## 11.3 Sandbox threat model

*Answers Q11.13, Q11.14, Q11.22, Q11.23, Q11.31–Q11.34.*

**Owner:** the Test Runner and `SandboxSpec` are owned by the security lead. Never the model, never the patch ([Q11.33](conversation.md)).

| Threat | Why it matters | Control |
| --- | --- | --- |
| Malicious patch run on the production server ([Q11.13](conversation.md)) | It would run with the application's database credentials and customer data, network routes, secrets and cloud metadata, before any test or person could intervene | Generated code runs only in the isolated environment |
| Escape to the host ([Q11.14](conversation.md)) | Ordinary containers share the host kernel | Container under gVisor (`runsc`; ADR-10), on a separate host from production and the platform's own processes (ADR-51) |
| Credential theft ([Q11.22](conversation.md)) | Anything in the sandbox is readable by the untrusted code | Fake, test-only values and local stand-ins. The launcher passes only `AISRE_SANDBOX=1`, and `validate_args` refuses any other variable |
| Docker socket ([Q11.23](conversation.md)) | Equivalent to root on the host | `validate_args` allows one mount, the run's workspace volume, so `docker.sock`, `containerd`, `/proc`, `/sys`, `/dev`, `/etc` and the Docker data directory can't be mounted. The test host runs only sandboxes |
| Patch changes the sandbox configuration ([Q11.33](conversation.md)) | It could add mounts, devices, capabilities, network or variables | `SandboxSpec` is versioned platform code; arguments come only from `launch_args`, and `validate_args` is an allow-list of what it emits, so an option nobody listed is refused |
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
| Workspace | 2 GB (`workspace_mb`) | Size-limited volume, which the launcher creates for each run | "No space"; run fails ([Q11.27](conversation.md)) |
| Image filesystem | Read-only | `--read-only` | Nothing can be written |
| Returned logs | 50 MB, stored as an artifact | Harness | Capped ([Q11.27](conversation.md)) |
| Each test | 120 s | Harness | Timeout ([Q11.28](conversation.md)) |
| Whole run | 900 s wall clock (`wall_clock_s`); stop, then kill after 10 s | Launcher, which records the result as timed out | Timeout ([Q11.28](conversation.md)) |
| Workflow | Activity timeout and heartbeat | Artifact 8 | Timeout ([Q11.28](conversation.md)) |
| Sandboxes at once | 3, provisional | Test Runner | Queued (ADR-56) |

The launcher, not `docker run`, applies the workspace size and the wall clock, because neither is a container flag.

Each sandbox gets its own cgroup, so one run's runaway process can't starve another run or the host's own services ([Q11.17](conversation.md), [Q11.19](conversation.md)).

**Namespaces** ([Q11.15](conversation.md), [Q11.16](conversation.md), [Q11.18](conversation.md)):

| Namespace | Isolates | Setting |
| --- | --- | --- |
| Mount | Filesystem view | Read-only image, workspace volume, bounded tmpfs; no host paths |
| Network | Interfaces and routes | `--network=none`: loopback only |
| PID | Process tree | Private, Docker's default. There is no `--pid=private` flag: Docker refuses it |
| IPC | Shared memory and queues | Private |
| UTS | Host name | Private, Docker's default. Docker refuses `--uts=private` too |
| User | User IDs | Runs as user 65534, never root |

**Privileges** ([Q11.20](conversation.md)): `--user=65534:65534`, with user 0 or root refused; all Linux capabilities dropped; `no-new-privileges`; never `--privileged`, added capabilities or host devices; disabling default system-call filtering is refused.

**Network** ([Q11.21](conversation.md), ADR-60): no network. Dependencies come pre-installed in the pinned image, and a patch can't add them: patches are limited to configuration and reverts, so nothing new needs installing. An approved package mirror would need a new ADR, a filtering proxy and Security lead sign-off, and none is planned. Attempts to reach out are recorded as an injection or abuse signal.

**Clean runs** ([Q11.29](conversation.md)): a fresh container from the pinned image, a new workspace volume checked out at `base_revision`, and the patch applied by the harness. No reused volumes, caches or databases. Each run has its own ID.

**Needs the team:** how the checkout reaches the sandbox at `base_revision`, Owner: Security lead, settled when: a seeding method is chosen and run on the test host. A tmpfs volume exists only while mounted, and `--rm` with `--network=none` leaves no way in, so nothing can pre-seed it. The options are the Test Runner piping a tar on stdin, or a disk volume with a quota.

**Cleanup** ([Q11.30](conversation.md)):

- `--rm` deletes the container on exit; the launcher deletes the workspace volume.
- A reaper runs every 5 minutes, removing any container or volume labelled `aisre.sandbox=1` older than 60 minutes (FR-23 target: none left after 60 minutes).
- Cleanup is part of the Test Runner activity's `finally`; cancellation also triggers it.
- Leftover counts are a platform metric, alerted above zero. `reap()` returns the count; the Test Runner schedules it every 5 minutes and sets `aisre_sandboxes_leftover`.

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

The required list is fixed in `classify`, not supplied by the bundle: `reproduction`, `unit`, `integration`, `smoke`, `error_signature`.

A test the model wrote can be added as extra evidence, but never replaces these and never counts toward "passed" on its own.

**Reproduction test** ([Q11.38](conversation.md), [Q11.39](conversation.md)): built by the harness from the evidence, not by the model.

- **Configuration:** production values at the failing revision, with secrets replaced by fakes.
- **Request:** the failing path, method and payload shape from the traces, with personal data removed.
- **Assertion:** the original symptom. In the reference incident: start with `ORDER_TAX_RATE_BPS: 8.25`, call `POST /v1/orders`, expect a 500 with the `ValueError`.
- **Before:** it must fail on the unpatched base revision, or the outcome is `not_reproduced` and testing stops.
- **After:** it must pass, in the same environment.

**Regressions** ([Q11.37](conversation.md), [Q11.41](conversation.md)): the full suite runs at the base revision and with the patch. A test that passed before and fails after is a regression. Any error signature present only after the patch is a `new_error_signature`. Either gives `failed`.

**Harness independence** ([Q11.42](conversation.md), [Q11.43](conversation.md)):

- Tests come from the base revision, never from the patch.
- The harness lives in the pinned image, on the read-only filesystem, outside the workspace.
- The patch is applied only to `/workspace`.
- `conftest.py`, `pytest.ini` and `pyproject.toml` are protected.
- The harness reads results from its own test runner, not from files in the workspace.

**`ResultBundle`** ([Q11.44](conversation.md), [Q11.45](conversation.md)): run ID, `patch_sha256` (computed by the harness from the applied content), base revision, image digest, sandbox spec version, harness version, reproduction before and after, each suite (required, ran, passed, failed, errors), new error signatures, exit code, duration, timed-out flag, and the fingerprint of the full log (secret-scanned, capped at 50 MB). Nothing else leaves: no workspace files, no binaries. The bundle's own digest goes into the action as `test_bundle_digest`.

The bundle reaches the launcher as the last line of the harness's stdout, read by `read_bundle`. That is the only channel left, because `--rm` removes the container and a tmpfs volume empties with it. Suite errors and new error signatures are free text, so `read_bundle` masks secrets in them before they leave (ST-24). The harness must keep that stdout away from the tests, and must report at least one check in `error_signature`, or every run is `inconclusive`. Unreadable output is no bundle, which classifies as `crashed`. `read_bundle` takes `exit_code` from what Docker reported, not from the bundle.

`exit_code` is the harness's own exit status, not the test runner's. It is 0 when the harness finished and wrote a bundle, even if tests failed. Non-zero means the harness itself died or was killed.

**Outcomes** (`classify`):

| Outcome | Meaning | What happens next |
| --- | --- | --- |
| `passed` | Reproduced before, gone after, no regressions, all required suites ran | Release B: may go to approval. Release A: hands over with the tested recommendation (ADR-44) |
| `failed` | Failure remains, or a regression appeared | Next candidate, or hand over |
| `not_reproduced` | Original failure never appeared | Testing stops |
| `inconclusive` | Something required has no result ([Q11.48](conversation.md)) | Never reaches approval; one retry if the cause is infrastructure, otherwise hand over |
| `timeout` | Wall clock or per-test limit hit ([Q11.47](conversation.md)) | Not rerun automatically; hand over with the hung test's name |
| `crashed` | No valid bundle, no exit code, a non-zero exit, or a bundle for a different patch ([Q11.46](conversation.md)) | One rerun as a new run ID, within the sandbox budget; a second crash hands over with the logs |

**How the verdict is reached.** `classify` checks in this order, and the first match wins. `passed` is the last row, so a run reaches it only if every earlier row didn't match.

| Step | Condition | Verdict | Reaches `passed`? |
| --- | --- | --- | --- |
| 1 | No bundle, or `exit_code` is None | `crashed` | No |
| 2 | `patch_sha256` differs from the expected patch | `crashed` | No |
| 3 | `timed_out` is true (checked before the exit code, because a timeout kill exits 137) | `timeout` | No |
| 4 | `exit_code` is not 0 | `crashed` | No |
| 5 | `reproduction_before` is false | `not_reproduced` | No |
| 6 | `reproduction_before` is None | `inconclusive` | No |
| 7 | `reproduction_after` is None | `inconclusive` | No |
| 8 | A required suite is absent from the bundle | `inconclusive` | No |
| 9 | A required suite has `ran = false` | `inconclusive` | No |
| 10 | A required suite ran zero tests | `inconclusive` | No |
| 11 | `reproduction_after` is true, a new error signature, or any suite failed or errored | `failed` | No |
| 12 | None of the above | `passed` | Yes |

Only `passed` can move an action to `awaiting_approval`. Every other final verdict moves it to `test_failed`.

## 11.6 Approval API

*Answers Q11.50, Q11.53, Q11.54, Q11.58, Q11.60–Q11.64.*

**Owner:** the Approval Service (security lead). It holds the signing key, in the API process. It decides nothing itself: a named person does.

**Endpoints:**

| Endpoint | Purpose | Permission | Source |
| --- | --- | --- | --- |
| `POST /v1/actions/{id}/approvals` | Approve one exact action | `action:approve` | [Technical specification §8](../../reference/technical-specification.md) |
| `POST /v1/actions/{id}/rejections` | Reject, with a reason | `action:reject` | [Technical specification §8](../../reference/technical-specification.md) |
| `POST /v1/actions/{id}/revocations` | Withdraw an approval, with the approval ID, the action digest and a reason | `approval:revoke_own` | [Q11.63](conversation.md), ADR-49 |

**The API accepts only** ([Q11.61](conversation.md)): the action ID (in the path), the action digest the approver saw, the decision (set by the endpoint), and a reason. A revocation adds the approval ID. An approval may add `rollback_action_digest` to sign the conditional approval (11.13). Everything else is read from trusted storage. If the call carried the patch or target, the screen could show patch A while the request approves patch B.

All three calls send `If-Match` with the action's version, and an `Idempotency-Key` (ADR-64). The version changes only when the action's status changes, so one person's approval on a two-approver action doesn't invalidate the other approver's screen. Stored decisions are `approved`, `rejected` and `revoked` (ADR-64).

**Identity** ([Q11.50](conversation.md), ADR-49): single sign-on with two-factor. The API receives a short-lived signed token with the subject, authentication time and two-factor claim.

| Call | Authentication | Recent sign-in (15 minutes) with two-factor |
| --- | --- | --- |
| Approve | Required | Required |
| Revoke | Required | Required |
| Reject | Required | Not required: a safety stop should be cheap |

**Refusals.** Every error is an RFC 9457 `Problem`, with `code` as listed and `type` set to `https://errors.aisre.internal/<code>`. The checks run in order: authentication, rights, headers, body, then binding and state.

| Code | Status | Approve | Reject | Revoke | Cause | Security alert |
| --- | --- | --- | --- | --- | --- | --- |
| `unauthenticated` | 401 | ✓ | ✓ | ✓ | Token missing, invalid or expired | Yes |
| `reauth_required` | 401 | ✓ | — | ✓ | Sign-in too old, or no two-factor. Sends `WWW-Authenticate: Bearer error="insufficient_user_authentication"` | Yes |
| `forbidden` | 403 | ✓ | ✓ | ✓ | Can read the service, but lacks this permission on it; or a `system:` identity rejecting or revoking | Yes |
| `self_approval` | 403 | ✓ | — | — | The subject is a proposer or a `system:` identity, or authored the change being reverted when only one approver is needed | Yes |
| `not_own_approval` | 403 | — | — | ✓ | The approval ID is someone else's, or doesn't exist | Yes |
| `not_found` | 404 | ✓ | ✓ | ✓ | No such action, or no read access to its service. The same answer for both, so existence isn't revealed (ADR-58) | Yes |
| `digest_mismatch` | 409 | ✓ | ✓ | ✓ | The presented action digest, or `rollback_action_digest`, differs from the stored one | Yes |
| `duplicate` | 409 | ✓ | — | ✓ | The same person approving twice, or revoking twice, under a new `Idempotency-Key` | No |
| `action_not_open` | 409 | ✓ | ✓ | ✓ | The action is not open for this decision (see the table below) | No |
| `execution_started` | 409 | — | ✓ | ✓ | The dispatcher has recorded intent. Undoing the change is a rollback action | No |
| `idempotency_key_reused` | 409 | ✓ | ✓ | ✓ | Same key with a different body | No |
| `request_in_progress` | 409 | ✓ | ✓ | ✓ | The first request with this key is still running; retry | No |
| `stale_version` | 412 | ✓ | ✓ | ✓ | `If-Match` doesn't match the current version. The client reloads and sends again | No |
| `version_required` | 428 | ✓ | ✓ | ✓ | `If-Match` is missing | No |
| `reason_required` | 422 | — | ✓ | ✓ | A rejection or revocation without a reason | No |
| `invalid_request` | 422 | ✓ | ✓ | ✓ | Missing `Idempotency-Key` or digest, or any extra field such as patch content | No |

A repeat with the same key and body returns the stored response, not a refusal.

**Open for a decision:**

| Action status | Approve | Reject | Revoke |
| --- | --- | --- | --- |
| `awaiting_approval` | Yes | Yes | Yes |
| `approved`, before the dispatcher records intent | No | Yes | Yes |
| `proposed` (a rollback holding a conditional approval, 11.13) | Through the parent's approval only | Yes | Yes |
| Any other status | No | No | No |

**Failed attempts** (ADR-49): a refusal writes no approval row. It writes an `aisre.approval.refused.v1` timeline event where the action exists, and raises a security alert where the table says so. "Nothing recorded" in Technical specification §6 means no approval decision.

**What the approver reviews** ([Q11.53](conversation.md)), in this order. A notification is only a link; decisions are made on this screen, in the CLI or API.

1. The action: kind, exact target, base revision and expected revision after, and the action digest.
2. The diff, with each hunk's rationale and evidence links.
3. The diagnosis: leading cause and confidence, evidence for and against, and alternatives with why they were ruled out.
4. Test results: reproduction before and after, each suite, new error signatures, bundle digest.
5. Risk and the rollback action, with its own digest, to sign as a conditional approval.
6. Recovery predicates, the recorded baseline and the hold period.
7. Flagged items: gaps, stale evidence, text flagged as possible injection.
8. Expiry of the request and of any approval.

**Binding** ([Q11.54](conversation.md)): the decision records `action_digest` and is signed with Ed25519. Any edit to the decision fails verification. A changed patch is a new action with a new digest that no one has approved.

**Expiry** ([Q11.58](conversation.md)):

| Clock | Value | Enforced by |
| --- | --- | --- |
| Request | Escalates at 15 minutes, expires at 60 | Workflow timer; `evaluate` returns `EXPIRED` |
| Granted approval | Valid 15 minutes (`valid_until`) | `evaluate` stops counting it |

Expired requests are closed, the action becomes `expired`, and nothing executes. A conditional approval has its own `valid_until` (ADR-48, 11.13).

**Second approver** (ADR-49): the API stores each decision and sends the workflow its own signal, carrying the decision ID, through the outbox. The workflow waits until `approvers_needed` valid decisions exist. It never receives a signature or a patch (ADR-51).

**Conflicting decisions** ([Q11.60](conversation.md), ADR-49): any valid rejection wins, regardless of order or number of approvals. `rejected` is final and the incident hands over. Both decisions are kept. Changing course needs a new action.

**Rejection is enforced in three places** ([Q11.62](conversation.md)): the workflow (`rejected` has no outgoing transitions), the database (an immutable `approval_decisions` row), and the dispatcher (`evaluate` returns `REJECTED`, and it refuses even if a stale workflow asked). A rejection that lands while the action is `approved` is stored, the dispatcher refuses, the action becomes `withdrawn`, and the incident hands over. `approved` → `rejected` isn't a transition (ADR-46), but the rejection still wins.

**Revocation** ([Q11.63](conversation.md), ADR-49):

- Recent sign-in with two-factor is required.
- Only the person who approved can revoke. Anyone with reject rights can instead reject.
- It is a new signed decision; the original stays in the record.
- It is valid until the dispatcher records intent. After that, undoing the change is a rollback action.
- Revoking an approval also revokes the conditional approval signed with it, in one transaction, unless the fix has already executed. After that, the rollback's conditional approval is revoked on the rollback's own action ID.

**Audit record** ([Q11.64](conversation.md)): approval ID, action ID, action digest; decision; subject and roles held at the time; authentication time and two-factor claim; the edit and approval policy versions, decision time, valid until, reason; `conditional` and `conditional_on_approval_id`, for a conditional approval; signing key ID and signature. Every decision is signed. It is linked to the action, the execution ledger entry and the verification result. The timeline is hash-chained and copied to a separate store.

## 11.7 Authorization matrix

*Answers Q11.49, Q11.51, Q11.52, Q11.59, Q11.65, Q11.66.*

**Owner:** the Approval Service performs rights checks (security lead). The Security lead role owns policies, keys and sandbox configuration. The engineering manager owns the approval rules, and the security lead the mechanism (ADR-49).

**Separation of concerns.** No component both proposes and authorises a change:

| Concern | Component | Owner | Holds | Can't do |
| --- | --- | --- | --- | --- |
| Propose | Diagnosis Engine (Patch Proposer skill, ADR-51) | SRE lead | Create a draft pull request, through the worker's draft-pull-request credential | Merge, deploy |
| Test | Test Runner | Security lead | Create sandboxes on the test host | Reach production |
| Authorise | Approval Service, on a named person's decision | Security lead | Sign decisions | Deploy |
| Execute | Trusted Dispatcher | Security lead | `apply`, `lookup` and `current_state` on named deployments | Approve, sign, run arbitrary commands |
| Verify | Recovery Verifier | SRE lead | Read monitoring | Write anything |
| Investigate | Evidence Collector | SRE lead | Read, per tool, per service | Write |
| Reason | Agent and model | SRE lead (Diagnosis Engine, Model Gateway) | Nothing | Anything external |

Holding the dispatcher's credential doesn't let anyone approve. Holding the signing key doesn't let anyone deploy ([Q11.66](conversation.md)).

**Role matrix** (ADR-50, with the permission names `MATRIX` uses in [action.py](files/action.py)):

| ADR-50 permission | Layer 11 permission | Viewer | Responder | Approver | Incident lead | Monitoring | Platform admin |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Read incidents, timeline, artifacts | `incident:read` | ✓ | ✓ | ✓ | ✓ | — | ✓ |
| Create an incident | `incident:create` | — | ✓ | ✓ | ✓ | ✓ | — |
| Add a note | `incident:note` | — | ✓ | ✓ | ✓ | — | — |
| Cancel an investigation | `incident:cancel` | — | ✓ | ✓ | ✓ | — | — |
| Reject an action | `action:reject` | — | ✓ | ✓ | ✓ | — | — |
| Approve or revoke an action | `action:approve`, `approval:revoke_own` | — | — | ✓ | ✓ | — | — |
| Request re-investigation | `incident:reinvestigate` | — | ✓ | ✓ | ✓ | — | — |
| Confirm severity, close | `incident:confirm_severity`, `incident:close` | — | — | — | ✓ | — | — |
| Read the full audit export | `audit:export` | — | — | — | ✓ | — | — |
| Clear an unknown outcome (11.11) | `action:resolve_outcome` | — | — | — | ✓ | — | — |
| Set a manual write fence | `fence:set` | — | — | — | ✓ | — | ✓ |
| Lift a fence, run a restore | `platform:operate` | — | — | — | — | — | ✓ |

Setting a fence is a safety stop, so a lead can do it directly. A person with a lead's sign-off lifts it (ADR-50).

**Outside the table** (ADR-50):

| Holder | Permissions |
| --- | --- |
| Security lead role | `policy:edit`, `keys:manage`, `sandbox:configure` |
| `system:workflow` (service identity) | `incident:read`, `incident:create` |
| `system:dispatcher` (service identity) | `incident:read`, `action:execute` |

Service identities use least privilege (ADR-51). Security staff read the full audit export through the incident lead permission on the audit scope.

**Who approves** (ADR-49): the service owner, on-call engineer or incident lead, holding `action:approve` for that service.

**High-impact actions** need two different people with approve rights. The incident lead may be one, but isn't required to be. An action is high-impact if any of these holds:

- confirmed severity `sev1`, or severity not yet confirmed
- kind `release_rollback`
- the service is marked `critical` in the catalogue
- the same change is proposed for more than one deployment (provisional). Each action names one deployment (11.8), so this marks every action in that set

`is_high_impact` derives this from the action and from `ImpactFacts`, which trusted storage supplies: confirmed severity, the catalogue's `critical`, the deployment count and a rollback's parent kind. The `high_impact` field can add to the result and never lowers it. `record_decision`, `evaluate` and the database's `action_is_high_impact` all apply it.

**Key separations** ([Q11.52](conversation.md)):

- administrators can't approve changes
- approvers can't change policy or keys
- the platform's own identities can't approve: the workflow proposes, and the dispatcher executes only
- responders can reject, but not approve, so anyone on the incident can stop a bad change

**Why the proposer can't approve** ([Q11.49](conversation.md)): no independent check; no accountability; injected text in a log could drive diagnosis, patch and approval end to end; and it breaks separation of duties. So `record_decision` refuses the proposer's identities and any `system:` identity.

**Reverting your own change** (ADR-49): nobody may be the sole approver of a revert of their own change. When one approver is needed, the change's author is refused as `self_approval`. When two are needed, the author may be one of them, because the other must be a different person.

**Per-service rights** ([Q11.51](conversation.md)): the directory maps (person, service) to roles. `record_decision` checks `action:approve` on the action's target service, and the dispatcher re-checks at execution time.

**Lost permissions** ([Q11.59](conversation.md)): an approval doesn't survive. The dispatcher re-evaluates against the current directory, and an approver who no longer holds rights no longer counts. Already-executed changes keep their record.

**Model tool calls** ([Q11.65](conversation.md)): no write tools are offered to the model (a write tool name is refused as `write_forbidden`), tool proposals are data, and the gateway never runs them. The only write path is the dispatcher, a separate process holding the only production write credential.

## 11.8 Immutable action schema

*Answers Q11.54–Q11.57, Q11.70.*

**Owner:** the workflow proposes actions (`system:workflow`; Workflow Orchestrator, platform owner). The platform owner owns the schema (ADR-64).

`ImmutableAction` ([action.py](files/action.py)) is created once, from a tested patch, and never changes. A new patch or target is a new action with a new `action_id` (ADR-47). A rollback action is created alongside its parent, so the approver can sign it (ADR-48).

| Field | Meaning |
| --- | --- |
| `action_id` | Stable identity, a UUID; also the execution key (ADR-47) |
| `incident_id`, `diagnosis_id` | What it belongs to |
| `kind` | `config_revert`, `commit_revert`, `release_rollback` or `rollback` (ADR-64) |
| `patch_sha256` | The patch fingerprint. None for a release rollback without files, and for a `rollback`, which restores a known revision |
| `base_revision` | The revision the change was generated and tested against |
| `expected_after_revision` | What the target should run afterwards. Required for every kind: the head of the draft pull request; for a release rollback, the target release's revision; for a `rollback`, the parent's `base_revision` |
| `target` | `service`, `environment` and `deployment` |
| `policy_version` | Edit policy and approval policy in force at creation, as `edit-policy@N\|approval-policy@N` |
| `test_bundle_digest` | The passing harness result for this exact patch. A `rollback` copies the parent's |
| `recovery_predicates_digest` | What "recovered" means, including the recorded baseline, fixed before execution. A `rollback` copies the parent's |
| `hold_seconds` | The hold period, from the service catalogue (ADR-64, ADR-66). A `rollback` copies the parent's |
| `high_impact` | Whether two approvers are needed, as recorded at creation. A `rollback` copies the parent's. The dispatcher re-derives the rule (11.7), so a false value lowers nothing |
| `parent_action_id` | For a rollback, the action it reverses |
| `created_at` | Creation time |

`digest` is SHA-256 over canonical JSON of all fields, stored as `action_digest` on actions, approval decisions and attempts (ADR-64).

- **Exactly one target** ([Q11.55](conversation.md)): service, environment and deployment name (`checkout-prod-eu1`), all in the digest. Several environments mean several actions, each tested and approved on its own.
- **Revision** ([Q11.56](conversation.md)): `base_revision` is in the digest the approval signs. Approving against an old version can never apply to a new one.
- **Policy** ([Q11.57](conversation.md)): every action and decision records both policy versions in force, in the same `edit-policy@N|approval-policy@N` form. The dispatcher compares both with the current policy: if either changed, earlier approvals are not counted and a fresh approval is needed. Old versions are kept for audit. A policy change can only make execution stricter, never looser, for decisions already given.
- **Identity** ([Q11.70](conversation.md)): two actions on the same service and revision have different IDs and different digests. A rollback is a separate action, linked by `parent_action_id`.

## 11.9 Dispatcher contract

*Answers Q11.65, Q11.67–Q11.69, Q11.73.*

**Owner:** the Trusted Dispatcher (security lead). It runs as its own process (ADR-51), and holds the only production write access and only the public approval key. Its database role reads actions, patches and approvals, and writes `execution_attempts` and its own timeline events. In Release A the production write credential isn't provisioned at all (ADR-44). The release process performs the deployment through its normal, audited pipeline.

**Input:** `Dispatcher.execute(action_id)` takes only the ID. Approvals are stored by the API, and the workflow sends only the action ID on the dispatcher's task queue ([Q11.65](conversation.md), ADR-51). The request isn't trusted: it could come from a compromised worker, a bug, or arguments shaped by injected text. So the dispatcher loads the action and decisions from trusted storage, recomputes the action digest, and verifies signatures against that digest ([Q11.68](conversation.md)).

**The `ReleaseProcess` interface** ([Q11.67](conversation.md)). There is no shell, no free-form command and no general API passthrough. There are three keyed operations and one unkeyed read. Undo isn't a separate method: it is a new `rollback` action, with its own execution key, sent through `apply`.

| Operation | Call | Returns | Keyed | Kind |
| --- | --- | --- | --- | --- |
| Apply a change | `apply(deployment, execution_key, base_revision, patch_sha256, to_revision)` | Applied or rejected | Yes | Write |
| Undo a change | The same `apply`, with the `rollback` action's key and revisions, and no `patch_sha256` | Applied or rejected | Yes | Write |
| Look up a key | `lookup(deployment, execution_key)` | None, applied, in progress or failed | Yes | Read |
| Read the target | `current_state(deployment)` | Revision, and any in-progress key | No | Read |

**Revalidated immediately before the write** ([Q11.69](conversation.md), `_prechecks`):

1. Signatures valid, and decisions bound to this action digest.
2. No rejection; revocations applied.
3. Approvals unexpired (within 15 minutes; a conditional approval, within its own `valid_until`, and only after the parent's `failed` verdict is recorded).
4. Approvers still authorised for the service.
5. Enough distinct approvers (two if high-impact).
6. Both policy versions are unchanged.
7. The target runs exactly `base_revision`. For a rollback, that is the revision the parent produced (ADR-48).
8. No other deployment is in progress on the target.
9. No unknown outcome is uncleared on this deployment, for any key (ADR-47).
10. The write fence allows it: no global or deployment reason, and the dispatcher's token holds the current epoch (12.5). Checked first, and again with no await before the intent write.
11. The action's status is `approved`, so a withdrawn or expired action is refused here, not only by the transition table.
12. The stored patch bytes hash to the approved `patch_sha256` (ST-13). Unreadable content is a refusal. The artifact store holds `Patch.canonical_bytes()`, the bytes the fingerprint covers.
13. A rollback's parent has a `failed` verdict, under a plain approval as well as a conditional one (ADR-48).

Any failure means `REFUSED`, nothing sent, with the reason recorded. Check 9 and the intent record are one transaction.

**Wrong version** ([Q11.73](conversation.md)) has two checks: the dispatcher reads `current_state` and refuses unless the revision equals `base_revision`; and the release process receives `base_revision` with `apply` and rejects a mismatch, a compare-and-set covering a deployment that lands between the check and the write. Either way, the result is a new patch, tested and approved again.

**Outcomes:** `applied`, `not_applied`, `refused` (pre-checks failed; nothing sent), `unknown`.

## 11.10 Idempotency ledger

*Answers Q11.70–Q11.72, Q11.77, Q11.78.*

**Owner:** the Trusted Dispatcher (security lead) owns the platform's ledger.

**Needs the team:** whether the real release process can apply, undo and look up one change by key, Owner: Platform owner, settled when: a scenario revert is done through it ([System Design §10](../../reference/system-design.md)).

**Identity** (ADR-47):

| Concept | Rule |
| --- | --- |
| Action | Immutable. A new patch or target is a new action |
| Execution key | Equals the `action_id` ([Q11.70](conversation.md)), and is passed unchanged to the release process on every attempt |
| Attempt | One `execution_attempts` row per try: same key, increasing `attempt_no` |
| Applied | At most one row with outcome `applied` per key, enforced by a partial unique index |
| Approval | Authorises the action. Two approvers still authorise one key. Each attempt records the `approval_id` that completed the count, for audit, and the `action_digest` |
| Unknown outcome | Blocks every further change on that deployment, not just that key, until a person clears it (11.11) |

**Three layers** ([Q11.71](conversation.md)):

| Where | Mechanism | A repeat gets |
| --- | --- | --- |
| Dispatcher's ledger | Keyed by the action ID | `applied`: "already applied", with no second write. `unknown`, or `started` past the in-flight window: reconciliation, never a resend. `started` inside the window: `unknown`, with the row and the block unchanged |
| Release process | Ledger keyed by execution key | Its recorded result |
| Database | At most one `applied` attempt per execution key (`execution_attempts`) | Refused |

**Ledger states** ([dispatcher.py](files/dispatcher.py)): `started` (intent, written before the write), `applied`, `not_applied`, `unknown`. An `applied` entry can never change state.

**In-flight window** (ST-37): a `started` row may belong to a live call, whose write can still land, so a repeat or a reconciliation can't read "no such key" as `not_applied`. For twice the call timeout plus 5 s of clock drift, `reconcile` raises `ReleaseInProgress`, and the row and the deployment block stay. Past that, the row is taken as a dead dispatcher's. A first call that outlives the window writes nothing and never raises. If reconciliation already recorded `not_applied` and the call then saw the change land, it returns `unknown`, for a person to check.

**Target's ledger** ([Q11.72](conversation.md)): key, received time, status, resulting revision. Keys are kept for at least 30 days.

| On `apply` | Result |
| --- | --- |
| Unknown key | Run it, and record it |
| Key in progress | Return "in progress" |
| Key finished | Return the recorded result without running again |

**Exactly once** ([Q11.77](conversation.md)): from the outside, "not received" and "received and done" look the same when the answer is lost. Exactly-once effect needs the target to deduplicate by key, let us look up a key's status, and ideally compare-and-set on the base revision. With those, the platform achieves effectively once. Without them, the honest guarantee is "at most once, plus a person when unsure".

**Retries** ([Q11.78](conversation.md)) are prohibited while the outcome is unknown:

- the ledger shows `started` or `unknown` for the key: reconcile only
- reconciliation is still unclear: hand over, with no further production action on that deployment until a person records the actual state (ADR-47)
- the target lacks key lookup or deduplication: never retry a write
- the approval has expired, or the target revision changed: a new action is needed anyway

A retry is allowed only while the action is still `applying`, after a lookup shows the change wasn't applied, with the approval still valid and the target still at the base revision (ADR-47). A retry reuses the key and takes the next `attempt_no`, or the release process couldn't recognise it as a repeat.

Once reconciliation sets `not_applied`, which is final (ADR-46, ADR-47), the action ends there and the incident hands over. Any further try is a new action needing a new approval, and that is a person's decision (ADR-29).

## 11.11 Reconciliation strategy

*Answers Q11.74–Q11.78.*

**Owner:** the Trusted Dispatcher (security lead). The incident lead resolves anything that stays `unknown`.

A timeout only means we didn't hear back: the request may never have arrived, may have been applied, or may still be running. So it's recorded as `unknown`, and reconciliation decides ([Q11.75](conversation.md)).

**Two independent sources must agree** ([Q11.76](conversation.md)): the release process's own record for the execution key, and the observed state of the target (its current revision, or the configuration value in the reference case).

`Dispatcher.reconcile` ([Q11.74](conversation.md)) asks `lookup`, reads the current revision, then:

| Release record | Target revision | Result |
| --- | --- | --- |
| Applied | Equals the expected revision after | `applied` |
| Not found or failed | Equals the base | `not_applied` |
| In progress | Any | `reconcile` raises the retryable `ReleaseInProgress`. It looks once per call, and Temporal's `READ_RETRY` asks again with backoff, for up to 10 minutes (Artifact 8, 8.5, 8.9) |
| Disagreement, or still unclear | Any | `unknown`; hand over; a person checks |

The release record alone never gives `applied`. `expected_after_revision` is required on every action (11.8), so the second source always exists. A fix that can't be compared stays `unknown`.

Tested: a lost response reconciles to applied with one `apply` call; a lost request reconciles to not applied; the release saying applied while the revision is still the base stays unknown, with no resend. Retry rules are in 11.10 ([Q11.77](conversation.md), [Q11.78](conversation.md)).

**Clearing an unknown outcome** (ADR-47). While a key stays `unknown`, the dispatcher refuses every action on that deployment. To clear it, the incident lead checks the target and calls `POST /v1/actions/{id}/outcome-resolutions` with the outcome (`applied` or `not_applied`), the observed revision and a reason. The call needs `action:resolve_outcome`, a recent sign-in with two-factor, `If-Match` and an `Idempotency-Key`, and uses the refusals in 11.6. The action then leaves `outcome_unknown`. The record stays with the attempts: a reconciliation row of `execution_attempts` carries `resolved_by`, `observed_revision` and `resolution_reason`, with the `aisre.execution.outcome_resolved.v1` event.

## 11.12 Verification predicates

*Answers Q11.79–Q11.88.*

**Owner:** the Recovery Verifier (SRE lead), with read-only monitoring credentials. The incident lead accepts recovery, in all cases (ADR-66).

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

Each must hold at every 60-second interval for the hold period, with at least 50 requests per interval.

**Baseline** (ADR-66): the median of each symptom metric over the 60 minutes before incident start, excluding the 5 minutes before start. Incident start is the incident's `started_at`. The baseline is computed once and recorded with the action, before execution: it is part of the plan that `recovery_predicates_digest` covers, and the approver sees it.

- **Zero baseline:** a limit of 1.1 × 0 would fail on one error, so error-rate limits have a floor of 0.1% (provisional).
- **Missing baseline:** if there's no data for a metric, a predicate relative to it can't be judged and counts as lacking data, which gives `inconclusive`. It never counts as held.

**Hold period** (ADR-66): 10 minutes by default, longer per service in the catalogue. It is stored as `hold_seconds` on the action (ADR-64), and the plan's window equals it.

**Checks** ([Q11.82](conversation.md), [Q11.83](conversation.md)):

- **Real-user signals:** success ratio and error rate, from monitoring.
- **Synthetic probes:** the originally failing request and a short list of key user paths, from outside the service, every interval.
- **Reproduction request as a probe:** same path and method, safe synthetic payload, marked as synthetic. A probe that can't be made safe for production isn't used; the real-user error rate on that path is used alone.
- **Service health endpoint:** necessary but not sufficient.

**Evaluation rules:**

- Rates over many requests, at every interval, never single responses. One bad interval fails verification ([Q11.84](conversation.md)).
- All required predicates must hold together. Fixing errors while making the service much slower is `failed`; the trade-off is a person's call ([Q11.85](conversation.md)).
- A longer window suits services where failures appear only under peak load or after caches expire, for example 30 minutes ([Q11.86](conversation.md)).
- After the window, a post-change watch of 60 minutes (provisional) flags new error groups or alert breaches and links them to the action, for a person to judge. It doesn't trigger automatic rollback. The change is marked on dashboards ([Q11.87](conversation.md)).

**Verdicts** ([Q11.88](conversation.md)):

| Verdict | Condition | Incident goes to | Then |
| --- | --- | --- | --- |
| `failed` | Any required predicate violated in any interval with enough data. Failure takes precedence | `not_recovered` | Rollback path (ADR-48, 11.13) |
| `inconclusive` | No violation, but a required predicate lacks data, or the window wasn't fully observed | `handed_over` | No rollback. A person decides (ADR-66) |
| `verified` | Every required predicate held in every interval, for the full window, with enough data | `recovered` | Waits for the incident lead to accept (11.13) |

Inconclusive is not verified: the incident hands over, with what was and wasn't measured.

## 11.13 Rollback strategy

*Answers Q11.89–Q11.92.*

**Owner:** the Trusted Dispatcher applies a rollback (security lead), under its own approval decision. The workflow triggers it, on the Verifier's verdict. The Verifier writes nothing to production and has no dispatcher path (ADR-48). The on-call engineer decides the next step after a failed fix, and the incident lead accepts recovery ([System Design §8](../../reference/system-design.md)).

**A rollback is a production change** ([Q11.90](conversation.md), ADR-43). It can fail, be wrong, or collide with another deployment, so it isn't implied by the fix's approval.

**Representation** ([Q11.91](conversation.md), `rollback_action_fields`, ADR-48). A rollback is a new immutable action with its own execution key, created alongside the fix:

| Field | Value |
| --- | --- |
| `kind` | `rollback` |
| `parent_action_id` | The original action |
| `base_revision` | The revision the original produced (its `expected_after_revision`) |
| `expected_after_revision` | The original's base revision |
| `patch_sha256` | None: it restores a known revision |
| `test_bundle_digest`, `recovery_predicates_digest`, `hold_seconds`, `high_impact` | Copied from the parent |

It isn't re-tested in the sandbox, because it restores the known prior revision. It goes through the same approval, dispatch, ledger, reconciliation and verification. The timeline links the two as a pair. A rollback of a rollback is simply another action.

**Conditional approval** ([Q11.90](conversation.md), ADR-48): the approver is shown the rollback alongside the fix, and signs both in one decision. For high-impact fixes both approvers sign it.

How it is recorded:

| Step | What happens |
| --- | --- |
| Request | `POST /v1/actions/{fix}/approvals` carries `action_digest` and `rollback_action_digest`. Leaving the second out gives no conditional approval |
| Storage | One transaction writes two signed `approval_decisions` rows: the fix's approval, and a row for the rollback action |
| Rollback row | `action_id` and `action_digest` are the rollback's, `decision` = `approved`, `conditional` = true, `conditional_on_approval_id` = the same person's approval of the fix. Both fields are inside the signature |
| Lifetime | `valid_until` = the fix approval's `valid_until` + `hold_seconds` + 15 minutes. It is fixed at signing, and counts only from a `not_recovered` verdict |
| Use | The dispatcher treats the row like any approval, and also requires the parent's recorded `failed` verdict. Before that verdict it doesn't count |

A fresh approval is needed if there is no conditional approval, it has lapsed, or a high-impact rollback has only one of its two signatures. The request then follows the normal timers: it escalates at 15 minutes and expires at 60 (ADR-49).

**Action states.** A rollback waits in `proposed`.

| Event | Rollback action |
| --- | --- |
| Parent verified, or never executed | `proposed` → `withdrawn` |
| `not_recovered` verdict | `proposed` → `awaiting_approval` (ADR-46), then `approved` at once if its conditional approvals count |
| Dispatch | `approved` → `applying` → `executed` |
| Rollback executed | Parent `executed` → `undone` |

**After a failed verification** ([Q11.89](conversation.md), ADR-48):

1. The incident goes to `not_recovered`; the approver and on-call are notified.
2. The workflow dispatches the rollback action, if its approval holds and the target still runs the revision the fix produced. If the deployment is at any other revision, the dispatcher refuses.
3. If the rollback's outcome is unknown, the incident goes to `reconciling`, and then to `handed_over` whatever the result, never to `verifying`.
4. Once applied, the Verifier reads the same plan over the hold period. The incident stays `not_recovered`, or `reconciling` if the rollback's outcome was unknown, and the verdict is recorded on the rollback action and the timeline. It triggers nothing further.
5. The incident goes to `handed_over`.

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

Then recovered → closed. `recovered` stays `recovered` until the incident lead closes it (ADR-46). "Appears healthy" is never enough.

## Assumptions

The answers in this layer have no labelled assumptions list, so these are the values and dependencies they mark as provisional or unconfirmed, plus the values this resolution added. Each stays provisional until its condition is met.

| Assumption | Status | Owner | Validated when |
| --- | --- | --- | --- |
| Sandbox budget of 3 runs per incident and 45 minutes in total is enough ([Q11.11](conversation.md)) | Provisional | Security lead | The reference scenario and the first pilot incidents finish testing within it |
| `SandboxSpec` values (CPU, memory, processes, disk, timeouts) suit the test workloads ([sandbox.py](files/sandbox.py)) | Provisional | Security lead | The reference application's full suites run inside them with no limit hit |
| Recovery predicate conditions in the reference plan ([Q11.81](conversation.md)) | Provisional | SRE lead | Replayed against past recoveries, they separate recovered from not recovered |
| A 10-minute hold period is enough for the reference service ([Q11.86](conversation.md), [Q4.13](../layer-04-requirements/conversation.md), ADR-66) | Provisional | SRE lead | Past recoveries show no relapse after 10 minutes |
| A 60-minute post-change watch is long enough ([Q11.87](conversation.md)) | Provisional | SRE lead | Pilot regressions linked to a change appear within it |
| The real release process can apply, undo and look up one change by key, deduplicates by key, and keeps keys for at least 30 days, or a thin adapter can add this ([Q11.72](conversation.md)) | Provisional | Platform owner | Scenario revert done through it |
| `expected_after_revision` can be known before execution, because the release process deploys the draft pull request's head (11.8) | Provisional | Platform owner | Scenario revert ends on the revision named in the action |
| Error-rate floor of 0.1% for a zero baseline (11.12) | Provisional | SRE lead | Pilot verifications on low-error services show no false `failed` |
| 15-minute margin in a conditional approval's lifetime (ADR-48) covers the time to deploy a fix | Provisional | Security lead | Pilot deployments finish within it, and conditional approvals are not lapsing unused |
| `READ_RETRY`'s backoff between reconciliation checks (5 s doubling to 1 minute, 10 attempts) fits the 10-minute limit (Artifact 8, 8.5; 11.11) | Provisional | Security lead | A scenario revert finishes within that limit through the real release process |
| 30 s dispatcher call timeout (`call_timeout_s`) | Provisional | Security lead | The real release process accepts `apply` within it |
| A memory-backed volume is acceptable for the 2 GB workspace (`workspace_mb`) | Provisional | Security lead | 3 sandboxes at once run on the test host without memory pressure |
| A change to more than one deployment counts as high-impact (ADR-49) | Provisional | Engineering manager | The pilot approval review confirms it |
| An approver is reachable within 15 minutes, at night too | Provisional | Engineering manager | Rotas confirmed (ADR-49) |

## Decisions and conflicts

**Decisions in this layer:** recorded in the sections above: ADR-10 in 11.3, ADR-29 in 11.10, ADR-43 in 11.13.

| Item | Resolution | Decided by |
| --- | --- | --- |
| Value: approval validity | 15 minutes in code, answers, System Design and Technical specification | ADR-49 |
| Value: request expiry | 60 minutes everywhere | ADR-49 |
| Value: request escalation | 15 minutes. Not in `ApprovalPolicy`; 15 minutes in Layer 8 `workflow.py` and the answers | ADR-49 |
| Value: recent sign-in | 15 minutes, two-factor, everywhere | ADR-49 |
| Value: approver count | 1, or 2 if high-impact | ADR-49 |
| Value: hold period | 600 s default, longer per service; open decision closed | ADR-66 |
| Value: interval and minimum requests | 60 s, 50 | ADR-66 |
| Value: reconciliation checks | One look per `reconcile` call. `READ_RETRY` allows 10 attempts, within the 10-minute limit (Artifact 8) | Q11.74, Layer decision (proposed) |
| Value: dispatcher call timeout | 30 s, code only; kept as provisional | Layer decision (proposed) |
| Value: wait between reconciliation checks | `READ_RETRY`'s backoff. `max_checks` and `wait_s` are removed, because a wait inside the activity would hide progress from Temporal | Layer decision (proposed) |
| Value: sandbox limits and patch limits | As in 11.2, 11.4; enforcement in C14 | Layer decision (proposed) |
| Approval expiry "15 vs 60 minutes" | Two different timers, not a conflict | ADR-49 |
| C1 Execution key | The action ID, on every attempt. `exec:<approval_id>` and `undo:<approval_id>` retired | ADR-47 |
| C2 Undo model | Rollback is its own immutable action. `is_undo` retired | ADR-48 |
| C3 Conditional approval lifetime | `valid_until` = parent's + hold period + 15 minutes. Two signed rows, one transaction (11.13) | ADR-48; recording is Layer decision (proposed) |
| C4 Who triggers the undo | The workflow, on the Verifier's verdict. The Verifier has no dispatcher path | ADR-48 |
| C5 Inconclusive verification | Hands over, no rollback. Workflow code corrected | ADR-66 |
| C6 Second approver | Two different people with approve rights. The incident lead may be one, but isn't required to be | ADR-49 |
| C7 Who may reject | Responder, Approver, Incident lead | ADR-49, ADR-50 |
| C8 Sandbox configuration rights | Security lead role, not Platform admin | ADR-50 |
| C9 Roles and permissions | One matrix, mapped to Layer 11 names in 11.7. Service identities sit outside it | ADR-50 |
| C10 What an approval names | The action digest. `patch_sha256` stays on the record for reading | ADR-49 |
| C11 Names and enums | Kinds `config_revert`, `commit_revert`, `release_rollback`, `rollback`; decisions `approved`, `rejected`, `revoked`; six test outcomes; Layer 11's verdicts | ADR-64 |
| C11 Attempt outcome | `started` from the schema replaces `intent_recorded`. The dispatcher never writes `failed`: a confirmed failure is `not_applied` | Layer decision (proposed) |
| C12 Release interface | Three keyed operations (apply, undo through `apply`, lookup) plus unkeyed `current_state`. `execute(action_id)` takes only the ID | Layer decision (proposed) |
| C13 `classify` and "never passed" | Non-zero exit is `crashed`; a missing reproduction result, or a required suite absent or empty, is `inconclusive`. Table in 11.5 | Layer decision (proposed) |
| C14 Limits not applied | The launcher applies workspace size and wall clock, records the timeout, and owns volume deletion and the reaper; the harness caps logs | Layer decision (proposed) |
| C15 Editable paths | `config/*.yaml` and siblings, anchored. Commit reverts limited to the reverted commit's files | Layer decision (proposed) |
| C16 Block after unknown outcome | Blocks the deployment, not just the key, until a person clears it | ADR-47 |
| C16 Who clears it | Incident lead, through `outcome-resolutions` with `action:resolve_outcome` (11.11) | ADR-49, ADR-50 |
| C17 Two sources must agree | `expected_after_revision` is required on every action, so the record alone never gives `applied` | Layer decision (proposed) |
| C18 Sign-in for rejection | Rejection needs authentication, not a recent sign-in | ADR-49 |
| C19 Policy recorded on a decision | Both versions on every decision, in the action's form. A change to either voids earlier approvals | Layer decision (proposed) |
| C20 Proposer component and credential | A skill inside the Diagnosis Engine. The worker holds a draft-pull-request credential only | ADR-51 |
| C21 Revocation endpoint | `POST /v1/actions/{id}/revocations`, added to the API list. Valid until the dispatcher records intent | ADR-49 |
| Status for each refusal | Table in 11.6. Status per code; a failed `If-Match` is 412 and a missing one 428, not 409 | ADR-64, Layer decision (proposed) for the table |
| Version and concurrent decisions | The action's version changes on status change only, so concurrent approvers don't collide | Layer decision (proposed) |
| Which refusals raise an alert | All write the event. Identity, rights and digest refusals alert; retries and validation don't | Layer decision (proposed) |
| Rejection while `approved` | Stored; the dispatcher refuses; the action becomes `withdrawn` | Layer decision (proposed) |
| Retry after `not_applied` | Retries happen only while the action is `applying`. `not_applied` is final, so a further try is a new action with a new approval | ADR-47 |
| Rollback's `patch_sha256` | None. It restores a revision | Layer decision (proposed) |
| Rollback's verification | Runs while the incident stays `not_recovered`, or `reconciling` if its outcome was unknown, and triggers nothing | ADR-48 |
| Baseline zero or missing | Floor of 0.1% on error-rate limits; missing baseline gives `inconclusive` | Layer decision (proposed) |
| Sandbox network option | `package_mirror` removed; `--network=none` required, and a missing network flag or `--net=` is refused | ADR-60 |
| How the result leaves the sandbox | The last line of the harness's stdout, because the volume can't be read after the run (11.5) | Layer decision (proposed) |
| Target answers neither `applied` nor `rejected` | `unknown`, not `not_applied`, because 11.10 lets the target say "in progress" | Layer decision (proposed) |
| Reader with no access to a service | `not_found`, not `forbidden`, as the 11.6 table and ADR-58 say | ADR-58 |

**Needs the team:** whether the amended behaviour works in code, Owner: Platform owner, settled when: the items in Reference code status below are built and tested on the real services.

**Reference code status.** [files/test_remediation.py](files/test_remediation.py) has 167 passing tests, and 19 safety lines broken one at a time were all caught. "Tested" in this artifact means that suite. It uses fakes for Docker and the release process, and these modules hold their ledger in memory.

**Docker and gVisor are not proven on a real run.** Checked on Docker 29, on 2026-11-02:

| Ran on real Docker | Did not run |
| --- | --- |
| Docker refuses `--pid=private` and `--uts=private`, so `launch_args` no longer passes them | `--runtime=runsc`: gVisor isn't installed, so nothing here has run under it, and no escape test has |
| Docker accepts the other launch flags | `docker volume create` for the size-limited workspace, and `docker volume rm` |
| The tmpfs size limit, user and read-only filesystem, through an equivalent anonymous `--mount` on a `--rm` container | The exit code after `docker stop`, so the timed-out path |
| One `docker run --rm` of a flag-check image finished normally | `reap()` and the volume deletion in `finally`, which are tested with fakes only, and a final listing of `aisre.sandbox=1` leftovers |

`docker stop --time` prints a deprecation warning on Docker 29 and still works. It is kept as 11.4 states it.

What remains:

| Item | What remains |
| --- | --- |
| Launcher | A real run of the table above, on the test host |
| Harness | It isn't in `files/`. It must meet the stdout and `error_signature` rules in 11.5 |
| Test Runner | Scheduling `reap()` every `REAP_INTERVAL_S`, and setting `aisre_sandboxes_leftover` |
| Dispatcher ledger | `Ledger.put` writes attempt, status and version together in memory. The real transaction, the `aisre_dispatcher` connection and the advisory lock remain. The deployment block is in `Ledger.begin` with the intent write, so the two can't be separated |
| Outcome resolution | `resolve_outcome` records it and clears the block. The HTTP layer, `If-Match`, `Idempotency-Key` and a refusal event remain |
| Refused approvals | Every refusal appends the event, with `security_alert` set per 11.6. Delivering the alert is outside these modules |

## Acceptance checks

- [ ] Every required section above is present and complete.
- [x] Every section lists the questions it answers, and every question in this layer is answered by at least one section.
- [x] Every assumption is marked provisional and has a named owner and a condition under which it will be validated.
- [ ] Every disagreement between Claude and ChatGPT that affected a decision is recorded with the team's resolution.
- [ ] The artifact is consistent with every artifact it is built from; any conflict is recorded as an explicit decision.
- [x] The artifact was produced with the team's coding agent and is saved in the team repository.

Reviewed by: TBD
Date: TBD
