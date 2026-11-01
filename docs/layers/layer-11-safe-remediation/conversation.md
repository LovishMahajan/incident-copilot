# Layer 11: Patch Generation, Sandboxing, Approval and Execution — Conversation

- **Central question:** How does a recommendation become a controlled, verified change?
- **Built from:** Artifacts 5, 8 and 9
- **Focus for this layer:** This is the critical safety layer. Treat patch generation, sandboxing, testing, approval, dispatch and verification as separate engineering problems with separate owners. No component may both propose and authorize a change.
- **Deliverable:** [Artifact 11: Safe Remediation Specification](artifact-11-safe-remediation-specification.md)
- **Questions:** 92

## Conversation log

| Questions | Agent | Conversation link | Decision reached | Open unknowns and owner | Artifact section |
| --- | --- | --- | --- | --- | --- |
| Q11.1–Q11.92 | TBD | [Shared conversation](https://claude.ai/share/c6a48a50-d511-4886-b4f5-0a3000f6a725) (needs sign-in; not read for these docs) | Recorded per question below | Listed under each question's Unknowns and Assumptions | [Artifact 11: Safe Remediation Specification](artifact-11-safe-remediation-specification.md) |

## Where Claude and ChatGPT disagreed

TBD: the source documents do not record any disagreements. The team fills this in.

## Questions and answers

### Q11.1 The AI identifies a bug and needs to communicate a proposed source-code modification. How should that modification be represented?

**Frame.** What form does a proposed change take?

**Answer.** As a **`Patch`** **record:**

- a structured diff against an exact `base_revision`
- target service and environment, and the action kind (configuration revert, commit revert, or release rollback)
- per file: path, content hash before and after, and hunks
- per hunk: its lines, a **rationale**, and the **evidence IDs** that justify it

The model proposes the new content; **code** computes the hunks and hashes (`build_patch_from_contents`), so the model can't misstate what changes.

### Q11.2 An AI-generated change modifies five lines of code, but engineers need to understand exactly what changed. How should the modification be displayed?

**Frame.** How do reviewers see the change?

**Answer.** As a **standard unified diff** (`Patch.unified_diff()`), the format every engineer reads:

- removed lines start with `-`, added lines with `+`, and 3 lines of context
- each hunk is preceded by `# why:` with its rationale and evidence IDs
- the header shows the patch digest and the base revision

The same diff is opened as a pull request in the review system, so reviewers can use their normal tools.

### Q11.3 The model generates a complete replacement file, making it difficult to review individual modifications. How could the platform represent changes as a structured diff?

**Frame.** How do we turn a whole-file rewrite into a reviewable diff?

**Answer.**

- **Never accept a replacement file as the patch.** If the model returns new content, code diffs it against the file at the base revision (Python `difflib`), giving minimal hunks with context.
- **Size limits:** at most 3 files and 40 changed lines per patch (configurable). A rewrite that changes more is refused as `too_large`, so a near-total rewrite can't hide a small malicious edit.
- **Hunks are typed** (start lines, counts, lines), so the review system, the sandbox and the digest all use the same structure.

### Q11.4 The model attempts to modify configuration files, application code and security policies simultaneously. How should editable file boundaries be defined?

**Frame.** Which files may a patch touch?

**Answer.** By a **versioned edit policy**, owned by the security lead (`EditPolicy`):

| **Action kind** | **May edit** |
| --- | --- |
| Configuration revert | `config/**` YAML, TOML and JSON only |
| Commit revert | Application source and config, as in the reverted commit |
| Release rollback | No files: selects a previous release |

**Always protected,** whatever the policy says:

- tests and test configuration
- CI files
- security policies, CODEOWNERS
- the sandbox launcher and harness
- deployment files and Dockerfiles
- lock files, secrets and keys

Paths with `..` or absolute paths are refused. A patch touching several categories at once is refused if any one file is outside its kind's list. All of this is tested.

### Q11.5 The agent generates a patch against the latest source code, but another engineer deploys a new version before execution. How should the original source revision be preserved?

**Frame.** How do we pin the revision a patch was made for?

**Answer.**

- **`base_revision`** (the exact commit) is part of the patch, its digest, the immutable action, and the approval.
- **Each file records** **`old_sha256`****,** the content at that revision. Applying the patch checks it first.
- **The dispatcher refuses** if the target no longer runs `base_revision` (tested: "target runs 99999999, action was approved for a1b2c3d4").

A newer deployment therefore means a new patch, a new test and a new approval.

### Q11.6 A patch modifies the test suite to make its own tests pass. How should the platform prevent modifications to trusted tests and security policies?

**Frame.** How do we stop a patch rewriting its own tests?

**Answer.** Three independent barriers:

1. **Boundary check:** test files, test configuration and policies are protected paths; such a patch is rejected before testing (tested).
2. **Trusted harness:** tests run from the **harness image**, built and pinned separately, not from the patched workspace. Even if a test file in the workspace changed, it isn't the one that runs (Q11.42).
3. **Code owners:** the review system requires the security lead's approval for protected paths, and the platform's own identity has no such approval.

### Q11.7 Engineers need to understand why each line was modified. How should the rationale behind a patch be recorded?

**Frame.** How is "why" recorded?

**Answer.**

- **Per hunk:** `rationale` (10–600 characters) and `evidence_ids` (at least one), shown in the diff and the approval screen.
- **Per patch:** a `summary`, and the diagnosis it implements.
- **Excluded from the digest,** so rewording an explanation doesn't change the patch's identity, but stored with it permanently.

### Q11.8 Two patches contain identical-looking changes but originate from different source revisions. How could a SHA-256 digest establish the identity of each patch?

**Frame.** How do we tell identical-looking patches apart?

**Answer.** **`Patch.digest`** = SHA-256 over a canonical JSON form (sorted keys, no whitespace) of everything that defines the change:

- repository and **base revision**
- target service and environment
- action kind, target release
- each file, sorted by path, with its path, before and after content hashes, and every hunk

The same edit against a different revision therefore gives a different digest; a reworded summary gives the same digest (both tested).

### Q11.9 A patch passes testing, but the model subsequently generates a modified version. How should the platform detect that the approved artifact has changed?

**Frame.** How do we notice the patch changed after testing?

**Answer.** By **digest binding** at every step:

- the test result bundle records the patch digest it ran; `classify` treats a mismatched bundle as **crashed** (tested)
- the immutable action records the patch digest and the test bundle digest
- the approval signs the action digest
- the dispatcher re-checks all of it

A new version from the model is **a new patch** with a new digest, which needs a new test and a new approval. Patches are append-only (Artifact 7).

### Q11.10 An incident has one diagnosis but several possible repair strategies. How should multiple patch candidates be associated with the same diagnosis?

**Frame.** How are several candidates tied to one diagnosis?

**Answer.** Each candidate is its **own patch and its own action**, carrying the same `diagnosis_id` and a `candidate_no` (1–3). They are tested independently. The approval screen shows them side by side, with the smallest reversal first. Approving one approves only that one; the others are withdrawn when it executes.

### Q11.11 The agent generates 15 different patch candidates, creating excessive testing costs. How should candidate generation be bounded?

**Frame.** How many candidates are allowed?

**Answer.**

- **At most 3 per diagnosis** (enforced by the schema, tested), drafted in order of preference: configuration revert, then commit revert, then release rollback.
- **Tested one at a time, stopping at the first that passes.**
- **Each draft counts** against the model-call budget (Artifact 10), and each test against a sandbox budget (provisional: 3 runs per incident, 45 minutes of sandbox time in total).
- **Identical digests are tested once.**

### Q11.12 Every proposed patch fails the trusted test suite. What should the platform do instead of attempting an unsafe production deployment?

**Frame.** What happens when nothing passes?

**Answer.** **Nothing is deployed.** The action becomes `test_failed`, and the incident goes to `handed_over` (Artifact 8). The hand-over includes:

- the diagnosis and evidence
- each candidate's diff and test results (which checks failed)
- the reproduction test itself, which engineers can reuse
- a suggestion of the safest manual option, labelled as a suggestion, not an action

There is no "best effort" deployment, and no relaxing of tests.

### Q11.13 An AI-generated patch contains malicious code. Why would executing it directly on the production application server create an unacceptable risk?

**Frame.** Why never run it in production?

**Answer.** On the production server, the code would run with the application's own access:

- its database credentials and customer data
- its network routes to other services
- its secrets and cloud metadata

It could steal data, change data, take the service down, plant persistence, or move on to other systems, before any test or person could intervene. It would also contaminate the very service being repaired. Artifact 5 rules it out: generated code runs only in the isolated environment.

### Q11.14 The engineering team needs to execute untrusted code without exposing the underlying host. Which isolation mechanism could provide this boundary?

**Frame.** What isolates untrusted code from the host?

**Answer.** A **container run under gVisor** (`runsc`; ADR-10). gVisor puts a user-space kernel between the code and the host kernel, so system calls from the code don't reach the host kernel directly. That removes the main weakness of ordinary containers (Q11.31). It runs on a **separate host** from production and from the platform's own processes. Lightweight virtual machines (Firecracker) are the next step if needed (Q11.32).

### Q11.15 A generated patch needs its own filesystem and process environment. How could containerization support this requirement?

**Frame.** How does a container give the patch its own environment?

**Answer.**

- **Own root filesystem:** from a pinned image (by digest, never a tag), mounted **read-only**.
- **Own writable areas:**
    - the patched checkout, on a size-limited workspace volume
    - a bounded in-memory `/tmp`, mounted no-exec
- **Own process tree, network, host name and IPC,** through namespaces (Q11.18).
- **Created fresh per run, and destroyed after** (Q11.29, Q11.30).

### Q11.16 An untrusted process attempts to inspect or modify processes belonging to other applications. Which operating-system isolation mechanisms could prevent this?

**Frame.** What stops it reaching other processes?

**Answer.**

- a **PID namespace** (`--pid=private`): it sees only its own processes
- **no capabilities** (`--cap-drop=ALL`): no `ptrace` of others, no raw devices
- **`no-new-privileges`****:** setuid binaries can't escalate
- gVisor's own kernel, which runs the container's processes inside its sandbox
- a **separate host** with no other workloads

### Q11.17 A generated process attempts to consume all available CPU and memory. How could the operating system enforce resource restrictions?

**Frame.** How are resources capped?

**Answer.** With **control groups (cgroups)**, set by the launcher:

- CPU quota (2 CPUs)
- memory limit (2 GB, with no extra swap)
- process limit (256)

Plus bounded disk (Q11.27) and wall-clock limits (Q11.28). When a limit is hit, the kernel throttles CPU, or kills the process on memory and process limits. The run is then classified as failed or timed out, never passed.

### Q11.18 An untrusted container needs isolation from the host network and filesystem. Which Linux namespace capabilities could support this?

**Frame.** Which namespaces apply?

**Answer.**

| **Namespace** | **Isolates** | **Our setting** |
| --- | --- | --- |
| Mount | Filesystem view | Read-only image, workspace volume, bounded tmpfs; no host paths |
| Network | Interfaces and routes | `--network=none`: loopback only |
| PID | Process tree | Private |
| IPC | Shared memory and queues | Private |
| UTS | Host name | Private |
| User | User IDs | Runs as user 65534, never root (Q11.20) |

### Q11.19 A generated process needs resource limits independent of other processes. How could control groups enforce these limits?

**Frame.** How do cgroups give each run its own limits?

**Answer.** Each sandbox gets **its own cgroup**, created by the runtime from the launch arguments: `--cpus=2.0`, `--memory=2g`, `--memory-swap=2g`, `--pids-limit=256`. Limits apply to that container's whole process tree, independently of other runs on the host. One run's runaway process can't starve another run, or the host's own services.

### Q11.20 A container runs with root privileges, giving generated code excessive permissions. How should execution privileges be restricted?

**Frame.** How are privileges reduced?

**Answer.**

- run as an unprivileged user (`--user=65534:65534`); the launcher **refuses** user 0 or root (tested)
- drop all Linux capabilities
- set `no-new-privileges`
- keep the image filesystem read-only
- never use `--privileged`, added capabilities or host devices (each refused by `validate_args`, tested)
- keep the runtime's default system-call filtering on; disabling it is refused

### Q11.21 An untrusted patch attempts to access the internet and download additional executable code. How should outbound network access be controlled?

**Frame.** How is outbound access blocked?

**Answer.**

- **Default: no network** (`--network=none`).
- **Dependencies come pre-installed in the pinned image.** A patch can't add dependencies; lock files are protected paths.
- **If a package mirror is ever needed,** it is the only reachable destination, through a filtering proxy that allows the mirror's host only and logs every request. There is no general internet access, ever.
- **Attempts are recorded** as an injection or abuse signal.

### Q11.22 The sandbox needs configuration information to execute tests. Why should production credentials never be included in its environment?

**Frame.** Why no real credentials in the sandbox?

**Answer.** Anything in the sandbox is readable by the untrusted code. A production credential there would hand production access to whatever the patch, or injected text, does. Tests use **fake, test-only values** and local stand-ins (a test database in the same sandbox). The launcher passes only `AISRE_SANDBOX=1`, and refuses any variable that looks like a secret (`TOKEN`, `SECRET`, `PASSWORD`, `DATABASE_URL`, and so on; tested).

### Q11.23 A generated process discovers the Docker host socket mounted inside its container. What level of host access could this expose?

**Frame.** What does the Docker socket give away?

**Answer.** **Full control of the host.** With the socket, the code can start a new privileged container that mounts the host's root filesystem, then read every secret, change any file and run anything as root. It's equivalent to root on the host. The launcher **refuses any mount** of `docker.sock`, `containerd`, `/proc`, `/sys`, `/dev`, `/etc` or the Docker data directory (tested). The test host runs only sandboxes, so even a breach has nothing else to reach.

### Q11.24 An agent generates a patch that consumes excessive CPU resources. How should CPU quotas be enforced?

**Frame.** How is CPU capped?

**Answer.** A cgroup CPU quota of **2 CPUs** (`--cpus=2.0`): the kernel throttles the container to that share, so it can't take more, however many threads it starts. A run that becomes too slow under the quota hits the wall-clock limit, and is classified **timeout**.

### Q11.25 A malicious test process allocates memory continuously. How should memory consumption be restricted?

**Frame.** How is memory capped?

**Answer.** A cgroup memory limit of **2 GB**, with swap equal to the limit (`--memory-swap=2g`), so no extra swap. When it is exceeded, the kernel's out-of-memory killer ends the process. The harness reports a non-zero exit, or no result bundle, which classifies as **failed** or **crashed**. Never passed.

### Q11.26 A generated process creates thousands of child processes. How should process limits be enforced?

**Frame.** How is a fork bomb stopped?

**Answer.** A cgroup process limit of **256** (`--pids-limit=256`). Process creation beyond it fails inside the container, and the host is unaffected. The run ends in failure or timeout.

### Q11.27 A test suite generates several gigabytes of temporary files. How should disk usage be bounded?

**Frame.** How is disk capped?

**Answer.**

- the image filesystem is **read-only**, so nothing can be written there
- `/tmp` is an in-memory filesystem capped at **1 GB**, mounted no-exec
- the workspace is a volume capped at **2 GB**
- writes past a limit fail with "no space", so the run fails
- results returned to the platform are capped (logs at 50 MB, stored as an artifact)

### Q11.28 A generated process never terminates. How should execution timeouts be enforced?

**Frame.** How is a run that never ends stopped?

**Answer.** Three layers:

1. **Per test:** 120 s, enforced by the harness.
2. **Whole run:** 900 s wall clock, enforced by the launcher, which stops then kills the container after 10 s.
3. **Workflow:** the activity's timeout and heartbeat (Artifact 8).

A run killed by a timeout is classified **timeout** (tested).

### Q11.29 Every patch needs a clean environment that cannot inherit state from a previous test. How should ephemeral execution environments be created?

**Frame.** How is every run kept clean?

**Answer.**

1. A **fresh container** from the pinned image, a **new workspace volume** checked out at `base_revision`, and the patch applied by the harness.
2. Nothing is shared between runs: no reused volumes, caches or databases.
3. A test database, if needed, is started fresh inside the same sandbox.
4. Each run has its own ID, used in the container name, its labels and its result bundle.

### Q11.30 A test completes, but its container remains running and consumes infrastructure resources. How should sandbox cleanup work?

**Frame.** How do we guarantee cleanup?

**Answer.**

- `--rm` deletes the container on exit, and the launcher deletes the workspace volume.
- **A reaper** runs every 5 minutes, removing any container or volume labelled `aisre.sandbox=1` older than 60 minutes (Artifact 4, FR-23 target: none left after 60 minutes).
- Cleanup is part of the Test Runner activity's `finally`; cancellation also triggers it (Artifact 8).
- Leftover counts are a platform metric, alerted above zero.

### Q11.31 The organization relies exclusively on ordinary containers for executing hostile code. Which security limitations should be considered?

**Frame.** Why aren't plain containers enough?

**Answer.**

- **A shared kernel:** every container uses the host kernel, so one kernel flaw can let code escape.
- **Misconfiguration is easy:** a privileged flag, an extra capability or a mounted socket undoes the isolation (Q11.23).
- **Runtime flaws:** the container runtime itself has had escape vulnerabilities.
- **Side channels and resource contention** between containers on one host.
- **Default settings vary:** system-call filtering and security profiles can be weakened by mistake.

Hence gVisor, a dedicated host, locked-down launch arguments that the launcher validates, and nothing valuable in reach.

### Q11.32 The threat model requires stronger isolation than ordinary containers provide. Under which circumstances should microVMs, Firecracker or gVisor be evaluated?

**Frame.** When do we need stronger isolation?

**Answer.**

- **gVisor (chosen now):** whenever hostile code runs. It intercepts system calls in user space, and has low overhead for test workloads.
- **Firecracker microVMs (next step):** each run gets its own minimal virtual machine and kernel; the boundary is hardware virtualisation. Move to it if:
    - an escape test fails under gVisor
    - tests need system calls that gVisor doesn't support
    - the platform starts running code with network access or real data
    - compliance requires virtual-machine isolation
- **Re-evaluate** when the sandbox's purpose widens (for example, new code written by the model, not only reverts), or after any security incident.

### Q11.33 The model proposes a new sandbox configuration as part of its patch. Who should retain authority over sandbox creation and configuration?

**Frame.** Who controls the sandbox?

**Answer.** **The platform, owned by the security lead. Never the model, never the patch.**

- `SandboxSpec` is versioned platform code. Changes need security review.
- The launcher builds arguments **only** from the spec (`launch_args`), and validates them (`validate_args`).
- No field of a patch, model output or action can add mounts, devices, capabilities, network or variables.
- The sandbox and harness directories are protected paths (Q11.4).

### Q11.34 The generated patch attempts to modify the trusted sandbox launcher itself. How should the platform prevent this?

**Frame.** How is the launcher protected?

**Answer.**

1. **It lives in a different place:** the launcher is part of the platform's own codebase, not the repository being patched, so the patch can't reach it.
2. **Protected paths** refuse `sandbox/**` and `harness/**` in any repository (tested).
3. **The harness runs from the pinned image,** not from the workspace.
4. **Platform changes need code-owner review** by the security lead, through a separate pipeline.

### Q11.35 The engineering team needs to establish whether an individual function behaves correctly. Which testing approach should be used?

**Frame.** How is one function tested?

**Answer.** **Unit tests:** small, fast, isolated tests of one function or class, with its dependencies replaced by fakes. They run in the sandbox as the `unit` suite (required).

### Q11.36 A function works independently but fails when communicating with PostgreSQL. Which testing approach would expose this integration failure?

**Frame.** How do we catch failures between components?

**Answer.** **Integration tests** against a real PostgreSQL, started fresh inside the same sandbox (the same version as production), with the schema migrated and test data seeded. They are the `integration` suite (required). If the database can't start, the run is **inconclusive**, not passed (Q11.48).

### Q11.37 A new patch fixes one issue but breaks previously working functionality. How should existing behavior be tested?

**Frame.** How do we catch new breakage?

**Answer.** **Regression testing:**

- run the **full existing suite** at the base revision, then again with the patch
- any test that passed before and fails after is a regression, so the outcome is `failed`
- add **error-signature comparison** (Q11.41)
- add a **smoke suite** of key user paths

### Q11.38 The original production failure cannot be reproduced in the development environment. How could a dedicated test recreate the original failure?

**Frame.** How do we recreate the incident's failure?

**Answer.** A **reproduction test**, built by the harness from the evidence (not by the model):

- **configuration:** the production values at the failing revision, with secrets replaced by fakes
- **request:** the failing path, method and payload shape from the traces, with personal data removed
- **assertion:** the original symptom, for example an HTTP 500 with the same error signature

In the reference case: start the service with `payments.timeout_ms: 0`, call `/api/pay`, and expect a 500 with the timeout error. The test is stored, and reused for verification (Q11.83).

### Q11.39 A patch appears to fix an error, but the original failure may never have been reproduced successfully. How would you establish a valid before-and-after comparison?

**Frame.** How do we prove the comparison is real?

**Answer.**

1. **Before:** run the reproduction test on the **unpatched base revision**. It **must fail** with the original symptom. If it doesn't, the outcome is **`not_reproduced`**, and testing stops (tested).
2. **After:** apply the patch, and run the same test in the same environment. It must pass.
3. Both runs are in the same result bundle (`reproduction_before`, `reproduction_after`).

Without a failing "before", a passing "after" proves nothing.

### Q11.40 The patch passes a newly generated test, but engineers need evidence that the original failure has actually been fixed. Which tests should be executed?

**Frame.** Which tests prove the fix?

**Answer.** All of these, and all required:

| **Suite** | **Proves** |
| --- | --- |
| Reproduction, before and after | The original symptom existed, and is gone |
| Existing unit suite | Nothing else broke at function level |
| Existing integration suite | Nothing broke at the service boundaries |
| Smoke suite | Key user paths still work |
| Error-signature comparison | No new error types (Q11.41) |

A test the model wrote can be added as **extra** evidence, but never replaces these, and never counts toward "passed" on its own.

### Q11.41 A patch fixes the original failure but introduces a different application error. How should the platform detect this regression?

**Frame.** How do we catch a new, different error?

**Answer.**

- **Error signatures:** the harness collects error types and stack fingerprints from logs and test output, **before and after**.
- **Any signature present only after the patch** is a `new_error_signature`, so the outcome is `failed` (tested).
- **Plus the regression suites** (Q11.37).
- **In production, the verifier** checks for new error groups after the change (Q11.87).

### Q11.42 The AI modifies both the application and its test suite. How should the trusted test suite remain independent of candidate code?

**Frame.** How do the trusted tests stay independent?

**Answer.**

- **Tests come from the base revision, never from the patch.** Test files are protected, and the harness checks out tests from `base_revision` separately.
- **The harness itself lives in the pinned image,** outside the workspace.
- **Test configuration is protected too** (`conftest.py`, `pytest.ini`, `pyproject.toml`).
- **The result is produced by the harness,** not read from anything the patch can write.

### Q11.43 The generated patch attempts to modify the test harness to bypass a failing assertion. Which security boundary should prevent this?

**Frame.** What stops tampering with the harness?

**Answer.** **The boundary between the trusted harness and the untrusted workspace:**

- the harness runs from the read-only image filesystem
- the patch is applied only to `/workspace`
- the harness reads results from its own test runner, not from files in the workspace
- harness paths are protected at the patch boundary check too (Q11.34)

A patch that tries is rejected before testing.

### Q11.44 A sandbox finishes executing a patch. Which artifacts should it return to the platform?

**Frame.** What comes back from the sandbox?

**Answer.** A **`ResultBundle`**:

- run ID, **patch digest**, base revision
- image digest, sandbox spec version, harness version
- reproduction before and after
- each suite: required, ran, passed, failed, errors
- new error signatures
- exit code, duration, timed-out flag
- the fingerprint of the full log, stored in the artifact store, secret-scanned, capped at 50 MB

Nothing else leaves: no files from the workspace, no binaries.

### Q11.45 Two different patches produce different test results. How should test results be associated with the exact patch that was executed?

**Frame.** How is each result tied to its patch?

**Answer.**

- the bundle records the **patch digest** that was applied, computed by the harness from the applied content
- `classify` rejects a bundle whose digest doesn't match the expected patch (classified as crashed; tested)
- the bundle's own digest goes into the immutable action (`test_bundle_digest`)
- `test_runs` rows (Artifact 7) store the patch digest and the run ID

### Q11.46 A sandbox process crashes before returning its results. How should the platform classify this execution?

**Frame.** What is a crashed run?

**Answer.** **`crashed`****:** no valid bundle, no exit code, or a bundle for a different patch (tested). Crashed is **not** passed or failed; it's "no answer". It may be rerun once, as a new run ID, within the sandbox budget. A second crash hands over with the logs. A crash pattern (an out-of-memory kill, for example) is recorded.

### Q11.47 A test suite runs indefinitely because of a deadlock. How should the platform handle execution timeouts?

**Frame.** How is a hung suite handled?

**Answer.**

- the per-test limit (120 s) is enforced by the harness, and the hung test is killed
- the run's wall clock (900 s) is enforced by the launcher, and the container stopped, then killed
- the outcome is **`timeout`**, never passed (tested)
- it isn't rerun automatically: a deadlock usually repeats, so the incident hands over with the hung test's name

### Q11.48 Some tests pass, but critical integration tests cannot execute because a dependency is unavailable. How should the platform represent an inconclusive result?

**Frame.** How is "couldn't run everything" shown?

**Answer.** As **`inconclusive`**: any required suite has `ran = false` (tested). The bundle says which suite, and why (for example, "test database failed to start"). Inconclusive is never treated as passed, and never reaches approval. The run may be retried once if the cause is infrastructure; otherwise the incident hands over.

### Q11.49 An AI system can generate and execute its own production repair. Why would allowing the same system to authorize its own action create a security problem?

**Frame.** Why can't the proposer approve?

**Answer.**

- **No independent check:** the same reasoning that may be wrong, or manipulated by injected text, also decides it's safe.
- **No accountability:** no person can be asked why.
- **Injection becomes production change:** text in a log could drive diagnosis, patch and approval end to end.
- **It breaks separation of duties,** which audits and policies require.

So `record_decision` refuses the proposer's identities and any `system:` identity (tested). The authorisation matrix gives the workflow and the dispatcher no approval permission at all.

### Q11.50 An engineer claims to be the person approving a patch. How should the platform establish their identity?

**Frame.** How do we know who's approving?

**Answer.**

- sign-in through the organisation's identity system (single sign-on), **with two-factor**
- the API receives a short-lived signed token: the identity system's subject, authentication time, and two-factor claim
- **approval needs a sign-in within the last 15 minutes, with two-factor;** otherwise `reauth_required` (tested)
- the decision records the subject, authentication time and two-factor claim, and is signed by the Approval Service

### Q11.51 An authenticated engineer attempts to approve an action for a service they do not control. How should the platform enforce authorization?

**Frame.** How do we stop approvals outside someone's services?

**Answer.**

- **Permissions are per service:** the directory maps (person, service) to roles.
- **`record_decision`** **checks** `action:approve` on the action's **target service**; refused as `forbidden` otherwise (tested: an approver for payments can't approve checkout).
- **The dispatcher re-checks** at execution time, against the current directory (Q11.59).

