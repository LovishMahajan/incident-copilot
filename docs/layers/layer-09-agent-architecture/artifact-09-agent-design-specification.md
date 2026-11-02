# Artifact 9: Agent Design Specification

- **Layer:** 9, Agent Architecture and Engineering Reasoning
- **Built from:** Artifact 3 (Incident and Evidence Catalog), Artifact 7 (Data and Interface Specification), Artifact 8 (Workflow Specification)
- **Status:** Proposed: gaps resolved 2026-11-02 per [decisions.md](../../decisions.md); pending team sign-off
- **Sources:** [conversation.md](conversation.md), [files/coordinator.py](files/coordinator.py), [files/agent_contracts.py](files/agent_contracts.py), [files/tool_registry.py](files/tool_registry.py), [files/validation.py](files/validation.py), [files/test_agent.py](files/test_agent.py), [assets/image2.png](assets/image2.png), [technical-specification.md](../../reference/technical-specification.md), [system-design.md](../../reference/system-design.md), [Layer 3](../layer-03-sre-fundamentals/conversation.md), [Layer 4](../layer-04-requirements/conversation.md), [Layer 7 schema](../layer-07-data-apis-events/files/schema-tables.sql), [Layer 7 contracts](../layer-07-data-apis-events/files/contracts.py), [Layer 8 workflow.py](../layer-08-workflows-state-machines/files/workflow.py), [Layer 10](../layer-10-inference-retrieval/conversation.md), [Layer 12 eval_harness.py](../layer-12-reliability-evaluation/files/eval_harness.py), [decisions.md](../../decisions.md)

## Required sections

| Required section | Questions this section answers | Done |
| --- | --- | --- |
| 9.1 Coordinator | Q9.1–Q9.4, Q9.7–Q9.9 | Yes |
| 9.2 Skill definitions | Q9.11–Q9.13, Q9.15–Q9.19 | Yes |
| 9.3 Typed contracts | Q9.13, Q9.14 | Yes |
| 9.4 Tool registry | Q9.5, Q9.6, Q9.10, Q9.39–Q9.47 | Yes |
| 9.5 Evidence schema | Q9.20–Q9.22, Q9.27–Q9.29 | Yes |
| 9.6 Hypothesis schema | Q9.16, Q9.23–Q9.26 | Yes |
| 9.7 Diagnosis schema | Q9.17, Q9.25–Q9.29 | Yes |
| 9.8 Critic schema | Q9.30–Q9.34, Q9.36, Q9.37 | Yes |
| 9.9 Revision policy | Q9.33, Q9.35 | Yes |
| 9.10 Loop termination policy | Q9.34, Q9.37, Q9.38 | Yes |
| 9.11 Agent evaluation metrics | Q9.28, Q9.47–Q9.53 | Partial |

## 9.1 Coordinator

*Answers Q9.1–Q9.4, Q9.7–Q9.9.*

A large language model only maps text to text: no memory between calls, no access to systems, no way to check its own claims ([Q9.1](conversation.md)). The application wraps it with tools, state, typed skills, a control loop owned by code, and checks. The model proposes; the application runs the tools, validates the output, and decides ([Q9.2](conversation.md)).

The design is a **workflow with an agent loop inside it**. The Temporal workflow (Artifact 8) holds the whole incident for up to 24 hours. Its investigation phase is a coordinator loop that runs skills in turn. Each step is an activity, so it survives crashes, and state is explicit and saved between steps ([Q9.3](conversation.md)).

**How the workflow drives the coordinator** (ADR-52). The workflow owns durability, and the coordinator owns the investigation's decisions. The `investigating` phase of the Layer 8 workflow is this loop:

```python
state = AgentState(started_at=workflow.now(), limits=LIMITS)
while True:
    step = coordinator.next_step(state, workflow.now())   # pure: decides, logs the reason
    if step in (Step.FINISH_ESTABLISHED, Step.FINISH_INCONCLUSIVE):
        break
    result = await self._investigation_act(ACTIVITY[step], skill_input(state, step), deadline)
    apply_result(state, step, result)                     # after_collect, after_hypotheses, ...
    await self._safe_point()
```

- **One step, one activity.** Each result is checkpointed by Temporal, and `AgentState` lives in the workflow, so a replay rebuilds it exactly.
- **A skill failure is a value, not an exception.** The activity returns a typed `SkillFailure` (9.3), and the coordinator stops the loop (9.10). A deadline with no result is recorded the same way, as `deadline_passed`.
- **Crash recovery.** If a worker is lost, Temporal re-runs the activity once on another worker (ADR-27). A completed model result is reused by `input_sha256` (ADR-61), so nothing is re-billed.
- **First summary.** The workflow sends it after the first `HYPOTHESISE` result, marked as not yet reviewed.
- **Leaving the loop.** `FINISH_ESTABLISHED` moves the incident to `diagnosed`, then the Patch Proposer runs (ADR-51). `FINISH_INCONCLUSIVE` follows the incident path in 9.10.

**The investigation loop: observe → hypothesise → test → decide** ([Q9.4](conversation.md))

![Investigation loop: collect baseline evidence, generate hypotheses, collect targeted evidence if requested, diagnose, critic, then established, inconclusive, or a bounded revision](assets/image2.png)

**What changed since this diagram.** It stays as the team's original. The current loop differs in three ways (ADR-52):

- `RETRIEVE_KNOWLEDGE` runs between "Collect baseline evidence" and "Generate hypotheses".
- After `revise`, the revision check runs first. Pending evidence requests run only if it passes, and with none pending the loop goes straight to Diagnose, not through "Collect targeted evidence".
- A targeted round that adds nothing stops the loop, whether or not a critic has run.

**Who decides.** The coordinator is deterministic code (`coordinator.next_step`), not the model. Pure Python, no I/O, no clock of its own; the workflow passes it `workflow.now()` ([coordinator.py](files/coordinator.py)). It decides from recorded facts: whether the critic accepted, whether evidence requests are pending, how many revisions have run, whether the last revision changed anything, the final confidence, and the time, model-call, tool-call, token and spend budgets. The model contributes suggestions only. Every decision is logged with its reason in `agent.decisions`, and exposed through the workflow query ([Q9.7](conversation.md)).

| Step (`Step`) | What it does | Activity |
| --- | --- | --- |
| `COLLECT_BASELINE` | Fixed evidence plan, no model | `collect_evidence` |
| `RETRIEVE_KNOWLEDGE` | Runbook and past-incident passages, as evidence, no model | `retrieve_knowledge` |
| `HYPOTHESISE` | Model: 2–5 hypotheses with claims | `hypothesise` |
| `COLLECT_TARGETED` | Tools chosen from hypotheses or critic requests | `collect_evidence` |
| `DIAGNOSE` | Model plus code scoring | `diagnose` |
| `CRITIQUE` | Code checks, then the independent critic | `review_diagnosis` |
| `FINISH_ESTABLISHED` | Ends the loop: critic accepted, confidence medium or high | None |
| `FINISH_INCONCLUSIVE` | Ends the loop: every other stop (9.10) | None |

**State kept between steps** ([Q9.8](conversation.md)). The model itself keeps nothing between calls: each call is given what it needs, by ID and extract.

| State | Where it lives |
| --- | --- |
| Evidence IDs collected, and gaps | Coordinator state (workflow history); evidence records in PostgreSQL |
| Hypotheses, and each diagnosis revision | PostgreSQL; fingerprints and support ratios in coordinator state |
| Pending evidence requests | Coordinator state |
| Critic verdicts and issues | PostgreSQL; the last verdict in coordinator state |
| Counters: model calls, tool calls, input and output tokens, cost, revisions, rounds, critic failures | Coordinator state, fed from each `SkillResult` (9.3) |
| Final confidence, and any skill failure | Coordinator state |
| Repeated-call hashes | ToolGate log in PostgreSQL |
| Start time and limits | Coordinator state |
| Decision log | Coordinator state, copied to the timeline |

**Model reasoning versus application policy** ([Q9.9](conversation.md)):

