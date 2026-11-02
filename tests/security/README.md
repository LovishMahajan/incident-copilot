# Security tests: ST-01 to ST-40

The 40 cases in section 5.8 of [Artifact 5](../../docs/layers/layer-05-threat-modeling/artifact-05-threat-model.md), run against the real reference modules in `docs/layers/*/files`. `conftest.py` puts those folders on `sys.path`. `support.py` holds the fixtures, and it fakes only what those modules talk to: the release process, trusted storage and the provider's HTTP transport.

## How to run

```
cd /path/to/incident-copilot
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider tests/security
```

It needs pytest, pydantic, temporalio, cryptography and prometheus_client. No database, Docker or provider is called. Each test name carries its ID, so `-k st13` runs one case.

## How to read a result

| Result | Meaning |
| --- | --- |
| passed | The attack was stopped by the real code. Refusal tests sit beside a control that shows the same set-up succeeds unattacked |
| xfailed | **The attack worked, or a required control is missing.** The test states the expected behaviour and is `xfail(strict=True)`, so the suite fails as soon as a fix lands, until the mark is removed. None are left today |
| skipped | The case needs something that doesn't exist yet. The skip reason names it |

A green run doesn't mean all 40 pass. The promotion gate (ADR-65) needs every case to pass on a running system, with no xfail or skip left.

**Status** below says how much of the case runs here, not whether it passes. Automated: the case's enforcement layer is exercised in full. Partly automated: the module-level half runs, and the rest is skipped. Needs running system: no reference module can exercise it.

Schema-enforced parts are named by their check ID in [schema-tests.sql](../../docs/layers/layer-07-data-apis-events/files/schema-tests.sql), which runs on a real PostgreSQL server, outside pytest. `test_audit.py` confirms each named check exists with the expected label. It doesn't run them.

## Cases