### Q11.52 Different users need different permissions to operate, approve and administer the platform. How should these roles be separated?

**Frame.** How are roles separated?

**Answer.** By the **authorisation matrix** (11.7). The key separations:

- **administrators can't approve changes:** platform administrators run the platform; security administrators own policies and keys (tested)
- **approvers can't change policy or keys**
- **the platform's own identities can't approve:** the workflow proposes, and the dispatcher executes only
- **responders can reject, but not approve,** so anyone on the incident can stop a bad change

### Q11.53 An approver receives a notification saying that the AI wants to fix an incident. What exact information should the approver review before making a decision?

**Frame.** What does the approver see?

**Answer.** The approval screen (CLI or API) shows, in this order:

1. **The action:** kind, exact target (service, environment, deployment), base revision and expected revision after, and the **action digest**.
2. **The diff,** with each hunk's rationale and evidence links.
3. **The diagnosis:** the leading cause and confidence, the evidence for it, **the evidence against it**, and the alternatives with why they were ruled out.
4. **The test results:** reproduction before and after, each suite, new error signatures, and the bundle digest.
5. **Risk and the rollback action:** what happens if verification fails, and the rollback action's own digest.
6. **Recovery predicates:** what "recovered" will mean.
7. **Flagged items:** gaps, stale evidence, and text flagged as possible injection.
8. **Expiry:** when the request and any approval expire.