- The model's output is a proposal record: a diagnosis with a `proposed_action` from the allowed list. It is data, not a command.
- Policy lives in code the model can't reach: the transition table (Artifact 8), the approval service, the dispatcher's checks, and the tool registry, which has no write tools for the agent.
- A proposal becomes a change only by the path in Artifact 8: a sandbox test, a person's signed approval, and the trusted dispatcher. In Release A the path ends at the tested recommendation, and people change production by hand (ADR-44).
- Nothing the model writes, including injected text, can skip a step: there is no tool or field for it.

## 9.2 Skill definitions

*Answers Q9.11–Q9.13, Q9.15–Q9.19.*

One do-everything agent marks its own homework, gives weaker results on each job, blurs contracts, concentrates power, is hard to measure, and can't have retries tuned per job ([Q9.11](conversation.md)). So jobs are split, each with its own contract and rules ([Q9.12](conversation.md)):

| Skill | Model? | Produces | Validated by |
| --- | --- | --- | --- |
| Evidence Collector | No | Evidence records | Tool schemas, ToolGate, redaction |
| Knowledge Retriever | No | Runbook and past-incident passages, as evidence | Service, permission and staleness filters |
| Hypothesis Generator | Yes | 2–5 hypotheses, with cited claims | Schema; citations; at least one non-deployment cause |
| Diagnoser | Yes, plus code scoring | Diagnosis draft | Schema; citations; confidence scored by code |
| Critic | Code checks, then a separate model | Verdict: accept, revise or escalate | Schema; code issues can't be overruled |
| Patch Proposer | Yes | Patch draft from the allowed list | Allowed list; scope; secret rules |
| *Approval, Dispatcher, Verifier* | **No: application only** | Decisions, changes, verification | Artifact 5 controls |

**What a skill is** ([Q9.13](conversation.md)): one activity with one typed input and one typed output; one versioned instruction set per model-backed skill (`prompts/<skill>/v<N>.md`; each attempt records the version and text fingerprint, ADR-63); a fixed list of tools it may suggest; its own limits (timeout, attempts); its own evaluation set. Skills don't call each other. The coordinator passes results between them by ID.

**Limits, tools and evaluation set per skill.** Timeouts are start-to-close and provisional. Inside the loop each is cut to the time left, as `_investigation_act` does. A model skill's 300 s covers 3 attempts of 60 s plus 2 s and 4 s of backoff, with margin (Artifact 8, 8.5).

| Skill | Timeout | Attempts | Fixed tool list | Evaluation set |
| --- | --- | --- | --- | --- |
| Evidence Collector | 180 s | 3 Temporal attempts (`TOOL_RETRY`). A failed source becomes a gap | Every READ tool (9.4), through the ToolGate | `eval-collector` |
| Knowledge Retriever | 30 s | 3 Temporal attempts (`TOOL_RETRY`). A failed search becomes a gap | `search_runbooks`, `search_past_incidents` | `eval-knowledge` |
| Hypothesis Generator | 300 s (provisional) | 3 gateway attempts, then the step fails | May suggest READ tools only, as `evidence_needed`. Runs none | `eval-hypothesis` |
| Diagnoser | 300 s | 3 gateway attempts, then the step fails | None | `eval-diagnosis` |
| Critic | 300 s (provisional) | 3 gateway attempts per run, 2 runs, 6 at most (ADR-52) | May suggest READ tools only, as `evidence_requests`. Runs none | `eval-critic` |
| Patch Proposer | 300 s (provisional) | 3 gateway attempts per candidate, up to 3 candidates (Artifact 11) | `draft_patch` | `eval-patch` |

Only the gateway retries a model failure (ADR-27). Model activities return every application failure as a value, and Temporal gets one extra run for one purpose: a lost worker (ADR-52).

**Evaluation sets** are named lists of case IDs inside the frozen suite (Artifact 12, 12.8), recorded in its manifest by the SRE lead (provisional). Development cases run nightly and for prompt changes (ADR-63). Held-out cases run only for promotion (ADR-65).

| Set | Suite categories | Scored on | Pass mark (provisional) |
| --- | --- | --- | --- |
| `eval-collector` | All 10, with sources made to fail in `insufficient_evidence` | Each required source collected or recorded as a gap (ADR-54) | 100% |
| `eval-knowledge` | `config_change`, `code_regression`, plus the labelled query set in Artifact 10 | Recall@5; wrong-service and wrong-permission hits | Recall@5 ≥ 0.9; 0 wrong hits |
| `eval-hypothesis` | `config_change`, `code_regression`, `dependency_failure`, `database_failure`, `capacity`, `noisy_coincident_deploy`, `multi_cause` | True cause among the hypotheses | ≥ 90% |
| `eval-diagnosis` | `config_change`, `code_regression`, `noisy_coincident_deploy`, `insufficient_evidence`, `flaky_or_transient`, `multi_cause` | Leading cause, honesty, false established | ADR-65 gates |
| `eval-critic` | Correct diagnoses from `config_change`, `code_regression`, `noisy_coincident_deploy`, each with one seeded fault (9.11) | Catch rate; false rejection | As in 9.11 |
| `eval-patch` | `config_change`, `code_regression`, `prompt_injection` | Unsafe proposals; patches outside the allowed list | 0 of each |

**Evidence Collector boundaries** ([Q9.15](conversation.md)). It has no model call, so it is predictable and testable.

| Does | Doesn't |
| --- | --- |
| Run the fixed baseline plan: alert, changes, 5xx rate, error rate by version, error logs, error groups, failing traces, dependency health | Interpret evidence or rank causes |
| Run requested tools, each through the ToolGate | Choose tools on its own beyond the plan |
| Redact, fingerprint, snapshot, and store each result | Write anywhere except evidence records |
| Record a gap for each failed or timed-out source | Treat a missing source as "healthy" |
| Flag text that looks like instructions | Pass raw logs to the model |
| Stay within the incident's service, window and size limits | Hold or reveal credentials |

**The remaining skills:**

- **Hypothesis Generator:** returns `HypothesesOut` (9.6) ([Q9.16](conversation.md)).
- **Diagnoser:** weighs hypotheses against all evidence, chooses a leading hypothesis or returns inconclusive, explains why each alternative was set aside, lists missing evidence, and proposes an action from the allowed list. It does not own confidence ([Q9.17](conversation.md)).
- **Patch Proposer:** a skill inside the Diagnosis Engine (ADR-51). It runs after the loop, once the critic has accepted, through the PROPOSE tool `draft_patch`, which creates a draft in the review system and changes nothing. Limited to configuration revert, commit revert and release rollback. The draft is then tested in the sandbox and approved by a person ([Q9.18](conversation.md)). If no allowed fix can be drafted, the incident goes from `diagnosed` to `handed_over` (ADR-46).

**Outside the agent** ([Q9.19](conversation.md)): approval, permissions, the transition table, the dispatcher and verification are application code, because the agent reads untrusted text, its output is probabilistic, authorisation needs a named person, policy must be identical every time, and external writes can't be taken back. The agent has no tool, field or path to any of them.

## 9.3 Typed contracts

*Answers Q9.13, Q9.14.*

All contracts are in [agent_contracts.py](files/agent_contracts.py). Every skill takes one Pydantic input and returns one Pydantic output; anything else is rejected whole. The base class `Strict` forbids unknown fields, is frozen, and strips whitespace.

| Skill | Input | Output |
| --- | --- | --- |
| Evidence Collector | `CollectIn` (proposed) | `EvidenceOut` |
| Knowledge Retriever | `KnowledgeIn` (proposed) | `KnowledgeOut` (proposed) |
| Hypothesis Generator | `HypothesisIn` | `HypothesesOut` |
| Diagnoser | `DiagnoseIn` | `DiagnosisDraft` |
| Critic | `CritiqueIn` | `CriticVerdict` |
| Patch Proposer | `PatchIn` (proposed) | Model: `PatchProposal` (proposed). Activity: `PatchOut` |

| Input contract | Fields |
| --- | --- |
| `SkillContext` | `incident_id`, `service`, `environment`, `started_at` |
| `HypothesisIn` | `context`, `evidence`, `previous` (optional), `critic` (optional) |
| `DiagnoseIn` | `context`, `evidence`, `hypotheses`, `critic` (optional) |
| `CritiqueIn` | `context`, `evidence`, `diagnosis`. The critic sees evidence and the diagnosis only, never the diagnoser's reasoning |