| ID | Status | What it exercises |
| --- | --- | --- |
| ST-01 | Partly automated | Wrong-key and malformed signatures through `Dispatcher.execute`: nothing sent, refusal event written. Bad base64 is ignored like any other bad row. Skipped: voiding the approval and alerting, which no component does |
| ST-02 | Partly automated | Every signed field edited, and action-row edits, against `verify_signature`, `evaluate` and the dispatcher. Immutable rows: schema D4, D5, F2. Skipped: voiding and alerting |
| ST-03 | Automated | A genuine approval for action A stored on action B, plain and with its fields rewritten |
| ST-04 | Automated | A stolen-key decision that was never stored; `execute` and `DispatchIn` accept only the action ID |
| ST-05 | Partly automated | Retired key and borrowed key ID are refused. The fence stops a write, globally or per deployment, including one set during the pre-checks, and a token from before a restore stays refused. The fence is a required constructor argument |
| ST-06 | Automated | Every `system:` identity, even one granted the role, plus requester and proposer, through `record_decision`. Schema F3, F4 |
| ST-07 | Automated | The author as sole approver of a revert is refused and the action waits. The author can be one of two on a high-impact revert |
| ST-08 | Partly automated | Viewer, Responder and Platform admin get `forbidden`. An Approver on another service gets `not_found`, following ADR-58 rather than the ST-08 row. Route-level refusal needs the API |
| ST-09 | Automated | Sign-in older than 15 minutes and no two-factor are refused, a rejection with the same session works, and the boundary is inclusive. Schema F5. Assumes the identity system supplies both claims |
| ST-10 | Automated | Expired approval, lost rights, rejection in either order and revocation before intent, re-checked by the dispatcher. Schema F6, F12 |
| ST-11 | Partly automated | One approval stays pending, the same person is refused `duplicate`, and the workflow signal handler counts them once. Schema E9, E12, E13, E14, F8. High impact is derived, so a false flag lowers nothing: `release_rollback`, sev1, unconfirmed severity, a critical service, more than one deployment and a rollback of a release each need two people. Real creation of actions is untested, as `request_approval` is an interface only |
| ST-12 | Automated | `record_decision` refuses every status but `awaiting_approval`, and a closed request. `states.py` allows no other route to `approved`. The dispatcher reads status, so only an `approved` action runs. Schema H1, C7, F15, F16 |
| ST-13 | Partly automated | Content changes the fingerprint, and a changed fingerprint on the action voids approval. The dispatcher hashes the stored bytes, so swapped or unreadable content stops execution. Skipped: voiding the approval and alerting security and the approver, which no component does |
| ST-14 | Automated | Approval, rejection and revocation contracts refuse patch and target fields. `record_decision` and `DispatchIn` have no such input. The release call carries only stored values |
| ST-15 | Partly automated | Moved deployment refused, a move between check and write caught by compare-and-set, and a new commit is a new digest. Skipped: the review lock |
| ST-16 | Partly automated | The launch holds one marker variable, the parent environment doesn't reach it, and credential variables are refused. `validate_args` is an allow-list, so `-e`, `--env` and `--env-file` are refused. Skipped: real code reading a real sandbox |
| ST-17 | Partly automated | No tool exposes a credential, asking by tool is refused, and environment credentials aren't in the prompt the gateway sends. Skipped: tool results and whole-investigation prompts, as the evidence adapters don't exist |
| ST-18 | Partly automated | Only `system:dispatcher` holds `action:execute`, platform identities hold no approval or key right, every skill is refused `apply_change`, and `dispatcher.py` imports no signing code. Schema E5, E7, E8, E10, F13, G11, K6. Skipped: release, review and secrets systems |
| ST-19 | Needs running system | Nothing here: no API or token verification exists |
| ST-20 | Partly automated | All five named denials in the ADR-50 matrix, each with a control, and a grant not crossing services. Skipped: route enforcement and denial logging |
| ST-21 | Needs running system | Nothing here: the 404, 422 and notification are route behaviour. Dead-letter storage: schema J5 |
| ST-22 | Partly automated | ToolGate refuses other services, other time windows and an environment argument. Retrieval filters service and group before ranking. Skipped: artifact reads and read logging |
| ST-23 | Automated | Commands and destructive statements fail `check_output`, no shell or SQL tool exists, malformed output stops at three attempts, and valid-shaped command output ends inconclusive. Dict keys are scanned. The pattern list is provisional, with controls for ordinary prose |
| ST-24 | Partly automated | Seeded secrets fail validation, the recorded failure doesn't hold the secret, and stored copies are masked. Headers, JSON fields, prefixed keys, GitHub tokens and JWTs fail, a masked private key loses its body, and test output is masked. Skipped: the leak procedure |
| ST-25 | Partly automated | Every launch is `--network=none`, any other network is refused, and no mirror setting exists. Skipped: real connection attempts |
| ST-26 | Automated | A patch can't carry launch options or edit the launcher, and weakening options and specs are refused. `validate_args` is an allow-list, so root, the host root and options nobody listed are refused |
| ST-27 | Needs running system | Nothing here: needs the chosen isolation under real escape tests |
| ST-28 | Partly automated | All limits on the launch, a wall-clock overrun ends as `timeout` and is destroyed, exit 137 is `crashed` whatever the output claims, and the reaper removes what's past 60 minutes. Docker is recorded, not run. Skipped: real fork bomb and fill |
| ST-29 | Partly automated | Links fail `check_output`, no tool can fetch, and a logged link stays text in its data block. Scheme-relative, bare-host, `data:` and `javascript:` links fail too, on a provisional list. Skipped: the canary server |
| ST-30 | Partly automated | Address as its own argument, as the service and as a metric is refused. The five free-text fields refuse a metadata or internal address in the schema, on a provisional list. Skipped: adapter behaviour |
| ST-31 | Needs running system | Nothing in pytest beyond confirming the checks exist. Schema D1, D2, D3, D4, D5, D19 |
| ST-32 | Needs running system | Chain verification: schema I18, I19, I20. `AisreAuditChainBroken` pages on `aisre_audit_chain_faults`, but nothing runs the weekly check that would set the gauge. The write-once store doesn't exist |
| ST-33 | Partly automated | Planted text in all five channels stays labelled data, off-list actions are rejected, and flagged evidence supports nothing. Evidence can't open or close its data block. Skipped: detecting and flagging, as no code sets `flagged_injection` |
| ST-34 | Automated | The model has no confidence field, timing-only evidence scores low and blocks, a critic can't raise it, low ends inconclusive, and missing or foreign citations reject the draft |
| ST-35 | Automated | Oversize input and every incident and fleet cap are refused before the provider is called, retries are charged, tool loops stop, the loop hands over at the cap, and a concurrent burst can't pass it. Status value is `budget_refused`, as in schema.sql. Schema I3, I9, I10 |
| ST-36 | Automated | Off-list action, claimed approval and extra fields rejected whole by the gateway, a link by the output check, a write tool by the gate, and no second provider is ever called |
| ST-37 | Automated | Repeat, concurrent and two-approver dispatches give one write and one `applied` row, and repeated signals are ignored. Schema E1, E2, G1. A repeat or reconciliation inside the in-flight window keeps the row `started` and the block on, and a first call that outlives it never raises |
| ST-38 | Partly automated | Simulated death before sending, and after applying but before confirmation. Intent comes first, reconciliation settles by key with no resend, and unknown blocks the deployment until an Incident lead clears it. Schema G3, G5, G13. Skipped: real process kills and Temporal replay; a same-key retry isn't implemented, which errs on the safe side |
| ST-39 | Partly automated | No approval, no failed verdict, past `valid_until`, off the parent's revision and a plain approval on a proposed rollback are refused. The Verifier has no import path to the dispatcher, and only `verdict == "failed"` calls `_roll_back`. Schema H2, H3b, H4. The dispatcher refuses a plain approval too unless the parent failed. Skipped: an API route |
| ST-40 | Partly automated | A false success, an unreadable target and a repeat dispatch are `unknown` and block the deployment. An out-of-band change refuses the next action. Schema G5. Skipped: the daily reconciliation job and its alert, which don't exist |

## Notes for whoever closes these

- ADR-59 says secret, link and command output is retried. `activities.checked_result` fails it at once instead. That's the safe side, and the tests assert what the code does.
- ST-23, ST-24, ST-29 and ST-30's pattern lists are provisional, and widening them can reject ordinary text. Each has controls for that, but how far to go needs the first evaluation run.
- The workflow's own paths run on Temporal in `docs/layers/layer-08-workflows-state-machines/files/test_workflow.py`. They aren't repeated here.