A notification saying "the AI wants to fix this" is only a link. Decisions are made on this screen.

### Q11.54 An engineer approves a patch, but the model changes it one second later. How should approval be cryptographically or logically bound to the original patch digest?

**Frame.** How is approval bound to the exact patch?

**Answer.**

- **The approver submits the action digest they reviewed** (`presented_digest`). If it differs from the current action, the request is refused as `digest_mismatch` (tested).
- **The decision records** **`action_digest`****,** which covers the patch digest, base revision, target, policy version, test bundle and predicates.
- **The decision is signed** (Ed25519) by the Approval Service; any edit to the decision fails verification (tested: a forged subject is ignored).
- **The model can't change an action:** actions are immutable, and a changed patch is a new action, with a new digest, that no one has approved (tested: same action ID with different content → pending).

### Q11.55 An approved action is associated with several different application environments. How should the approval identify the exact target?

**Frame.** How is the target made exact?

**Answer.** An action has **exactly one target:** service, environment and deployment name (`checkout-prod-eu1`), all in the action digest. Changing several environments means several actions, each tested and approved on its own. The dispatcher applies only to the deployment named in the reloaded action.

### Q11.56 An engineer approves a patch against an old version of the application. How should the approval be bound to the source revision?

**Frame.** How is approval tied to a revision?