**Proposed contracts.** These follow `Strict` and aren't yet in [agent_contracts.py](files/agent_contracts.py). `EvidenceOut` and `PatchOut` keep Layer 8's field names and types, and `PatchDraftParams` stays as it is in [tool_registry.py](files/tool_registry.py).

`CollectIn`, Evidence Collector input:

| Field | Type | Rule |
| --- | --- | --- |
| `context` | `SkillContext` | Required |
| `mode` | Literal | `baseline` or `targeted` |
| `requests` | list of `EvidenceRequest` | Empty for `baseline`; 1–3 for `targeted` |

`EvidenceOut`, Evidence Collector output:

| Field | Type | Rule |
| --- | --- | --- |
| `evidence_ids` | list of str | New evidence records, gap records included |
| `gap_count` | int | How many of those are gaps |

`KnowledgeIn`, Knowledge Retriever input:

| Field | Type | Rule |
| --- | --- | --- |
| `context` | `SkillContext` | Required |
| `evidence_ids` | list of UUID | Baseline evidence. Code builds the search terms from it: alert name, error signatures, changed keys |
| `k` | int | 1–10, default 5 |

Permitted groups come from the platform's identity, never the input. Stale passages are excluded.

`KnowledgeOut`, Knowledge Retriever output. It replaces Layer 8's `list[str]`:

| Field | Type | Rule |
| --- | --- | --- |
| `evidence_ids` | list of str | Passages stored as evidence of kind `runbook` or `past_incident`, gap records included |
| `gap_count` | int | As in `EvidenceOut` |
| `index_revision` | str | The index revision searched (ADR-62) |

`PatchIn`, Patch Proposer input:

| Field | Type | Rule |
| --- | --- | --- |
| `context` | `SkillContext` | Required |
| `diagnosis_id` | UUID | The accepted diagnosis |
| `diagnosis` | `DiagnosisDraft` | `outcome` is `established`, with a `proposed_action` |
| `evidence` | list of `Evidence` | Evidence cited by the leading hypothesis |
| `candidate_no` | int | 1–3 (Artifact 11) |

`PatchProposal`, the Patch Proposer's model output. Code computes hunks and hashes from it (Artifact 11):

| Field | Type | Rule |
| --- | --- | --- |
| `params` | `PatchDraftParams` | `service`, `action_kind`, `target_ref`; `service` must equal the incident's |
| `files` | list of `PatchFileEdit` | Empty only for `release_rollback` |
| `summary` | str | 10–1000 characters |

`PatchFileEdit`:

| Field | Type | Rule |
| --- | --- | --- |
| `path` | str | Relative; must pass the `EditPolicy` |
| `new_content` | str | The file's proposed content |
| `rationale` | str | 10–600 characters |
| `evidence_ids` | list of UUID | At least 1 |

`PatchOut`, Patch Proposer output:

| Field | Type | Rule |
| --- | --- | --- |
| `action_id` | str | The new action |
| `patch_sha256` | 64 hex characters | Fingerprint computed by code (ADR-64) |

**Every activity's result** (proposed, ADR-52). Each skill's output arrives in `SkillResult`, so the coordinator gets one shape.

| Contract | Field | Type | Rule |
| --- | --- | --- | --- |
| `SkillResult` | `output` | The skill's output, optional | Absent on failure |
| | `failure` | `SkillFailure`, optional | Absent on success |
| | `usage` | `SkillUsage` | Always present |
| `SkillFailure` | `skill` | str | Which skill |
| | `code` | Literal | `invalid_output`, `provider_unavailable`, `budget_refused`, `deadline_passed`, `no_allowed_fix`, `store_failed`, `security_risk` |
| | `detail` | `Short` | Recorded in `stop_reason` |
| `SkillUsage` | `model_calls` | int | Gateway attempts; a reused result counts 0 |
| | `tool_calls` | int | Calls recorded by the ToolGate |
| | `input_tokens`, `output_tokens` | int | From the gateway |
| | `cost_micros` | int | Charged amount |
| | `daily_spend_exhausted` | bool | Daily limit reached (ADR-55) |

The Critic's invalid output isn't a `SkillFailure`: it returns `verdict = None` and runs once more (9.8).

**What typed contracts fix** ([Q9.14](conversation.md)):

- **Bad input is refused before running:** a reversed time range, an unknown metric, a limit of 100,000, or an unexpected field. Each fails with a clear code.
- **Defaults are explicit:** with no window given, the tool uses 60 minutes before the incident start, up to now.
- **Output always has the same shape** (`Evidence`), so downstream code can rely on every field.
- **Schemas are shown to the model,** so its requests are well-formed far more often.
- **Contract changes are versioned** and caught in tests.

## 9.4 Tool registry

*Answers Q9.5, Q9.6, Q9.10, Q9.39–Q9.47.*

Direct access would risk destructive commands, injection becoming code execution, secret theft, lateral movement, no review, broken audit trails, and unbounded load ([Q9.10](conversation.md)). Hence no shell, no direct credentials, read-only tools, and writes only through the dispatcher.

**How a capability is exposed** ([Q9.5](conversation.md), [Q9.6](conversation.md)):

1. The model sees only a name, a one-line purpose and a strict input schema.
2. It returns a structured request naming a tool and parameters as JSON, never free text the application interprets.
3. The ToolGate checks it.
4. If it passes, the adapter (which holds the credential) runs it read-only, redacts the result, and stores it as evidence.
5. The model gets the evidence ID and a short extract, or the refusal code. Refusals are recorded.

Tool requests come from the hypothesis skill's `evidence_needed` and the critic's `evidence_requests`. Both are typed `EvidenceRequest` records naming READ tools only, and the coordinator decides whether to run them. The activity that produced them pre-checks each with the ToolGate, and drops and records any that would be refused or that repeat an earlier call. A targeted round therefore runs only calls that are new and allowed.

**Tool kinds** ([Q9.40](conversation.md)):

| Kind | Meaning | Agent may call | Examples |
| --- | --- | --- | --- |
| READ | Reads operational data | Yes | `get_alert`, `get_logs`, `get_metric`, `get_changes`, `get_traces`, `get_error_groups`, `get_dependency_health`, `search_runbooks`, `search_past_incidents` |
| PROPOSE | Creates a draft record; changes no live system | Yes | `draft_patch` |
| WRITE | Changes an external system | **No; application only** | `apply_change` (dispatcher) |

**Registry** (`REGISTRY` in [tool_registry.py](files/tool_registry.py); [Q9.43](conversation.md)). Default window: 60 minutes before the incident start, up to now.

| Tool | Kind | Parameters | Credential scope | Max window | Max rows | Max output | Calls per incident |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `get_alert` | READ | `AlertQuery` | `alerts:read:{service}` | Point in time | — | 16 KB | 2 |
| `get_logs` | READ | `LogQuery` | `logs:read:{service}` | 6 h | 500 | 256 KB | 8 |
| `get_metric` | READ | `MetricQuery` | `metrics:read:{service}` | 24 h | 500 | 64 KB | 10 |
| `get_changes` | READ | `ChangeQuery` | `changes:read:{service}` | 24 h | 500 | 64 KB | 4 |
| `get_traces` | READ | `TraceQuery` | `traces:read:{service}` | 2 h | 500 | 128 KB | 6 |
| `get_error_groups` | READ | `ErrorGroupQuery` | `errors:read:{service}` | 24 h | 500 | 64 KB | 4 |
| `get_dependency_health` | READ | `DependencyQuery` | `catalog:read:{service}` | Point in time | — | 32 KB | 4 |
| `search_runbooks` | READ | `KnowledgeQuery` | `runbooks:read:{service}` | — | 10 | 32 KB | 3 |
| `search_past_incidents` | READ | `KnowledgeQuery` | `incidents:read:{service}` | — | 10 | 32 KB | 3 |
| `draft_patch` | PROPOSE | `PatchDraftParams` | `review:propose:{service}` | — | — | 64 KB | 3 |
| `apply_change` | WRITE | `PatchDraftParams` | `release:write:{service}` | — | — | 16 KB | 1 |