**Answer.** `base_revision` is in the action digest the approval signs. The dispatcher checks the target's **current revision equals** **`base_revision`** immediately before writing; otherwise it refuses (tested). Approving against an old version therefore can never apply to a new one.

### Q11.57 An administrator changes the security policy after an approval has been issued. How should the original policy version be preserved and validated?

**Frame.** What happens to approvals when policy changes?

**Answer.**

- **Every action and decision records the policy versions in force:** the edit policy and the approval policy.
- **At execution, the dispatcher compares them with the current policy.** If the approval policy changed, earlier approvals are **not counted**, and a fresh approval under the new policy is needed (tested).
- **Old versions are kept,** so an audit can show which rules applied.
- **Rule:** a policy change can only make execution stricter, never looser, for decisions already given.

### Q11.58 An approval request remains pending indefinitely. How should the platform enforce approval expiry?

**Frame.** How do pending requests expire?

**Answer.** Two clocks (Artifact 8, ADR in Artifact 4):

- **Request:** escalates at 15 minutes, **expires at 60** (the workflow timer, and `evaluate` returns `EXPIRED`; tested).
- **Granted approval:** valid **15 minutes** (`valid_until`); after that it isn't counted (tested).

Expired requests are closed, the action becomes `expired`, and nothing executes.