The four added tools' limits are provisional. `draft_patch` allows 3 calls to match the 3 candidates in Artifact 11. `apply_change` is usable only in Release B, and in Release A its credential isn't provisioned (ADR-44).

The gate caps output too. `ToolGate.cap_output` runs on every result before storage: anything over `max_output_bytes` is truncated, marked `is_partial`, and stored as a snapshot; only a short extract goes to the model.

**Parameter schemas** ([Q9.41](conversation.md)): unknown fields refused; `end` after `start`; `limit` 1–500 (default 200); `level` one of error, warning, info; text filter up to 200 characters; `metric` one of `http_5xx_rate`, `http_latency_p95`, `http_request_rate`, `error_rate_by_version`; `action_kind` from the allowed list. `AlertQuery`: `service` and an optional `alert_id`. `ErrorGroupQuery`: the window fields, plus an optional `release` up to 100 characters. `KnowledgeQuery`: `service`, `query` up to 200 characters, `k` 1–10 (default 5).

**`ToolGate.check`, in order.** The model sees the refusal code, so it can correct the request.

| Check | Refusal code | Source |
| --- | --- | --- |
| Tool is registered; no reflection or dynamic dispatch | `unknown_tool` | [Q9.39](conversation.md) |
| Tool isn't WRITE | `write_forbidden` | [Q9.40](conversation.md) |
| Tool is on the calling skill's fixed list (9.2), so hypothesis and critic requests can only name READ tools | `tool_not_allowed` | [Q9.13](conversation.md), [Q9.34](conversation.md) |
| Parameters match the schema | `invalid_params` | [Q9.41](conversation.md) |
| `service` equals the incident's | `out_of_scope` | [Q9.42](conversation.md) |
| Window within the tool's maximum | `window_too_large` | [Q9.43](conversation.md) |
| Window not in the future, allowing 1 minute of clock difference. Applies to a point-in-time `at` as well | `window_in_future` | [Q9.41](conversation.md) |
| Window, or `at`, not more than 24 h before the incident | `window_too_old` | [Q9.41](conversation.md) |
| Total calls under 20 | `call_budget` | [Q9.47](conversation.md) |
| Calls of this tool under its budget | `tool_budget` | [Q9.47](conversation.md) |
| Identical call made fewer than 2 times | `repeated_call` | [Q9.47](conversation.md) |

**Restricting the model** ([Q9.39](conversation.md)): only named tools exist; the model is shown only the tools on its skill's list, never WRITE; adding a tool requires a code change and review.

**Service scope** ([Q9.42](conversation.md)): credentials are scoped per service, so even a gate bug can't read another service's logs. Dependency health follows the same rule: `catalog:read:{service}` reads the incident service's catalogue entry and the health of its declared dependencies, never those dependencies' logs (Artifact 5).

**Credentials** ([Q9.44](conversation.md)): per tool and per service; short-lived, issued per investigation; never in the model's context, evidence or the sandbox. PROPOSE credentials can create drafts only. WRITE credentials exist only in the dispatcher process.

**No path to production** ([Q9.45](conversation.md)): no shell tool in the registry; the worker has no production host credentials or network route; writes go only through the dispatcher's `apply_change`, which acts only on a signed approval for a specific patch; generated code runs only in the isolated sandbox.

**Prompt injection** ([Q9.46](conversation.md)): evidence enters only as labelled data, as short extracts; instruction-like text is flagged (`flagged_injection`) and shown to the critic and approvers; outputs are typed, with no field for "run this"; tool requests are gated; the coordinator decides the next step; writes need a person and the dispatcher. The injection test set runs against the agent, and zero unsafe actions is the pass mark.

**Repeated calls** ([Q9.47](conversation.md)): the canonical hash covers the tool name and validated, normalised parameters. The first repeat returns the stored evidence rather than querying again. A third identical call is refused.

## 9.5 Evidence schema

*Answers Q9.20–Q9.22, Q9.27–Q9.29.*

`Evidence` in [agent_contracts.py](files/agent_contracts.py). One collected fact, produced only by code (tools), never by the model.

| Field | Type | Meaning |
| --- | --- | --- |
| `evidence_id` | UUID | Identifier |
| `incident_id` | UUID | Owning incident |
| `kind` | `EvidenceKind` | See below |
| `source_system` | str | Source system |
| `tool` | str | Registry name of the tool that produced it; `test_runner` for `test_result` |
| `query` | Optional str | Exact, rerunnable |
| `window_start` | Optional datetime | When the data happened (from) |
| `window_end` | Optional datetime | When the data happened (to) |
| `collected_at` | datetime | When we read it |
| `source_as_of` | Optional datetime | The source's own version or last-modified time |
| `source_link` | Optional str | Link to the source. Required unless `kind` is `gap`; a record with no link is dropped and a gap recorded (Technical specification 6) |
| `summary` | str, max 4000 | Redacted extract given to the model |
| `content_sha256` | 64 hex characters | Fingerprint of the snapshot |
| `is_partial` | bool, default false | Result was truncated or partial |
| `flagged_injection` | bool, default false | Text looks like instructions |
| `gap_reason` | Optional str | Set only when `kind` is `gap`; the validator requires it for gaps and forbids it otherwise |
| `strength_tier` | Optional int, 1–8 | Set by code from computed facts (9.7, ADR-53). Null for `gap`, `runbook`, `past_incident`, and records no tier rule matches |

The stored record also keeps `collected_by`, `attributes` and `artifact_id`, which agents don't see (ADR-64). Field names follow the canonical [schema.sql](../layer-07-data-apis-events/files/schema.sql).

**`EvidenceKind`:** `alert`, `metric`, `log`, `trace`, `error_group`, `change`, `dependency`, `past_incident`, `runbook`, `test_result`, `gap`.

**Provenance** ([Q9.20](conversation.md)): facts enter only as evidence records made by tools. Every claim cites at least one evidence ID, and code checks every citation (`check_citations`): it exists, belongs to this incident, and isn't a gap. A claim with no valid evidence is an unsupported claim.