### Q11.59 An engineer approves a patch but subsequently loses their production permissions. Should the previous approval remain valid, and how should this be decided?

**Frame.** Does an approval survive the approver losing access?

**Answer.** **No.** The dispatcher re-evaluates approvals **at execution time against the current directory**. An approver who no longer holds approval rights for the service no longer counts (tested), and a fresh approval is needed.

**Rationale:** losing access often signals a role change or a security concern, and the approval window is short (15 minutes), so the cost is small. Already-executed changes are unaffected: their record stays as it was.

### Q11.60 Two approvers submit conflicting decisions for the same action. How should the platform resolve this conflict?

**Frame.** What wins when approvers disagree?

**Answer.** **Any valid rejection wins**, regardless of order or number of approvals (tested). The action becomes `rejected`, which is final; the incident hands over. Both decisions are kept. Changing course needs a new action.

**Rationale:** a safety stop should be cheap, and approving should need agreement. High-impact changes need two different approvers (tested).

### Q11.61 An API accepts an approval request containing replacement patch content. How could this allow an attacker to substitute the approved action?

**Frame.** Why must approval requests never carry patch content?

**Answer.** If the approval call carried the patch (or the target), an attacker, or a buggy client, could send an approval for content the approver never saw: the screen shows patch A, and the request approves patch B. The approval would look legitimate.

So the **Approval API accepts only:**

- the action ID (in the path)
- the **action digest** the approver saw
- the decision, and a reason

Everything else is read from trusted storage. A digest mismatch is refused. The dispatcher also takes only an action ID (Q11.68).

### Q11.62 An engineer rejects a patch but the workflow continues toward execution. How should rejection become an enforced state transition?

**Frame.** How is a rejection made binding?

**Answer.** In three places, any one of which stops execution:

1. **The workflow:** the rejection signal moves the action to `rejected` and the incident to `handed_over`; `rejected` has no outgoing transitions (Artifact 8, `states.py`).
2. **The database:** a rejection is an immutable `approval_decisions` row.
3. **The dispatcher:** `evaluate` returns `REJECTED` if any rejection exists, and the dispatcher refuses, even if a stale workflow asked (tested).

### Q11.63 An approver wants to withdraw an approval before execution begins. How should approval revocation work?

**Frame.** How does someone take back an approval?

**Answer.**

- **`POST /v1/actions/{id}/revocations`**, with the approval ID to withdraw, the action digest and a reason. Recent sign-in with two-factor is required.
- **Only the person who approved** can revoke their own approval (`not_own_approval` otherwise). Anyone with reject rights can instead **reject**, which stops the action outright.
- **The revocation is a new signed decision.** The original stays in the record.
- **Effective until execution starts:** the dispatcher re-evaluates first, and a revoked approval no longer counts (tested: the high-impact action drops back to pending).
- **After execution starts,** undoing the change is a rollback action (Q11.90).

### Q11.64 An auditor needs to establish who approved a production change and when. Which information should be recorded?

**Frame.** What does the audit record contain?

**Answer.** For every decision (`Decision`, stored in `approval_decisions`, and also as a timeline event):