**Three clocks** ([Q9.21](conversation.md)): `window_start` and `window_end` (when the data happened), `collected_at` (when we read it), `source_as_of` (the source's own version). Two engineers' records of the same log both stay; neither overwrites the other.

**Changed sources** ([Q9.22](conversation.md)): before the critic's review and again before approval, `recheck_fingerprints` re-reads the source's fingerprint for every cited item. A change, or a source that can't be read, creates a blocking `stale_evidence` issue, and the diagnosis must be revised. The snapshot is kept for comparison.

**Missing evidence** ([Q9.27](conversation.md), ADR-54): missing sources are recorded as `gap` evidence with a reason, and collection continues. A gap supports nothing and appears in what the model and critic see.

| Missing source | Effect |
| --- | --- |
| Required: the alert, the change history, or both logs and the error tracker | The diagnosis can't be established. Confidence is low and the result is inconclusive, with the gap named |
| Optional: traces, dependency health, past incidents | Confidence drops from high to medium. Nothing blocks |

**Invalid references** ([Q9.28](conversation.md), [Q9.29](conversation.md)):

| Layer | Control |
| --- | --- |
| Code | `check_citations` looks up every cited ID before the draft is stored. An unknown ID, or one whose `incident_id` differs, raises `UnsupportedReference`, and the whole draft is rejected |
| Database | Composite foreign keys tie hypotheses, diagnoses and actions to the same incident (Artifact 7) |
| Prevention | The model is only ever shown this incident's evidence IDs |

## 9.6 Hypothesis schema

*Answers Q9.16, Q9.23–Q9.26.*

From [agent_contracts.py](files/agent_contracts.py).

**`Claim`:** one factual statement, with the evidence that supports it.

| Field | Type | Rule |
| --- | --- | --- |
| `text` | `Short` | 1–500 characters |
| `evidence_ids` | list of UUID | At least 1 |

**`HypothesisDraft`:**

| Field | Type | Rule |
| --- | --- | --- |
| `statement` | `Short` | 1–500 characters |
| `category` | Literal | `deployment_code`, `deployment_config`, `dependency`, `capacity`, `data`, `infrastructure`, `unknown` |
| `claims` | list of `Claim` | 1–8 |
| `contradicting_evidence_ids` | list of UUID | Default empty |
| `confirm_test` | `Short` | What would confirm it |
| `refute_test` | `Short` | What would rule it out |
| `evidence_needed` | list of `EvidenceRequest` (9.8) | Up to 5; READ tools only; requests the coordinator may run |

**`HypothesesOut`:** `hypotheses`, 2 to 5 `HypothesisDraft`. The validator refuses a set where every category starts with `deployment` ([Q9.16](conversation.md)).

**Why alternatives are required** ([Q9.23](conversation.md)): they stop early lock-in, point to the deciding evidence, make "ruled out" explicit, and make inconclusive honest.

**Supporting evidence** ([Q9.24](conversation.md)): tied at claim level, stored as `hypothesis_evidence` rows marked supports or contradicts (Artifact 7). The explanation shown to people is built from these claims, and strength is judged by code from the `strength_tier` of the observations cited (ADR-53).

**Contradicting evidence** ([Q9.25](conversation.md)): represented as `contradicting_evidence_ids` and `contradicts` links. One strong contradiction outweighs many weak supports. For the leading hypothesis, any contradicting observation other than a gap, retrieved guidance or tier 8 makes `code_checks` raise a blocking `contradicted` issue, and confidence drops to low. The diagnosis must then explain the contradiction with new evidence, change the leading hypothesis, or become inconclusive.

**Correlation versus causation** ([Q9.26](conversation.md)): timing and change records alone can't establish a cause. If the leading deployment hypothesis doesn't meet the Medium bar in 9.7, `code_checks` raises a blocking `correlation_only` issue and `score_confidence` returns low. A log line or a change record doesn't pass by its kind alone. Causation needs observations from the tier table: the failure reproduces with the change and not without it; only the new version fails; the changed value sits on the failing path; the stack trace points to the changed code. The baseline plan always includes dependency health, and a non-deployment hypothesis is required.

## 9.7 Diagnosis schema

*Answers Q9.17, Q9.25–Q9.29.*

`DiagnosisDraft` in [agent_contracts.py](files/agent_contracts.py):

| Field | Type | Rule |
| --- | --- | --- |
| `outcome` | Literal | `established` or `inconclusive` |
| `leading_index` | Optional int, 0–4 | Required if established, absent if inconclusive; must be in range |
| `hypotheses` | list of `HypothesisDraft` | 2 to 5 |
| `summary` | str | 1–4000 characters |
| `ruled_out` | list of `Short` | Why each alternative was set aside |
| `missing_evidence` | list of `Short` | Default empty |
| `proposed_action` | Optional Literal | `config_revert`, `commit_revert`, `release_rollback`; never from an inconclusive diagnosis |

The stored diagnosis adds fields the model never fills, all set by code: `confidence`, `stop_reason`, `reviewed`, `review_notes` and `produced_by`. Stored hypotheses and diagnoses also carry `category`, claims and `ruled_out` (ADR-64).

**Confidence** isn't a field the model fills. Code scores it (`score_confidence` in [validation.py](files/validation.py)), and the critic can only lower it ([Q9.17](conversation.md)). The levels are `low`, `medium`, `high`.

Strength belongs to the observation, not the evidence kind. Code assigns `strength_tier` when it computes facts (ADR-53). The "Assigned when" rules are provisional.

| Tier | Observation | Class | Assigned when |
| --- | --- | --- | --- |
| 1 | Reproduction | Mechanism | A `test_result` fails with the change and clears without it. It comes from the sandbox, so normally after the diagnosis |
| 2 | Version split | Mechanism | `error_rate_by_version` shows only instances on the new version failing |
| 3 | Config match | Mechanism | A key from `get_changes` is read on the failing path, per a log or trace |
| 4 | Code match | Supporting | Trace or error-group frames point at a file in the change |
| 5 | New error signature | Supporting | An error group is first seen after the change |
| 6 | Scale match with the rollout | Supporting | Failures grow with the rollout's share |
| 7 | Rollback clears it | Supporting | Change history shows a rollback, and `http_5xx_rate` recovers |
| 8 | Timing alone | Weak | A record links to the change by time only |

| Level | Requires | Result |
| --- | --- | --- |
| High | Tier 1–3 cited, no unresolved contradicting evidence, no gap | Established |
| Medium | Tier 4, or two independent tier 5–7 observations, no unresolved contradiction; or High lowered by an optional gap | Established |
| Low | Anything else, a missing required source, or a critic cap of `low` | Inconclusive, no patch |

- **The threshold is medium** (provisional, ADR-53). The SRE lead recalibrates it on the frozen suite before Release A.
- **Independent** means from different `source_system` values (provisional).
- **Final confidence** is the lower of the scored level and the critic's `confidence_cap`.
- `validation.STRENGTH` is retired. `context.EVIDENCE_PRIORITY` stays under a new name, as it orders context for reading (ADR-53).

**Code checks on a draft** ([validation.py](files/validation.py)):

- `check_citations` returns a `CitationReport`: `claims`, `supported`, `unknown_ids`, `foreign_ids`, and `supported_ratio`.
- `fingerprint` hashes the outcome, leading statement, cited evidence and action, so rewording isn't a revision.
- A claim is supported only if it cites at least one record that isn't a gap, isn't flagged and isn't retrieved guidance. So a claim citing only gaps ([Q9.27](conversation.md)), only flagged evidence (ADR-59) or only runbooks and past incidents (Artifact 10) is unsupported. If the skill's output has an invented reference, it retries with the error explained, up to its attempt limit; after that the step fails and the investigation is inconclusive. Nothing with an invented reference is ever stored or shown ([Q9.28](conversation.md)).
- A secret in model output fails validation and is retried, and is masked first if stored for audit. Commands in model output are rejected. Links are never fetched (ADR-59). Flagged items are shown to the approver.

## 9.8 Critic schema

*Answers Q9.30–Q9.34, Q9.36, Q9.37.*

The Critic has two layers ([Q9.30](conversation.md)):

1. **Code checks** (`code_checks`), which can't be overruled: unsupported claims, correlation only, strong contradiction, alternatives not ruled out, stale evidence, scope.
2. **An independent model critic:** a separate instruction set and call. It sees only the evidence and the diagnosis, never the diagnoser's reasoning, and may use a different model where available.

`combine()` merges the two. A blocking code issue turns the model's "accept" into "revise", and it applies `confidence_cap` to the scored confidence, giving the final confidence (9.7).

**`CriticVerdict`** in [agent_contracts.py](files/agent_contracts.py):

| Field | Type | Rule |
| --- | --- | --- |
| `verdict` | Literal | `accept`, `revise`, `escalate` |
| `issues` | list of `CriticIssue` | Up to 20; default empty |
| `evidence_requests` | list of `EvidenceRequest` | Up to 3; default empty |
| `confidence_cap` | `Confidence` | The highest confidence the critic will allow |

The validator refuses `accept` with a blocking issue, and `revise` with no issues and no evidence requests ([Q9.32](conversation.md)).

| Verdict | Meaning |
| --- | --- |
| accept | No blocking issues |
| revise | Fixable, with listed issues or evidence requests |
| escalate | Can't be resolved by the platform (conflicting strong evidence, out of scope) |

**`CriticIssue`:**

| Field | Type | Rule |
| --- | --- | --- |
| `kind` | `IssueKind` | See the next table |
| `detail` | `Short` | 1–500 characters |
| `claim_index` | Optional int | Which claim |
| `evidence_ids` | list of UUID | Default empty |
| `blocking` | bool | Required |

**`EvidenceRequest`:** a bounded, typed request, used by both the hypothesis skill and the critic. The coordinator decides whether to run it.

| Field | Type | Rule |
| --- | --- | --- |
| `tool` | str | A registered READ tool; the ToolGate refuses anything else (`tool_not_allowed`) |
| `reason` | `Short` | 1–500 characters |
| `params` | `dict[str, str \| int]` | Validated against the tool's schema |

**Review criteria** ([Q9.31](conversation.md)). Accept needs `supported_ratio` of 1.0 across the whole draft: `code_checks` raises a blocking `unsupported_claim` for each unsupported claim, in the leading hypothesis and the alternatives. So with two of five claims supported (a support ratio of 0.4), the verdict can't be accept.

| Criterion | Issue kind | Blocking |
| --- | --- | --- |
| Each claim cites evidence that actually says it | `unsupported_claim` | Yes |
| No strong evidence contradicts the leading hypothesis | `contradicted` | Yes |
| Every alternative has a reason it was set aside | `alternative_not_ruled_out` | Yes |
| The causal link is more than timing | `correlation_only` | Yes |
| A decisive check was collected (for example, a version split) | `missing_evidence` | Yes, if decisive |
| Cited evidence hasn't changed | `stale_evidence` | Yes |
| The action is allowed, and within the incident's service. Code raises this if an action is proposed for a non-deployment leading hypothesis, or one that cites no `get_changes` evidence. The ToolGate then checks `service` on `draft_patch` | `scope` | Yes |
| Confidence matches the evidence | `overconfident` | Lowers the cap |

**After a contradiction** ([Q9.33](conversation.md)): see 9.9.

**Critic evidence requests** ([Q9.34](conversation.md)): yes, as a request, not a command. Up to 3 typed requests per verdict, naming registered READ tools only. The coordinator decides whether to run them, and each runs through the ToolGate. The critic can't call tools itself, so a compromised or confused critic can't widen access.

**Why agreement isn't proof** ([Q9.36](conversation.md)): models given the same evidence make the same errors, and correlated models share blind spots. Agreement measures consistency, not correctness. The critic's value comes from checking claims against evidence, demanding mechanism evidence, and re-checking that evidence is current. Ground truth comes from tests and verification.

**Invalid critic output** ([Q9.37](conversation.md)):

1. Reject it whole: nothing partial is used.
2. Retry inside the gateway with the validation error, within the 3-attempt limit.
3. Still invalid: the activity returns `verdict = None`, the coordinator records `invalid`, and the critique runs once more.
4. Invalid twice: fail closed. The result is inconclusive ("critic output invalid twice; failing closed"). A critic therefore can't exceed 6 attempts (ADR-52).

An unreviewed diagnosis is never treated as accepted.

## 9.9 Revision policy

*Answers Q9.33, Q9.35.*

The policy lives in `coordinator.next_step` ([coordinator.py](files/coordinator.py)) and applies when the last verdict was `revise`. The revision checks run before pending evidence requests, so a third round is never wasted on a diagnosis that can't be revised again (ADR-52).

| Order | Rule | Stop reason | Tested |
| --- | --- | --- | --- |
| 1 | At most 2 revisions: the diagnosis runs at most 3 times | `revision limit (2) reached` | Yes |
| 2 | A revision repeats the same diagnosis (same fingerprint) | `revision repeated the same diagnosis` | Yes |
| 3 | Support ratio didn't improve, and no new evidence arrived since the previous diagnosis | `revision did not improve support and no new evidence` | To add (C18) |

If none applies, pending requests run as a targeted round, then the coordinator runs the next `DIAGNOSE`. With none pending it goes straight to `DIAGNOSE`.

One more rule applies at any point in the loop: a targeted round that adds nothing new stops it, whether or not a critic has run (`targeted round added nothing new`). Gap records don't count as new. All time, call, token and spend budgets apply throughout ([Q9.35](conversation.md)).

**Worked path: the critic rejects because a metric contradicts the diagnosis** ([Q9.33](conversation.md)):

1. The verdict is revise, with a blocking `contradicted` issue citing the metric.
2. The coordinator checks the revision policy: revisions left, and whether the last one improved.
3. If allowed, it runs the critic's evidence requests through the ToolGate.
4. The diagnoser revises with the new evidence. It must explain the contradiction, switch the leading hypothesis, or return inconclusive.
5. The critic reviews again.
6. If the contradiction remains after the revision limit, the result is inconclusive, and is handed over with both sides shown.

## 9.10 Loop termination policy

*Answers Q9.34, Q9.37, Q9.38.*

The loop stops at the first of these conditions, and every stop records its reason in `stop_reason` ([Q9.38](conversation.md)):

| Order | Condition | Step | Incident path |
| --- | --- | --- | --- |
| 1 | A sign of a security or data risk (ADR-54) | `FINISH_INCONCLUSIVE` | Direct to `handed_over` |
| 2 | The critic accepts, final confidence medium or high | `FINISH_ESTABLISHED` | To `diagnosed` |
| 2 | The critic accepts, final confidence low | `FINISH_INCONCLUSIVE` | Through `inconclusive` |
| 3 | 15-minute investigation limit | `FINISH_INCONCLUSIVE` | Through `inconclusive` |
| 4 | 30 model calls | `FINISH_INCONCLUSIVE` | Through `inconclusive` |
| 5 | 200,000 input or 20,000 output tokens | `FINISH_INCONCLUSIVE` | Through `inconclusive` |
| 6 | $2.00 per incident, or the daily limit reached (ADR-55, ADR-56) | `FINISH_INCONCLUSIVE` | Through `inconclusive` |
| 7 | A skill fails after its retries, including a provider outage | `FINISH_INCONCLUSIVE` | Through `inconclusive` |
| 8 | The critic escalates | `FINISH_INCONCLUSIVE` | Through `inconclusive` |
| 9 | The critic is invalid twice | `FINISH_INCONCLUSIVE` | Through `inconclusive` |
| 10 | A targeted round adds nothing new | `FINISH_INCONCLUSIVE` | Through `inconclusive` |
| 11 | The diagnoser returns inconclusive | `FINISH_INCONCLUSIVE` | Through `inconclusive` |
| 12 | 2 revisions used | `FINISH_INCONCLUSIVE` | Through `inconclusive` |
| 13 | A revision repeats the same diagnosis | `FINISH_INCONCLUSIVE` | Through `inconclusive` |
| 14 | A revision doesn't improve support, with no new evidence | `FINISH_INCONCLUSIVE` | Through `inconclusive` |

"Direct" is the `investigating` → `handed_over` transition, for a security or data-risk stop only (ADR-46). "Through `inconclusive`" is `investigating` → `inconclusive` → `handed_over`, so the record says "Root cause not established". A `budget_refused` or `deadline_passed` failure goes through `inconclusive`; only `security_risk` takes the direct path.

A new investigation started after the daily limit is reached is evidence-only: it runs `COLLECT_BASELINE` and `RETRIEVE_KNOWLEDGE`, then stops through `inconclusive` (ADR-46, ADR-55). A running one finishes its current step, then stops.

Pending requests aren't run once 3 targeted rounds or 20 tool calls are used ([Q9.34](conversation.md), [Q9.38](conversation.md)). A round runs at most 3 requests. From hypotheses they are taken one per hypothesis in turn, so alternatives get a test, and the rest are logged in `decisions` as not run.

**`Limits` in [coordinator.py](files/coordinator.py)** is the one configuration object (ADR-52). The workflow, the gateway's budget defaults and the ToolGate read from it. All values are provisional.

| Limit | Value | Enforced by |
| --- | --- | --- |
| `investigation` | 15 minutes | Coordinator, workflow deadline, gateway `deadline_epoch_s` |
| `max_model_calls` | 30 | Coordinator, gateway |
| `max_input_tokens` | 200,000 | Coordinator, gateway |
| `max_output_tokens` | 20,000 | Coordinator, gateway |
| `max_cost_micros` | 2,000,000 ($2.00, ADR-56) | Coordinator, gateway |
| `max_tool_calls` | 20 | Coordinator, ToolGate |
| `max_revisions` | 2 | Coordinator |
| `max_targeted_rounds` | 3 | Coordinator |
| `max_requests_per_round` | 3 | Coordinator |
| `max_critic_failures` | 2 | Coordinator |
| `max_attempts_per_step` | 3 | Gateway |

The gateway refuses a call before it is made, and the coordinator stops the loop between steps, so a cap holds even if one of them is wrong. The per-call limit of 16,000 input tokens stays in the gateway (ADR-61).

Model calls count gateway attempts (Q10.34), and a reused result counts 0. The loop's worst case is 24: hypothesise 3, diagnose 9, critique 12. The Patch Proposer draws on the same incident budget, and the gateway refuses it at 30.

## 9.11 Agent evaluation metrics

*Answers Q9.28, Q9.47–Q9.53.*

| Area | Metric | Definition | Target |
| --- | --- | --- | --- |
| Accuracy ([Q9.48](conversation.md)) | Top-1 accuracy | Leading cause matches the label, change-caused cases only | ≥ 80%, and no more than 2 points below baseline (ADR-65) |
| | Top-3 | True cause is among the hypotheses | Provisional: ≥ 90% |
| | Correct inconclusive | Cases with evidence withheld are marked inconclusive | 100% (ADR-65) |
| | False established | An established diagnosis that is wrong; the most costly, tracked separately | ≤ 2% (ADR-65) |
| Coverage ([Q9.49](conversation.md)) | Evidence coverage | Required sources collected ÷ required sources (ADR-54); gaps count as not collected | Provisional: 100% before any established diagnosis. A gap in a required source means inconclusive |
| | Decisive-evidence coverage | Whether the evidence separating the leading hypothesis from the runner-up was collected | Provisional: ≥ 95% of established diagnoses |
| Claims ([Q9.50](conversation.md), [Q9.28](conversation.md)) | Unsupported-claim rate | Claims without valid supporting evidence ÷ all claims, from `CitationReport` | Provisional: 0 in accepted diagnoses |
| | Invented-reference rate | Drafts rejected for unknown or foreign IDs ÷ all drafts | Provisional: below 2% of drafts |
| | Semantic support | Evidence actually says what the claim says; checked by the critic, sampled by people | 100% of citations exist; ≥ 99% support their claim (ADR-65). 10% of diagnoses sampled in the pilot |
| Escalation ([Q9.51](conversation.md)) | Over-escalation rate | Handed over, but evidence was sufficient and the label clear | Reduce, after under-escalation |
| | Under-escalation rate | Established, but wrong or unsupported; weighted far more heavily | Below 2% (ADR-65) |
| | Escalation precision and recall | Hand-over treated as a classification | Provisional: recall ≥ 98%, matching under-escalation below 2%; precision ≥ 80% |
| Critic ([Q9.52](conversation.md)) | Catch rate | Seeded faults flagged ÷ seeded faults, per issue kind | Provisional: ≥ 95% code-checked kinds, ≥ 80% semantic kinds |
| | False-rejection rate | Correct diagnoses the critic sends back | Provisional: ≤ 10% |
| | Value added | Accuracy of accepted diagnoses compared with all drafts | Provisional: at least 3 points above all drafts |
| | Agreement with human reviewers | On a sample | Provisional: ≥ 80% on the 10% sample |
| Efficiency ([Q9.53](conversation.md)) | Per-incident usage | Model calls, tool calls, tokens, cost, time to first summary and to diagnosis, revisions, targeted rounds | Provisional, reference case: ≤ 6 model calls, ≤ 12 tool calls, ≤ 96,000 input tokens, ≤ $2.00, ≤ 1 revision, ≤ 1 targeted round. First summary p95 ≤ 300 s and diagnosis ≤ 900 s (ADR-65) |
| | Redundant-call rate ([Q9.47](conversation.md)) | Repeated calls ÷ all calls | Provisional: ≤ 5%; 0 in the reference case |
| | Cost per correct diagnosis | Total cost ÷ correct diagnoses | Provisional: ≤ 1.2× baseline, matching the cost gate in ADR-65 |

**How they are measured:**

- **Accuracy** runs against labelled incidents: synthetic cases with a known cause, then sanitised replays, labelled by the engineers who resolved them. In the pilot, compare against the cause in the incident review, and check whether the verified fix removed the symptom. Break results down by incident category.
- **Required evidence** for change-caused failures is the alert, the change history, and at least one of logs or the error tracker (ADR-54). Impact metrics, error rate by version, traces and dependency health stay in the baseline plan, and their absence doesn't block. Other categories are recognised and handed over, so they have no required list. Their cases are scored on the `required_evidence_kinds` in each answer key (Artifact 12).
- **Hand-over reasons** are recorded in `stop_reason`, so over-escalation can be traced to a specific rule.
- **Critic effectiveness** uses seeded errors: an unsupported claim, a foreign citation, a correlation-only cause, a hidden contradiction, stale evidence. A critic that rarely objects may simply be accepting everything; seeded errors show the difference.
- **100 model calls** is impossible by design: the cap is 30, and the reference case is expected to use 3–6.
- **Per-skill results** come from the evaluation sets in 9.2.

**The reference case** is the synthetic incident at [layer-03-sre-fundamentals/files/reference-incident/](../layer-03-sre-fundamentals/files/reference-incident/), part of the `config_change` category. Its targets are limits, not measurements.

| Reference-case budget | Model calls | Tool calls |
| --- | --- | --- |
| Accepted first time | 4: hypothesise, diagnose, critique, patch proposal | 10: 8 baseline, 2 knowledge |
| One revision | 6: adds diagnose and critique | 12: adds 2 targeted calls |

The 96,000 input tokens is 6 calls at the 16,000 per-call limit.

**Needs measurement:** baseline values for the baseline-relative targets (top-1 margin, cost ratios) and the first results for every provisional target, Owner: SRE lead, settled when: the frozen suite is built from the specification in Artifact 12 and run once.

## Assumptions

Layer 9's answers have no Assumptions blocks, although the conversation log refers to them. This table lists the values the sources mark provisional, and the values added on 2026-11-02. Owners are roles from [decisions.md](../../decisions.md).

| Assumption | Status | Owner | Validated when |
| --- | --- | --- | --- |
| 15 minutes is enough for an investigation of a change-caused failure | Provisional | SRE lead | Test-suite investigations finish within it (Layer 4) |
| Change-caused investigations need fewer than 30 model calls | Provisional | SRE lead | Measured on the test suite (Layer 4) |
| 20 tool calls per incident | Provisional | SRE lead | Measured on the frozen suite: no correct diagnosis is lost to `call_budget` |
| 2 revisions per diagnosis | Provisional | SRE lead | Measured on the frozen suite: a third revision rarely changes the result |
| 3 targeted rounds, 3 requests per round | Provisional | SRE lead | Measured on the frozen suite: requests left unrun don't lower accuracy |
| Default evidence window: 60 minutes before the start, up to now | Provisional | SRE lead | Measured on the frozen suite: cases' decisive evidence falls inside it |
| Top-1 accuracy ≥ 80% | Provisional | SRE lead | Measured on replays before the pilot (Technical specification, section 13) |
| Withheld-evidence cases marked inconclusive: 100% | Provisional | SRE lead | Measured on the held-out split (ADR-65) |
| Evidence coverage: 100%, or a recorded gap, before any established diagnosis ([Q9.49](conversation.md)) | Provisional | SRE lead | Measured on the frozen suite |
| 0 unsupported claims in accepted diagnoses; invented references below 2% of drafts ([Q9.50](conversation.md)) | Provisional | SRE lead | Measured on the frozen suite |
| Under-escalation below 2% ([Q9.51](conversation.md)) | Provisional | SRE lead | Measured on the held-out split (ADR-65) |
| Critic catch rate ≥ 95% for code-checked kinds, ≥ 80% for semantic kinds ([Q9.52](conversation.md)) | Provisional | SRE lead | Measured on `eval-critic` |
| Reference case: ≤ 6 model calls, ≤ 12 tool calls, ≤ 96,000 input tokens, first summary ≤ 5 minutes ([Q9.53](conversation.md)) | Provisional | SRE lead | Measured on the synthetic reference incident, then the `config_change` development cases |
| Skill timeouts: Hypothesis Generator, Critic and Patch Proposer 300 s | Provisional | SRE lead | No step times out on the frozen suite with the gateway's 3 attempts |
| Added tool limits: `get_alert`, `get_error_groups`, `search_runbooks`, `search_past_incidents` (9.4) | Provisional | Platform owner | Checked against real adapters at M1, with no routine truncation |
| Credential scopes for the added tools, and `catalog:read:{service}` | Provisional | Security lead | Scopes issued and tested against each source system |
| Confidence threshold is medium; tier assignment rules; independent means different `source_system` (9.7) | Provisional | SRE lead | Recalibrated on the frozen suite before Release A (ADR-53) |
| Per-incident cap $2.00 (ADR-56) | Provisional | Engineering manager | Finance confirms before the pilot |
| Per-skill pass marks and set membership (9.2) | Provisional | SRE lead | Recorded in the suite manifest, then met on the development split |
| Top-3 ≥ 90%; decisive-evidence coverage ≥ 95%; escalation recall ≥ 98%, precision ≥ 80% | Provisional | SRE lead | First baseline run of the frozen suite shows they are reachable |
| False rejection ≤ 10%; value added ≥ 3 points; human agreement ≥ 80%; redundant calls ≤ 5%; cost per correct diagnosis ≤ 1.2× baseline | Provisional | SRE lead | First baseline run, then the pilot sample |

## Decisions and conflicts

The sections above state the resolved position. [test_agent.py](files/test_agent.py) was run on a scratch copy outside the repo before these decisions: 25 passed. C7 and C8 were confirmed by driving `next_step` with scripted results.

Code in `files/` is kept as the team's record. Every change it needs is listed in `docs/code-changes-required.md`.

| Item | Resolution | Decided by |
| --- | --- | --- |
| Decisions beyond the sections above | None in the answers. Layer decisions are in the rows below | Layer decision (proposed) |
| Limit: investigation time | 15 minutes everywhere. `INVESTIGATION_LIMIT` now reads `Limits` | ADR-52 |
| Limit: model calls | 30 everywhere. The gateway's `max_calls` now reads `Limits` | ADR-52 |
| Limit: tool calls | 20 (provisional), one value in `Limits`, read by `ToolGate`. The specification's silence stands | ADR-52 |
| Limit: revisions | 2 revisions, 3 diagnosis runs (provisional) | ADR-52 |
| Limit: targeted rounds | 3 (provisional) | ADR-52 |
| Limit: requests per round | 3 per round. Hypothesis requests are taken one per hypothesis in turn; the rest are logged as not run | Layer decision (proposed) |
| Limit: attempts per step | 3 gateway attempts; `max_attempts_per_step` in `Limits` | ADR-27, ADR-52 |
| Limit: invalid critic output | 3 attempts per step, 2 failed runs end it, 6 attempts at most. The specification's "3 times" is the per-step figure | ADR-52 |
| Limit: tokens and spend | Added to the termination policy and `Limits`: 200,000 input, 20,000 output, $2.00 | ADR-52, ADR-56 |
| Limit: `Limits` source | `Limits` is the one source, with its own provisional values. The docstring changes | ADR-52 |
| Limit: model-call counting | Gateway attempts count, from `SkillUsage.model_calls`; a reused result counts 0 | ADR-61, Layer decision (proposed) |
| C1 Pipeline shape | The coordinator loop runs inside `investigating`, with `RETRIEVE_KNOWLEDGE` added | ADR-52 |
| C2 Where the model is called | Hypothesise, diagnose, critique and patch proposal, through the gateway. Specification section 5 is amended | ADR-52 |
| C3 Model output shape | `DiagnosisDraft` replaces `ModelDiagnosisOutput` | ADR-52 |
| C4 Fields versus Artifact 7 | Storage adds `tool`, `source_as_of`, `gap_reason`, `strength_tier`, and keeps `collected_by`, `attributes`, `artifact_id`. `source_link` is required unless `kind` is `gap` | ADR-64, Layer decision (proposed) for `source_link` |
| C5 Missing tools | Added `get_alert`, `get_error_groups`, `search_runbooks`, `search_past_incidents` (9.4) | Layer decision (proposed) |
| C6 Evidence strength | Strength attaches to the observation, by `strength_tier`. `correlation_only` uses the Medium bar | ADR-53 |
| C7 Order of revision checks | Revision checks run before pending requests | ADR-52 |
| C8 "A targeted round adds nothing new" | Stops the loop at any point. Gaps don't count as new. Repeats are dropped first, so a repeat can't trigger it | ADR-52, Layer decision (proposed) for counting |
| C9 `evidence_needed` is free text | Becomes `list[EvidenceRequest]`, up to 5 | Layer decision (proposed) |
| C10 READ tools only | Enforced in the ToolGate by each skill's fixed list (`tool_not_allowed`), for hypothesis and critic requests alike | Layer decision (proposed) |
| C11 Skill failure stop | `SkillFailure` reaches the coordinator, which stops as inconclusive. A source failure stays a gap | ADR-52 |
| C12 Gap citations | A claim is unsupported if it has no valid support, not if any one citation is a gap. `check_citations` follows `code_checks`. Flagged-only and guidance-only claims are unsupported too | Layer decision (proposed), ADR-59 |
| C13 Support ratio isn't a gate | Accept needs `supported_ratio` of 1.0 across the whole draft; each unsupported claim is a blocking code issue | ADR-53, Layer decision (proposed) |
| C14 Scope check | Code raises `scope` for an action on a non-deployment cause or with no cited change. The draft has no target, so the ToolGate checks `service` on `draft_patch`, and `code_checks` drops its unused `service` argument | Layer decision (proposed) |
| C15 Confidence cap and threshold | `combine()` applies the cap, and the coordinator reads final confidence. Accept with low ends inconclusive | ADR-53 |
| C16 Credential scope | `get_dependency_health` uses `catalog:read:{service}` | Layer decision (proposed) |
| C17 Gate details | `ToolGate.cap_output` enforces `max_output_bytes` after each call. The 1-minute allowance stays, as clock difference. Adapters are M1 work | Layer decision (proposed) |
| C18 "Tested" claims without a test | Tests added for `call_budget`, `revise` with nothing to fix, blocking `stale_evidence`, and the support-ratio stop, plus each change above | Layer decision (proposed) |
| Step name | `HYPOTHESISE`, matching ADR-52, `coordinator.py` and the gateway task `hypothesise` | ADR-52 |
| Crash recovery versus one attempt | Model activities return failures as values and allow one Temporal re-run, for a lost worker only. No model failure is retried above the gateway | ADR-27, ADR-52, Layer decision (proposed) |
| Time and budget stops versus "inconclusive" | The step stays `FINISH_INCONCLUSIVE`. Time, budget and invalid-output limits go through `inconclusive`. Only a security or data-risk stop goes direct to `handed_over` | ADR-46 |
| Critic and patch timeouts | The original 60 s and 120 s can't hold 3 model attempts, so both become 300 s, Layer 8's value (provisional) | Layer decision (proposed) |
| `draft_patch` calls per incident | 3, not 2, to match Artifact 11's 3 candidates | Layer decision (proposed) |
| First summary | Sent after the first `HYPOTHESISE` result, marked not yet reviewed | Layer decision (proposed) |
| `confidence_cap` | Final confidence is the lower of scored and cap. Low applies when the cap is `low`, since every verdict carries a cap | ADR-53, Layer decision (proposed) for the reading |
| Required evidence list | ADR-54's list replaces Q9.49's six kinds for blocking and coverage | ADR-54 |
| Knowledge evidence kinds | `search_runbooks` gives `runbook`, `search_past_incidents` gives `past_incident`. Artifact 10 uses `runbook` for both | Layer decision (proposed) |

**Needs the team:** whether any Claude and ChatGPT disagreement affected a decision in this layer, Owner: Team, settled when: the conversation logs are filled in.

**Needs the team:** whether the amended behaviour works in code, Owner: Platform owner, settled when: the changes in `docs/code-changes-required.md` are made and tested.

## Acceptance checks

- [ ] Every required section above is present and complete.
- [x] Every section lists the questions it answers, and every question in this layer is answered by at least one section.
- [x] Every assumption is marked provisional and has a named owner and a condition under which it will be validated.
- [ ] Every disagreement between Claude and ChatGPT that affected a decision is recorded with the team's resolution.
- [ ] The artifact is consistent with every artifact it is built from; any conflict is recorded as an explicit decision.
- [x] The artifact was produced with the team's coding agent and is saved in the team repository.

Reviewed by: TBD
Date: TBD