- approval ID, action ID, **action digest**
- decision: approve, reject or revoke
- **subject** (the identity system's ID), roles held at the time
- **authentication time, two-factor claim**
- **policy version**, decision time, valid until, reason
- **signing key ID and signature**

Linked to it: the action (patch digest, test bundle, target, revisions), the execution ledger entry, and the verification result. The timeline is hash-chained and copied to a separate store (Artifact 5).

### Q11.65 The AI generates a tool call that directly modifies production infrastructure. How should the platform prevent model-generated tool calls from executing without authorization?

**Frame.** How do model tool calls never become production writes?

**Answer.**

- **No write tools are offered to the model** (Artifact 9's registry). A write tool name is refused (`write_forbidden`).
- **Tool proposals are data** (Artifact 10); the gateway never runs them.
- **The only write path is the dispatcher,** a separate process, triggered by the workflow with **an action ID only**, after a signed approval.
- **The dispatcher holds the only production write credential** (Artifact 6).

A model-generated call has no route to production at all.

### Q11.66 Different components have different levels of authority over production resources. How could a capability-based permission model restrict each component?

**Frame.** How do capabilities limit each component?

**Answer.** Each component holds only the specific capabilities its job needs, as narrow, separate credentials:

| **Component** | **Holds** | **Can't do** |
| --- | --- | --- |
| Agent and model | Nothing | Anything external |
| Evidence Collector | Read, per tool, per service | Write |
| Patch Proposer | Create a draft pull request | Merge, deploy |
| Test Runner | Create sandboxes on the test host | Reach production |
| Approval Service | Sign decisions | Deploy |
| Dispatcher | `apply`, `lookup` and `current_state` on named deployments | Approve, sign, run arbitrary commands |
| Verifier | Read monitoring | Write anything |

Holding the dispatcher's credential still doesn't let anyone approve. Holding the signing key still doesn't let anyone deploy.

### Q11.67 The AI needs to execute a repair, but should only be able to invoke a limited set of predefined operations. How should a narrow allowlisted execution interface be designed?

**Frame.** What does the execution interface look like?

**Answer.** The `ReleaseProcess` interface has **three operations**, all keyed:

- `current_state(deployment)` → revision, and any in-progress key (read)
- `apply(deployment, execution_key, base_revision, patch_digest, to_revision)` → applied or rejected (write)
- `lookup(deployment, execution_key)` → none, applied, in progress or failed (read)

There is no shell, no free-form command and no general API passthrough. Supported action kinds map to `apply` with fixed parameters: configuration revert, commit revert and release rollback. The release process (not the platform) performs the deployment through its normal, audited pipeline.

### Q11.68 A dispatcher receives an action request containing patch information from the model. Why should it reload the original immutable action from trusted storage instead of trusting the supplied arguments?

**Frame.** Why reload instead of trusting the request?

**Answer.** Because the request is not trusted: it could come from a compromised worker, a bug, or arguments shaped by injected text. If the dispatcher used supplied patch content or targets, it would apply **whatever it was sent**, regardless of what was approved.

So `Dispatcher.execute(action_id)` takes **only the ID**. It loads the action and decisions from trusted storage, recomputes the action digest, and verifies signatures against **that** digest (tested: changed content under the same ID isn't approved).

### Q11.69 An action was approved several minutes ago, but the target environment has changed. Which conditions must be revalidated immediately before execution?

**Frame.** What is re-checked right before writing?

**Answer.** All of these, in `_prechecks`, after reloading the action:

1. **Signatures** valid, and decisions bound to this action digest.
2. **No rejection;** revocations applied.
3. **Approvals unexpired** (within 15 minutes).
4. **Approvers still authorised** for the service.
5. **Enough distinct approvers** (two if high-impact).
6. **The policy version** is unchanged.
7. **The target runs exactly** **`base_revision`****.**
8. **No other deployment is in progress** on the target.

Any failure means `REFUSED`, nothing sent, with the reason recorded (tested).

### Q11.70 Multiple operations target the same service and source revision. How should each operation receive a stable identity?

**Frame.** How does each operation get its own identity?

**Answer.** Each is **its own immutable action** with its own **action ID** (a UUID), created once when the action is recorded. The **execution key is the action ID**. Two actions on the same service and revision have different IDs and different digests. A rollback is a separate action with its own ID, linked by `parent_action_id`.

### Q11.71 A worker retries an action after a network failure. How could a stable action ID prevent duplicate external writes?

**Frame.** How does the stable ID stop duplicates?

**Answer.**

- **On our side:** the dispatcher's ledger is keyed by the action ID. An `applied` entry returns "already applied" without a second write (tested: two executions, one `apply` call). An `unknown` or intent-only entry leads to reconciliation, never a resend.
- **On the target's side:** the release process receives the same key, and returns its recorded result for a repeat (Q11.72).
- **In the database:** at most one `applied` attempt per execution key (Artifact 7 constraint).

### Q11.72 An external deployment system receives the same action twice. How could the target maintain an action-key ledger to identify previously processed requests?

**Frame.** How does the target recognise a repeat?

**Answer.** The release process keeps a **ledger keyed by execution key**: key, received time, status, resulting revision. On `apply`:

- **unknown key:** run it, and record it
- **key in progress:** return "in progress"
- **key finished:** return the recorded result without running again

Keys are kept for at least 30 days. The fake release process in the tests behaves exactly this way. Confirming the real release process supports it, or adding a thin adapter in front of it, is an M3 dependency (Artifact 2 open question).

### Q11.73 A patch was generated against revision A, but the application is now at revision B. How should the dispatcher prevent the change from being applied to an unexpected version?

**Frame.** How do we avoid applying to the wrong version?

**Answer.** Two checks:

1. **The dispatcher** reads `current_state` and refuses unless the revision equals `base_revision` (tested).
2. **The release process** receives `base_revision` with `apply`, and rejects if it doesn't match: a compare-and-set, covering a deployment that lands between the check and the write.

Either way, the result is a new patch against B, tested and approved again.

### Q11.74 A production write succeeds, but the dispatcher loses its response. How should the platform establish whether the write occurred?

**Frame.** How do we find out if the write happened?

**Answer.** **Reconcile** (`Dispatcher.reconcile`):

1. Ask the release process, `lookup(deployment, key)`.
2. Read the target's current revision.
3. **Applied** in the target's ledger **and** the revision equals the expected revision after → `applied`.
4. **Not found or failed,** **and** the revision still equals the base → `not_applied`.
5. **In progress** → wait, and ask again (up to 10 checks).
6. **Disagreement, or still unclear** → `unknown`; hand over; a person checks.

Tested: a lost response reconciles to applied with one `apply` call; a lost request reconciles to not applied.

### Q11.75 A dispatcher reports a timeout, but the external system may have completed the operation. Why should a timeout not automatically be interpreted as failure?

**Frame.** Why isn't a timeout a failure?

**Answer.** A timeout only means **we didn't hear back**. The request may never have arrived, may have been applied, or may still be running. Treating it as failure would invite a retry that applies the change **twice**, or a "fix" that runs against a state we don't know. So a timeout is recorded as `unknown`, and reconciliation decides (tested).

### Q11.76 An external action has an uncertain outcome. Which reconciliation mechanism should establish its actual effect?

**Frame.** What establishes the real effect?

**Answer.** Two independent sources, which must agree:

- the **release process's own record** for the execution key
- the **observed state of the target:** its current revision, or the configuration value in the reference case

Only agreement resolves the outcome. Disagreement stays `unknown` and goes to a person (tested: the release says applied but the revision is still the base, so unknown, and no resend).

### Q11.77 An organization wants to guarantee that every production write executes exactly once, even when network responses are lost. Why might this guarantee be impossible without cooperation from the target system?

**Frame.** Why can't we promise exactly once alone?

**Answer.** From the outside, we can't tell "not received" from "received and done" when the answer is lost (Artifact 8, Q8.42). Without cooperation from the target:

- **retrying** risks running it twice
- **not retrying** risks never running it
- **no transaction** covers both systems

Exactly-once **effect** needs the target to:

- **deduplicate by key** (an idempotency ledger, Q11.72)
- **let us look up** a key's status
- ideally **compare-and-set** on the base revision (Q11.73)

With those, the platform achieves effectively once. Without them, the honest guarantee is "at most once, plus a person when unsure".

### Q11.78 A previous execution has an unknown outcome. Under which circumstances should automatic retries be prohibited?

**Frame.** When must the platform not retry?

**Answer.** **Always, while the outcome is unknown.** Specifically:

- the ledger shows `intent_recorded` or `unknown` for the key: reconcile only (tested)
- reconciliation is still unclear: hand over; no further production action on that deployment until a person records the actual state
- the target lacks key lookup or deduplication: never retry a write
- the approval has expired, or the target revision changed: a new action is needed anyway

An automatic retry is allowed only when reconciliation **confirms** `not_applied`, the approval is still valid, and the target is still at the base revision. In the first version even that goes back to a person (ADR-29)

### Q11.79 The dispatcher reports that an action executed successfully, but users are still experiencing errors. Why is an execution success response insufficient to establish service recovery?

**Frame.** Why isn't "deployed" the same as "recovered"?

**Answer.** "Applied" means the release process changed what runs. It doesn't show that:

- the diagnosis was right
- the new configuration took effect on every instance
- users' requests now succeed
- nothing else broke

Only measurements of the service, from monitoring and over time, show recovery. Hence a separate verifier with predicates set before the change (Artifact 1).

### Q11.80 The AI reports that the application is healthy after a repair. How could a separate verification component independently establish this claim?

**Frame.** How is health established independently?

**Answer.** The **Recovery Verifier**:

- a separate component, with **read-only monitoring credentials**
- it reads metrics and runs probes **directly**; it never takes the model's or the dispatcher's word
- it evaluates the **verification plan fixed before execution** (its digest is in the immutable action)
- it returns `verified`, `failed` or `inconclusive` (`verification.verify`)

The model's statement isn't an input at all.

### Q11.81 An application has several service health requirements. Which measurable recovery conditions should the verifier evaluate?

**Frame.** What does "recovered" mean, measurably?

**Answer.** The reference plan (`reference_plan`):

| **Predicate** | **Condition (provisional)** |
| --- | --- |
| Error rate on the failing path | ≤ 1.1 × pre-incident baseline |
| Error rate for the service | ≤ 1.1 × baseline |
| Latency p95 | ≤ 1.2 × baseline |
| Synthetic probe of the failing request | ≥ 99% success |
| Availability (success ratio) | ≥ 99.5% |
| New error groups since the change | 0 |

Each must hold **at every 60-second interval for 10 minutes**, with at least 50 requests per interval.

### Q11.82 The platform needs to determine whether users can successfully access the application. Which health checks should be performed?

**Frame.** Which checks show users get through?

**Answer.**

- **Real-user signals:** success ratio and error rate, from monitoring.
- **Synthetic probes:** the verifier calls the originally failing request (path, method, payload shape) and a short list of key user paths, from outside the service, every interval.
- **Service health endpoint:** necessary but not sufficient. A process can report healthy while every request fails.

### Q11.83 A patch was intended to resolve an HTTP 500 error. How could the verifier reproduce the original failure condition?

**Frame.** How does the verifier re-test the original failure?

**Answer.** It reuses the **reproduction test's request** (Q11.38) as a **synthetic probe** against production: the same path and method, with a safe, synthetic payload. Read-only or test-account requests are marked as synthetic so they don't affect real data. Success means the 500 no longer occurs for that request, every interval, across the window. A probe that can't be made safe for production isn't used; real-user error rate on that path is used alone.

### Q11.84 The application returns HTTP 200 once after the patch is applied, but continues failing intermittently. How should post-repair error rates be evaluated?

**Frame.** How do we judge intermittent failures?

**Answer.** By **rates over many requests, at every interval**, never single responses:

- the error rate on the failing path must be ≤ 1.1 × baseline in **every** 60-second interval
- one bad interval fails verification (tested: a relapse at the third interval fails)
- intervals with fewer than 50 requests don't count, which keeps the result statistically sound (Q11.88)

### Q11.85 A repair reduces the error rate but increases request latency significantly. How should recovery verification account for both conditions?

**Frame.** What if errors drop but latency rises?

**Answer.** **All required predicates must hold together.** Latency p95 has its own predicate (≤ 1.2 × baseline). Fixing errors while making the service much slower is `failed` (tested). Trade-offs are a person's call: the incident hands over, with both measurements.

### Q11.86 The application passes one health check immediately after repair but fails again two minutes later. How should the verifier establish a sufficiently long observation window?

**Frame.** How long must recovery hold?

**Answer.**

- **10 minutes, sampled every 60 seconds** (provisional; Artifact 4). Every predicate must hold in every interval.
- **Fewer intervals observed than required** → `inconclusive` (tested).
- **Longer for slow failures:** the window is per service in the verification plan, for example 30 minutes where failures appear only under peak load or after caches expire.

### Q11.87 A repair appears successful, but an unrelated application regression emerges shortly afterward. How should the platform monitor for new failures?

**Frame.** How do we catch a new failure after the fix?

**Answer.**

- **Inside the window:** "new error groups since the change = 0" is a required predicate (tested: a new error group at minute 7 fails verification).
- **After the window:** a **post-change watch** of 60 minutes (provisional) flags new error groups, or a breach of the service's alerts, and links them to the action as a possible regression, for a person to judge. It doesn't trigger automatic rollback.
- **The change is marked on dashboards** (Artifact 3), so later responders see it.

### Q11.88 A repair has been applied, but telemetry is insufficient to establish whether it worked. How should the verifier distinguish verified, failed and inconclusive outcomes?

**Frame.** How are the three outcomes decided?

**Answer.**

- **Failed:** any required predicate is violated in any interval with enough data. Failure takes precedence.
- **Inconclusive:** no violation, but a required predicate lacks data (missing metric, or fewer than 50 requests in an interval), or the window wasn't fully observed (tested).
- **Verified:** every required predicate held in every interval, for the full window, with enough data (tested).

Inconclusive is **not** verified: the incident hands over, with what was and wasn't measured.

### Q11.89 A repair fails verification. What should happen next, and under which conditions should the incident be escalated?

**Frame.** What follows a failed verification?

**Answer.**

1. The incident goes to `not_recovered` (Artifact 8).
2. **The rollback action is dispatched,** if its conditional approval holds and the target still runs the revision the fix produced. It is reconciled if its outcome is unknown.
3. The incident goes to `handed_over`; the approver and on-call are notified.

**Escalate to the incident lead at once if:**

- the rollback can't run (approval invalid, or the target changed)
- the rollback's outcome is unknown after reconciliation
- the service is worse than before the change
- verification is inconclusive and impact continues

The platform proposes no further fix by itself.

### Q11.90 A rollback could restore service, but it would itself modify production state. Why should rollback require an independently authorized action?

**Frame.** Why does rollback need its own authorisation?

**Answer.** A rollback **is a production change:** it can fail, be wrong (if something else changed meanwhile), or collide with another deployment. Authorising it implicitly under the fix's approval would let a production write happen that no one decided on. So (ADR-43):

- **the rollback is its own immutable action,** with its own digest: base = the revision the fix produced, after = the original revision
- **it has its own approval decision.** For speed, the approver is shown it alongside the fix, and signs it as a **conditional approval**, valid only if verification fails within the window, and only while the target runs the fix's revision.
- **the dispatcher re-checks it like any action.** If the conditions don't hold, a fresh approval is needed.

### Q11.91 An engineer needs to reverse a previous repair. How should the rollback operation be represented and linked to the original action?

**Frame.** How is a rollback recorded?

**Answer.** As an `ImmutableAction` with:

- `kind = rollback`
- `parent_action_id` = the original action
- `base_revision` = the revision the original produced
- `expected_after_revision` = the original's base revision (`rollback_action_fields`, tested)

It goes through the same approval, dispatch, ledger, reconciliation and verification. The timeline links the two, so an auditor sees the change and its reversal as a pair. A rollback of a rollback is simply another action.

### Q11.92 The service appears healthy, but one required recovery predicate remains unsatisfied. Which conditions must be fulfilled before the incident can be marked resolved?

**Frame.** What must be true to resolve?

**Answer.** All of these (`resolution_allowed`, tested):

1. The verification verdict is **verified:** every required predicate held for the full window, with enough data. One unsatisfied predicate means failed or inconclusive, not resolved.
2. **No action is still open:** nothing executing, reconciling or awaiting rollback.
3. **The incident lead accepts.**

Then recovered → closed (Artifact 8). "Appears healthy" is never enough.
