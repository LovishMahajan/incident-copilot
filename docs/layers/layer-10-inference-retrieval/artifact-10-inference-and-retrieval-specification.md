# Artifact 10: Inference and Retrieval Specification

- **Layer:** 10, Inference Engineering and Knowledge Retrieval
- **Built from:** Artifacts 2 and 9
- **Status:** Draft compiled from the team's answers; pending team review
- **Sources:** [conversation.md](conversation.md), [files/gateway_contracts.py](files/gateway_contracts.py), [files/adapters.py](files/adapters.py), [files/gateway.py](files/gateway.py), [files/retrieval.py](files/retrieval.py), [files/context.py](files/context.py), [files/test_layer10.py](files/test_layer10.py), [System Design](../../reference/system-design.md), [Technical Specification](../../reference/technical-specification.md), Layer [4](../layer-04-requirements/conversation.md), [6](../layer-06-system-architecture/conversation.md), [7](../layer-07-data-apis-events/conversation.md) (and [schema-tables.sql](../layer-07-data-apis-events/files/schema-tables.sql)), [8](../layer-08-workflows-state-machines/conversation.md) and [12](../layer-12-reliability-evaluation/conversation.md) conversations

## Required sections

| Required section | Questions this section answers | Done |
| --- | --- | --- |
| 10.1 Provider capability matrix | Q10.1–Q10.8, Q10.24 | Partial |
| 10.2 Model gateway contracts | Q10.9–Q10.16, Q10.18, Q10.22, Q10.23 | Yes |
| 10.3 Claude adapter | Q10.11, Q10.12, Q10.15, Q10.17–Q10.21, Q10.38 | Yes |
| 10.4 Tinker adapter | Q10.2, Q10.5, Q10.22 | Yes |
| 10.5 Budget policy | Q10.27–Q10.29, Q10.36 | Partial |
| 10.6 Concurrency policy | Q10.30, Q10.31 | Yes |
| 10.7 Retry and timeout policy | Q10.17–Q10.20, Q10.32, Q10.33, Q10.37–Q10.39 | Yes |
| 10.8 Token accounting | Q10.25, Q10.26, Q10.34–Q10.36 | Yes |
| 10.9 BM25 ingestion and search | Q10.40–Q10.42, Q10.45–Q10.50, Q10.52–Q10.56 | Partial |
| 10.10 Chunking strategy | Q10.43, Q10.44 | Yes |
| 10.11 Context assembly | Q10.57–Q10.63 | Yes |
| 10.12 Prompt versioning | Q10.23, Q10.64 | Partial |
| 10.13 Retrieval evaluation | Q10.42, Q10.51 | Partial |

Sections 10.1–10.8 cover running models reliably. Sections 10.9–10.13 cover supplying them with the right information. The two designs are separate: `retrieval.py` calls no model, and retrieved passages reach a model only as context items.

## 10.1 Provider capability matrix

*Answers Q10.1–Q10.8, Q10.24.*

**The thing being provided** is a large language model: a transformer that predicts the next token from all the tokens before it, repeated until a stop condition ([Q10.1](conversation.md)). The same model can be delivered by different providers, so the platform names the model and the provider separately ([Q10.2](conversation.md)).

**Matrix.** The attributes are those Q10.24 requires. Values come from [Q10.5](conversation.md) and the capabilities each adapter declares in [adapters.py](files/adapters.py). "Not stated" means no source gives the value.

| Attribute | Claude | Tinker |
| --- | --- | --- |
| What it is | Hosted frontier models, used as-is | Fine-tuning service for open-weight models, plus sampling from your checkpoints |
| Our job | Prompts, context, validation | Training data, a training loop, choosing checkpoints, then everything Claude needs too |
| Identity | Pinned model ID | Base model + checkpoint path |
| Structured output | Supported | Unverified for us until our conformance suite passes |
| Strict tool use | Supported | Unverified for us until our conformance suite passes |
| Tool use | Declared by adapter | Not declared |
| Streaming | Declared by adapter | Not declared |
| Usage reporting | Declared by adapter | Declared by adapter (conflict C13) |
| Pinned versions | Declared by adapter | Declared by adapter (conflict C13) |
| Deprecation notice | Not stated | Not stated |
| Context window | Not stated | Not stated |
| Maximum output | Not stated | Not stated |
| Rate limits | Not stated; 429 is handled (10.7) | Not stated |
| Data retention and training terms | Not confirmed (below) | Not stated |
| Region | Not stated | Not stated |
| Price per input and output token | Not stated; set in configuration | Not stated |
| Caching | Cache read and write usage are counted (10.8) | Not stated |
| Availability record | Not stated | Not stated |
| Maturity for serving | Generally available | Beta (as of October 2026) |

**Gap:** the sources name these matrix attributes but give no values for deprecation notice, context window, maximum output, rate limits, region, price or availability record. Data retention is an open unknown: "What does the provider agreement say about data retention?", owner Security lead, settled when terms are confirmed ([System Design](../../reference/system-design.md), section 10). The `PRICE` values in `test_layer10.py` are test fixtures, not source prices.

**Comparing providers.** Fill in the matrix, then run the same evaluation suite through the same gateway: accuracy, false-established rate, schema validity, refusal rate on incident data, latency at p95, and cost per correct diagnosis ([Q10.24](conversation.md)).

**Delivery approach.** The first version uses managed inference through a provider's API: Claude through the Anthropic API, over HTTPS, paying per token ([Q10.3](conversation.md)). Artifact 2: no GPUs and no custom model in the first version.

|  | External provider | Self-hosted |
| --- | --- | --- |
| Hardware | Theirs | Ours: GPUs bought or rented |
| Models | Their hosted models | Open-weight models only |
| Capacity planning, scaling, failover | Theirs | Ours |
| Serving software and upgrades | Theirs | Ours |
| Data location | Leaves our network (under contract) | Stays inside |
| Cost | Per token; nothing when idle | Fixed GPU cost, even when idle |
| Model quality | Frontier models | Usually below frontier for reasoning tasks |
| On-call | Theirs for serving | Ours |

([Q10.4](conversation.md))

| Moves to the provider ([Q10.6](conversation.md)) | Kept by us, whichever provider is used |
| --- | --- |
| GPU procurement and capacity | Prompts and context |
| Serving software, batching and memory management | Output validation |
| Scaling and load balancing | Retries and timeouts |
| Hardware failures and failover | Budgets and concurrency |
| Model hosting and updates | What data we send (redaction) |
| Security patching of the serving stack | The provider agreement (data retention, training use) |
| Availability of the API | Evaluation; what happens when the provider is down (Artifact 2: evidence only) |

**Own GPU cluster.** Justified only when one or more of these is true and measured ([Q10.7](conversation.md)):

- Data must not leave the network: a regulatory or contractual rule no provider agreement satisfies.
- Sustained high volume: GPU cost per month falls below the API bill at real, steady utilisation.
- A fine-tuned open model clearly beats hosted models on our evaluation, and must be served at scale.
- Latency or availability needs a provider can't meet.
- No acceptable provider in the required region.

At current volume (a few incidents a day, 3–6 calls each) none hold. If one ever did, the usual serving choice is vLLM (PagedAttention, continuous batching, an OpenAI-compatible API); alternatives are Hugging Face Text Generation Inference, and NVIDIA TensorRT-LLM with Triton. None of these is in the first version ([Q10.8](conversation.md)).

## 10.2 Model gateway contracts

*Answers Q10.9–Q10.16, Q10.18, Q10.22, Q10.23.*

Code: [files/gateway_contracts.py](files/gateway_contracts.py).

**Rule:** anything that affects correctness, cost or safety stays in the application, so changing providers doesn't change behaviour ([Q10.9](conversation.md)).

| Provider | Application (Model Gateway and above) |
| --- | --- |
| Running the model; tokenisation; sampling | One request and result contract for every provider |
| Its own rate limits and quotas | Budgets, reservations, per-incident and global concurrency |
| Reporting usage | Token accounting; handling missing usage |
| Constrained decoding, where supported | Schema validation of every output, always |
| Its availability | Timeouts, retry policy, deadlines, provider-outage behaviour |
| Refusing unsafe requests | Interpreting refusals; never retrying them blindly |
| — | Prompts, context, versions, identity, evaluation, redaction |

**One interface.** The Model Gateway exposes one call, `call(ModelRequest) → ModelResult`. Skills never see a provider format, and never name a provider or model: they ask for a model alias (`default`), which configuration resolves to one pinned provider and model ([Q10.2](conversation.md), [Q10.10](conversation.md)).

**ModelRequest** ([Q10.13](conversation.md)):

| Field | Purpose |
| --- | --- |
| `request_id` | Idempotency key: incident, step, attempt |
| `incident_id`, `task` | Accounting and limits |
| `prompt_id`, `prompt_version`, `system` | Versioned instructions (10.12) |
| `context` | Assembled items, each with a source reference (10.11) |
| `output_schema`, `output_schema_name` | Required answer shape, and the Pydantic contract to validate against |
| `tools` | Tool specs the skill may propose |
| `required_capabilities` | For example, structured output; refused if the provider lacks it |
| `max_output_tokens`, `temperature` | Generation limits (temperature 0 for repeatability) |
| `deadline_epoch_s` | The investigation's absolute deadline |
| `call_timeout_s` | Per-call limit (≤ 60 s) |
| `model_alias` | Resolved to a pinned model by configuration |

**ModelResult** ([Q10.14](conversation.md)): `request_id`, `attempt`; `status`, `stop_reason`; `content` blocks and `parsed`; `validation_errors`, `refusal_detail`, `error`; `usage`; `cost_micros`, `latency_ms`; `identity`; `provider_request_id`. Each attempt is stored as a `model_attempts` row (Artifact 7; conflict C11).

**Content blocks** are an ordered list; order is preserved, and unknown block types are dropped and logged ([Q10.15](conversation.md)):

| Block | Meaning |
| --- | --- |
| `TextBlock` | The answer, or the JSON text to validate |
| `ToolProposal` | `proposal_id`, `tool`, `arguments`: a requested tool call |
| `ReasoningBlock` | Reasoning text a provider may return. Kept for debugging, never treated as evidence, never shown as a finding |

**Tool proposals are data, not an action** ([Q10.16](conversation.md)). The gateway never runs tools. A proposal goes to the coordinator, then to the ToolGate (Artifact 9), which checks the tool, schema, scope, window, size, budgets and repeats. Strict tool schemas make well-formed proposals more likely, but the gate still validates every one.

**Normalised enums** ([Q10.18](conversation.md), code):

- `StopReason`: `COMPLETE`, `TOOL_USE`, `MAX_TOKENS`, `CONTEXT_EXCEEDED`, `STOP_SEQUENCE`, `REFUSAL`, `PAUSED`, `INCOMPLETE`, `UNKNOWN`.
- `ResultStatus`: `OK`, `INVALID_OUTPUT`, `TRUNCATED`, `INCOMPLETE`, `REFUSED`, `TIMEOUT`, `RATE_LIMITED`, `PROVIDER_ERROR`, `AUTH_ERROR`, `BAD_REQUEST`, `CAPABILITY_UNSUPPORTED`, `BUDGET_REFUSED`, `DEADLINE_PASSED`.

**Capabilities** ([Q10.22](conversation.md)): `structured_output`, `tool_use`, `strict_tools`, `streaming`, `usage_reporting`, `pinned_version`.

- Each skill declares `required_capabilities`; each adapter declares what it supports.
- The gateway refuses before calling: `CAPABILITY_UNSUPPORTED`, no tokens spent.
- No silent downgrade: it won't drop the schema and hope for JSON.
- Capabilities are "verified", not "advertised": an adapter declares one only after our conformance suite passes against it.

**ModelIdentity**, stored with each model attempt ([Q10.23](conversation.md)):

- Provider: who runs it and how we reach it (`anthropic`, `tinker`). The adapter is chosen by this.
- Model: the exact model ID returned in the response, not just the alias.
- Checkpoint: for a fine-tuned model, the exact weights, such as a Tinker sampler path.
- Adapter version, prompt version, output schema fingerprint, context fingerprint, provider's request ID.

Temperature 0 is used for repeatability, but outputs can still differ slightly, so the stored input and output are the record. Re-running is a comparison, not a guarantee.

## 10.3 Claude adapter

*Answers Q10.11, Q10.12, Q10.15, Q10.17–Q10.21, Q10.38.*

Code: `ClaudeAdapter` in [files/adapters.py](files/adapters.py). An adapter does exactly two things: `build(request) → body`, and `parse(response) → ModelResult`. It does no retries, no budgeting and no validation of meaning. Those belong to the gateway. All provider code is in one adapter per provider ([Q10.11](conversation.md)).

**Request mapping:**

| ModelRequest | Claude request |
| --- | --- |
| `model_alias` | `model`, from the configured alias-to-pinned-ID map |
| `system` | `system` |
| `context` | One user message of labelled `<data ref=… kind=…>` blocks, with a statement that they are evidence and never instructions |
| `output_schema` | `output_config.format`, type `json_schema` |
| `tools` | `tools`, each with `strict: true` |
| `max_output_tokens`, `temperature` | `max_tokens`, `temperature` |
| `incident_id` | `metadata.user_id` |

**Response mapping:** a `tool_use` block becomes a `ToolProposal` with its `id`, `name` and `input`; text becomes a `TextBlock`; `thinking` becomes a `ReasoningBlock` ([Q10.12](conversation.md), [Q10.15](conversation.md)). The `request-id` header becomes `provider_request_id`.

**Stop reasons** ([Q10.18](conversation.md)):

| StopReason | Claude value | Result status | Handling |
| --- | --- | --- | --- |
| `COMPLETE` | `end_turn` | OK, then validated | Use it |
| `TOOL_USE` | `tool_use` | OK | Proposals to the gate (conflict C15) |
| `MAX_TOKENS` | `max_tokens` | `TRUNCATED` | One retry with double the output limit (≤ 16,000), within budget (conflict C4) |
| `CONTEXT_EXCEEDED` | `model_context_window_exceeded` | `TRUNCATED` | Context assembly must shrink the input (conflict C5) |
| `REFUSAL` | `refusal` | `REFUSED` | Not retried |
| `PAUSED` | `pause_turn` | — | Provider-side tool loops; not used by our skills |
| `INCOMPLETE` | stream ended without a stop event | `INCOMPLETE` | Retried |

`stop_sequence` maps to `STOP_SEQUENCE`, and any other value to `UNKNOWN` (code only; no answer covers them). A truncated output is never validated as if complete.

**Refusal** ([Q10.17](conversation.md)): the Claude API returns a refusal as a normal response with `stop_reason: "refusal"`, not as an error. It is status `REFUSED`, with the provider's details in `refusal_detail`. It is logged and counted: a pattern of refusals on incident data usually means a prompt or redaction problem, which is reviewed.

**Broken stream** ([Q10.19](conversation.md)): a stream is complete only when the final stop event arrives (`message_stop`, after a `message_delta` carrying the stop reason). The transport reports `stream_completed`. If it's false, the adapter sets `INCOMPLETE`, whatever text arrived.

**Structured output** ([Q10.20](conversation.md), [Q10.21](conversation.md)): `output_config.format` makes invalid JSON rare on Claude, but validation stays (10.7).

**HTTP errors** ([Q10.38](conversation.md), `classify_http_error`):

| Status | Result status | Retried |
| --- | --- | --- |
| 401, 403 | `AUTH_ERROR` | No |
| 400, 404, 413, 422 | `BAD_REQUEST` | No |
| 429 | `RATE_LIMITED` | Yes |
| 408 | `TIMEOUT` | Yes |
| 5xx, including 529 overloaded, and anything else | `PROVIDER_ERROR` | Yes |

**Conformance suite:** the adapter tests cover request shape, tool proposals, stop reasons, usage and errors. A new provider means a new adapter, passing that suite, plus an evaluation run. The agent doesn't change ([Q10.11](conversation.md)).

## 10.4 Tinker adapter

*Answers Q10.2, Q10.5, Q10.22.*

**Tinker is deferred in the first version.** Claude only initially, with Tinker deferred (ADR-07): it is for fine-tuning our own model, which Artifact 2 excluded from the first version, and its serving API is in beta ([System Design](../../reference/system-design.md), section 5). The adapter exists, disabled by default, and is not on the production path.

**Why it is not a like-for-like swap** ([Q10.5](conversation.md)): Tinker is a service for fine-tuning open-weight models, where you control the training loop. Every saved checkpoint can be sampled. Its inference API, compatible with the OpenAI and Anthropic interfaces, became available in beta in October 2026. See the matrix in 10.1.

**Adapter** (`TinkerAdapter` in [files/adapters.py](files/adapters.py)):

- Disabled by default: `enabled=False`, and `build` raises while disabled (conflict C13).
- Reuses the Claude request shape, against a different base URL and a checkpoint model.
- Maps each alias to a checkpoint, `tinker://<run>/sampler_weights/<step>`.
- Identity records provider `tinker`, adapter `tinker-adapter@0`, and the checkpoint ([Q10.2](conversation.md)).
- Capabilities stay unverified until our conformance suite passes against it, so a skill that requires structured output gets `CAPABILITY_UNSUPPORTED` and no tokens are spent ([Q10.22](conversation.md), tested).

**Evaluation plan** ([Q10.5](conversation.md)):

1. Build a labelled dataset from resolved incidents (Artifact 9's evaluation set).
2. Fine-tune on Tinker in a separate experiment, never on the production path.
3. Run the same evaluation suite through the same gateway (same prompts, contexts and schemas), on both.
4. Compare accuracy, false-established rate, schema validity, latency and cost per correct diagnosis.

**Reaching production** takes a recorded decision, through the ADR-07 revisit conditions ([Layer 6](../layer-06-system-architecture/conversation.md)): accuracy stalls below target with a hosted model; there are enough labelled incidents to train on; cost per incident is too high; or the organisation requires that data stays on a self-managed model.

## 10.5 Budget policy

*Answers Q10.27–Q10.29, Q10.36.*

Code: `IncidentBudget`, `BudgetLedger` in [files/gateway.py](files/gateway.py).

**Setting the budget** ([Q10.28](conversation.md)):

- **Budget = 10% × engineer time saved × loaded hourly cost**, then capped by the token and call caps in Artifact 4.
- Set in configuration per incident category.
- Recorded on each incident's `incident_budgets` row at creation.
- Reviewed after the pilot against real cost per incident.

**Gap:** no source gives the loaded hourly cost, the per-category values or the monthly budget ceiling, so no per-incident money figure can be derived. The code's default `max_cost_micros = 2_000_000` has no source (see Limits check).

**Caps** (Artifact 4; [System Design](../../reference/system-design.md), section 9): 30 calls, 200,000 input and 20,000 output tokens, a cost cap per incident; daily and monthly ceilings with alerts. When a limit is reached, the platform stops and hands over.

**Lifecycle of a call** ([Q10.27](conversation.md), [Q10.29](conversation.md)):

| Step | Rule |
| --- | --- |
| Estimate | Input tokens from the request size (about 3 characters per token, deliberately pessimistic), plus the full output limit, at configured prices |
| Reserve | Take that maximum cost out of the budget under a lock; in the database, a row lock |
| Refuse | If it doesn't fit, the call is refused before it is sent. Parallel calls each reserve, so together they can never exceed the budget |
| Settle | The reservation is replaced by actual cost |
| Release | Only if the call provably never left the process, for example when no concurrency slot came free before the deadline |

**Why unknown usage is not released** ([Q10.36](conversation.md)): the tokens were probably spent. Releasing the reservation would make the budget look larger than it is, so further calls could overspend the cap; under-report cost; and let a stream of timeouts drain money while the budget appears untouched. This differs from the Technical Specification (conflict C1).

Reporting: cost per incident and per correct diagnosis (Artifact 9). "20 calls can't produce a surprise, because each was reserved within a cap before it was made" ([Q10.27](conversation.md)).

`BudgetLedger` is an in-memory stand-in for `incident_budgets` and `budget_reservations`. The production implementation does the same arithmetic under a row lock in one transaction.

## 10.6 Concurrency policy

*Answers Q10.30, Q10.31.*

Layered limits ([Q10.30](conversation.md)); code: `GatewayConfig` in [files/gateway.py](files/gateway.py):

| Layer | Limit | Enforced by |
| --- | --- | --- |
| Incident level | At most 5 investigations at once; the rest queue by severity | Workflow (Artifact 4) |
| Gateway global slots | At most 8 model calls in flight per provider (configuration) | `global_concurrency` |
| Per-incident slots | At most 2 | `per_incident_concurrency` |
| Provider rate limits | 429 retried with backoff, within the deadline, never multiplied | Gateway retry (10.7) |

Hundreds of incidents therefore produce a queue, not hundreds of simultaneous calls.

**Per-incident semaphore** ([Q10.31](conversation.md)): a semaphore of 2, taken before a global slot, so one incident can hold at most 2 of the 8. Tested: 6 parallel calls for one incident never exceed 2 at a time. The coordinator is sequential anyway (Artifact 9), so 2 leaves room only for small parallel steps.

If no slot comes free before the deadline, the result is `DEADLINE_PASSED`, and the reservation is released because the call was never sent.

Historical peak concurrency is an open unknown (owner SRE lead; [System Design](../../reference/system-design.md), section 10).

## 10.7 Retry and timeout policy

*Answers Q10.17–Q10.20, Q10.32, Q10.33, Q10.37–Q10.39.*

Code: `ModelGateway.call` and `_attempt` in [files/gateway.py](files/gateway.py).

**Only the gateway retries model calls** ([Q10.37](conversation.md)):

- The provider SDK's own retries are turned off.
- The Temporal activity for each model skill has 1 attempt.
- The gateway allows 3 attempts per step, with backoff (2 s, then 4 s), within the deadline.
- Every attempt reserves budget, so retries are visible in cost.
- The coordinator's revision loop is separate, and bounded (Artifact 9).

At most 3 provider calls per step, never 27.

| Result | Retried | Handling |
| --- | --- | --- |
| `INVALID_OUTPUT` | Yes | Never repaired by guessing. Retried with the error added to the context: "Your previous answer failed validation: …" |
| `TRUNCATED` from `max_tokens` | One retry | Double the output limit (≤ 16,000), within budget |
| `TRUNCATED` from context | See 10.3 | Context assembly must shrink the input |
| `INCOMPLETE` | Yes | New attempt; the partial text is discarded |
| `TIMEOUT`, `RATE_LIMITED`, `PROVIDER_ERROR` | Yes | Backoff, within the deadline |
| `REFUSED` | No | The same request would be refused again, and it costs budget. The step fails; the coordinator treats it as inconclusive, and hands over |
| `AUTH_ERROR` | No | The platform alerts immediately; investigations degrade to evidence only until it's fixed |
| `BAD_REQUEST` | No | Retrying changes nothing |
| `CAPABILITY_UNSUPPORTED`, `BUDGET_REFUSED`, `DEADLINE_PASSED` | No | No call is made |

After 3 attempts, the step fails, and the coordinator records it as inconclusive ([Q10.20](conversation.md)).

**Why permanent errors aren't retried** ([Q10.38](conversation.md)): the key is wrong, expired or revoked, or the permission is missing. Retrying wastes time inside the deadline, adds load, may trip the provider's abuse protection, and hides an outage that needs a person.

**Two time limits** ([Q10.33](conversation.md)). The question's 30 seconds and 10 minutes are illustrative; the platform's values are 60 s and 15 min.

| Limit | Field | Meaning | On breach |
| --- | --- | --- | --- |
| Per-call timeout | `call_timeout_s`, ≤ 60 s | How long one request may take | Ends one attempt, which may be retried |
| Investigation deadline | `deadline_epoch_s`, investigation start + 15 minutes | When all work must stop | Ends the step and the loop |

The effective timeout of each call is the smaller of the two: `min(call_timeout, deadline − now)`.

**Deadline enforcement** ([Q10.32](conversation.md)):

- The gateway checks it before each attempt (`DEADLINE_PASSED`, no call; tested), before waiting for a slot, and before each backoff.
- A retry is only scheduled if its backoff ends before the deadline.
- The coordinator also stops at the limit (Artifact 9), and the workflow's activity timeouts bound everything (Artifact 8).

**No automatic cross-provider fallback** ([Q10.39](conversation.md)): `allow_cross_provider_fallback = false` (ADR-38). The risks:

- Different behaviour, unmeasured: prompts, schemas and refusal patterns were evaluated on one model.
- Different capabilities: the fallback may lack structured output, so validation failures spike.
- Data and contract risk: incident data would go to a provider with different terms, possibly one not approved.
- Cost surprise: different prices, and sudden full traffic.
- Reproducibility: one investigation would mix models.
- It can amplify an outage: a burst of traffic to the second provider hits its rate limits too.

The agreed outage behaviour is Artifact 2's: evidence and timeline only, no fix proposals. A second provider is added only after it passes the same evaluation, as a recorded decision, and with fallback per skill, not global.

## 10.8 Token accounting

*Answers Q10.25, Q10.26, Q10.34–Q10.36.*

Code: `Usage` in [files/gateway_contracts.py](files/gateway_contracts.py); `estimate_tokens`, `cost_micros`, `BudgetLedger.settle` in [files/gateway.py](files/gateway.py).

**Unit:** tokens, as reported by the provider, converted to money with the configured price per million tokens. Money is stored as whole millionths of the currency unit (Artifact 7), so there are no rounding errors ([Q10.25](conversation.md)).

**Four counters, priced separately** ([Q10.26](conversation.md)): input, output (usually several times the input price), cache read and cache write. Both the per-incident token caps and the cost cap use the split.

**Cumulative tracking** ([Q10.34](conversation.md)):

- One ledger per incident (`incident_budgets`): calls, input tokens, output tokens, reserved and spent cost.
- Every attempt by every skill (hypothesise, diagnose, critique, revisions, retries) reserves and settles against it.
- Each attempt is also a `model_attempts` row, with task and attempt number, so totals can be broken down by skill.

The coordinator's model-call count and the ledger agree, because both count attempts.

**Missing usage** ([Q10.35](conversation.md)):

| Step | Rule |
| --- | --- |
| Mark | `Usage.reported = false` |
| Charge | The full reservation, the pessimistic maximum, to the incident (tested) |
| Reconcile | Add to `pending_reconcile`: a daily job compares these with the provider's usage records or billing export, and corrects the ledger |
| Count | Frequent missing usage from a provider is an alert |

The code treats a timeout and any non-200 response as missing usage, so a 401 is also charged in full and queued for reconciliation. That matches "only a call that provably never left the process is released" ([Q10.36](conversation.md)). The daily job and the alert are not in `files/`.

## 10.9 BM25 ingestion and search

*Answers Q10.40–Q10.42, Q10.45–Q10.50, Q10.52–Q10.56.*

Code: [files/retrieval.py](files/retrieval.py). Nothing here calls a model.

**Purpose** ([Q10.40](conversation.md), [Q10.41](conversation.md)): the model doesn't know our services, our configuration keys, our error codes, our runbooks or what fixed last month's incident. The Knowledge Retriever searches an index of runbooks and past incident reviews, filtered to the incident's service and the platform's permissions. It returns the top passages as evidence records of kind `runbook`. Retrieved passages are guidance, not proof: the incident's own evidence still has to support the diagnosis.

**Ingestion** ([Q10.42](conversation.md)):

1. **Source:** runbooks from the repository or wiki, and past incident reviews, each read with its version (commit or revision ID), owner, services, permitted groups and last-reviewed date.
2. **Redact** secrets and personal data (Artifact 5).
3. **Chunk** with headings kept (10.10).
4. **Store** documents and chunks in PostgreSQL, keyed by `doc_id@version#ordinal`, each with its text span and fingerprint.
5. **Build a new immutable index revision** on any change, nightly at most, and on demand after an edit.
6. **Evaluate** the new revision against the retrieval test set (10.13) before activating it.
7. **Activate:** new investigations use it; old ones keep the revision they recorded.

**Gap:** Artifact 7's schema has no tables for documents, chunks or index revisions, so where step 4 stores them is not defined. `retrieval.py` holds them in memory.

**Why BM25 first** ([Q10.50](conversation.md)):

- Our queries are mostly exact terms: alert names, error codes, configuration keys and service names copied from evidence.
- No new system: there is no embedding model, vector store or extra provider.
- Explainable and debuggable: a hit can be traced to the matched terms.
- Deterministic and reproducible: the same revision and query always give the same results (but see conflict C14).
- Measured before replaced: if recall falls short on paraphrased queries, add embeddings with hybrid fusion as a recorded decision.

Conflict C9 records where BM25 runs.

**Search** ([Q10.45](conversation.md), [Q10.46](conversation.md)):

| Part | Rule |
| --- | --- |
| Tokenizer | Keeps identifiers whole (`err_conn_reset`, `payments.timeout_ms`) and adds their parts (`timeout_ms`, `timeout`, `ms`). Tested: the identifier query ranks the right runbook first |
| Rare terms | idf = ln(1 + (N − df + 0.5) / (df + 0.5)). `db-failover-7` gets a high weight, `checkout` a low one (tested) |
| Repeats | `k1 = 1.2` caps the gain from repeating a term |
| Length | `b = 0.75` normalises for chunk length |
| Filters | Permission, service and staleness apply before ranking |
| Results | Top 5 by default (`k = 5`) |

**Deferred: semantic and hybrid search.** Not in the first version ([Q10.47](conversation.md)–[Q10.49](conversation.md)):

- Embeddings let "Checkout is throwing server errors after release" match "HTTP 500 after deploy", although they share few words.
- Dense vector search is nearest-neighbour search over chunk embeddings (cosine similarity). The simplest option for us would be the `pgvector` extension in PostgreSQL.
- Hybrid retrieval would run both and merge with reciprocal rank fusion, scoring `Σ 1 / (60 + rank)`, optionally with a re-ranker over the top 20. Adopted only if evaluation shows BM25 alone misses relevant passages.

**Immutable index revisions** ([Q10.52](conversation.md)):

- A revision is built from a fixed set of document versions. Its ID is a hash of `doc_id@version` for every live document.
- Revisions are never modified. Any edit, addition or deletion builds a new one (tested).
- Chunks are stored by version, so old chunks remain.
- Each investigation records the revision ID it searched, in the context manifest.
- Keep every revision referenced by an incident for the incident retention period (1 year; conflict C10).

**Pinned citations** ([Q10.53](conversation.md)): each runbook hit becomes an evidence record carrying document ID and version, chunk ID, character start and end in that version, the chunk's fingerprint, and the heading path. The text is stored with the evidence. The display shows "version v3; the current version differs" when it has changed.

**Stale documents** ([Q10.54](conversation.md)):

- Review age: a document not reviewed by its owner within 180 days is marked stale.
- Deprecated flag: set by owners.
- Changed since review: the service it describes was deployed with relevant changes after the last review.
- Stale chunks are excluded from search by default (tested). They can be included explicitly, and are then labelled stale in the context.
- A weekly report (`stale_report`) goes to owners (tested).
- Feedback: a runbook passage cited in a diagnosis that was later rejected is flagged for review.

**Deleted documents** ([Q10.55](conversation.md)):

- Deletion builds a new index revision without the document, activated straight away. New searches no longer return it (tested).
- Old revisions keep it, only for reproducing past investigations, readable by incident reviewers, never searched by new investigations.
- If the deletion was for legal or safety reasons, the document's chunks are purged from every revision, and the affected evidence records are marked "content removed".

**Permissions** ([Q10.56](conversation.md)):

- Each document carries its permitted identity groups, copied from the source system at ingestion, and re-synced at each index build.
- Search filters by permission before ranking, never after (tested).
- The platform's own searches use the incident service's responder groups.
- Engineers viewing an incident see a runbook citation's text only if they may read that document; otherwise "restricted runbook (owner: …)".

## 10.10 Chunking strategy

*Answers Q10.43, Q10.44.*

Code: `chunk_document` in [files/retrieval.py](files/retrieval.py).

**Structure first, size second** ([Q10.43](conversation.md)):

- Split at headings, so each section is about one thing.
- Within a long section, group paragraphs up to about 220 words.
- Never split a code block, a list, or mid-sentence.
- Prefix every chunk with its heading path, such as "Checkout runbook › HTTP 500 after deploy › Revert config".
- Record each chunk's exact character span in the source.

Tested: the "Revert config" chunk keeps its full path and its whole code block, and its span maps back to the source text.

**Why not fixed-size** ([Q10.44](conversation.md)): a fixed 500-character cut can leave "Run `deployctl config revert …`" in a chunk without the heading that says when to run it ("HTTP 500 after deploy"). With heading-aware chunking:

- each chunk starts at a heading boundary and carries its full heading path, so its context is never lost
- the heading words are searchable with the body, so "500 after deploy" finds the steps under that heading
- a step is never cut away from the section it belongs to

Each chunk stores: `doc_id@version#ordinal`, heading path, text, start and end, SHA-256 fingerprint, services, allowed groups, stale flag.

## 10.11 Context assembly

*Answers Q10.57–Q10.63.*

Code: `assemble`, `merge_repeated_logs` in [files/context.py](files/context.py).

**Selection.** `context.assemble` picks from canonical records under a token budget, by priority ([Q10.57](conversation.md)):

| Priority | Content | Under pressure |
| --- | --- | --- |
| 0 | Computed facts: the timeline, version split, changed values | Always kept |
| 1 | Contradictions, both sides | Always kept |
| 2 | Gaps (missing sources) | Always kept |
| 3 | Evidence cited by the current draft (on revision) | Always kept; may be truncated |
| 10–15 | Other evidence, most decisive kind first: test result (10), trace and error group (11), metric (12), change and dependency (13), log (14), alert (15) | Truncated, then dropped |
| 30 | Past incidents (code; other kinds default to 20) | Truncated, then dropped |
| 40+ | Up to 3 runbook passages | Truncated, then dropped |

Tested: under an 800-token budget, low-priority items are dropped, and the drops are recorded in the manifest.

**Target size** ([Q10.61](conversation.md)): the per-call input cap is 16,000 tokens (Artifact 4); assembly targets about 12,000, leaving room for instructions and schema.

**Decisiveness for the question being asked, then recency** ([Q10.61](conversation.md)):

1. What the current draft cites, which the critic must check.
2. Mechanism evidence: test results, traces, error groups, version-split metrics.
3. Change records and dependency health, which separate deployment from non-deployment causes.
4. Logs, then alerts.
5. Past incidents and runbooks.

**Live data and documentation** sit as separately labelled blocks in one data section, each with its kind and reference ([Q10.58](conversation.md)):

- `[metric from monitoring, window 09:00–09:15] …` with ref `evidence:<id>`
- `[runbook Checkout › HTTP 500 after deploy, version v3] …` with ref `runbook:<chunk>`

Live evidence shows what is happening; runbooks show what usually helps. A claim about this incident must cite live evidence, never only a runbook. Runbooks come last in priority, so they never push out incident evidence.

**Contradictions** ([Q10.59](conversation.md)) are first-class items: "These sources disagree: A vs B", with both references and a short note. They are always included, regardless of budget (tested). Both sides' evidence is kept, never deduplicated into one. The diagnosis schema has `contradicting_evidence_ids`, and the critic blocks a diagnosis that ignores strong contradicting evidence (Artifact 9).

**Compression, in this order** ([Q10.60](conversation.md)):

1. Reduce at collection: log lines become error templates with counts and first and last seen; metrics become summary points around the change; traces become the failing span and its error.
2. Merge repeats: identical log templates become one item, "seen 4 times", listing every source reference (tested).
3. Truncate long items from priority 3 onwards, with "[truncated; full record: evidence:\<id>]". The code's per-item limit is 600 tokens.
4. Drop by priority from the bottom, recording each drop in the manifest.
5. Never drop facts, contradictions, gaps or cited evidence. If those alone exceed the budget, assembly raises an error, and the coordinator narrows the scope rather than silently cutting.

There is no model-written summarisation of evidence in the first version: compression is deterministic code (conflict C3).

**Provenance** ([Q10.62](conversation.md)):

- Every context item carries `refs`: the list of every source it stands for (tested: all items have refs, and a merged item lists all 4).
- Truncated items name their full record.
- The manifest lists each included item's reference and fingerprint, plus everything excluded and why.
- Any future model-based summariser must output a schema in which every sentence carries refs, validated like any other output. A summary sentence without refs is rejected.

**Summaries never replace evidence** ([Q10.63](conversation.md)). A summary is an interpretation: it may be wrong, may drop the detail that matters, and may carry injected text. Verification needs the exact log line, metric values and query; audit needs what was actually seen, with fingerprints; later revisions and critics must reason from facts. So evidence records are canonical and immutable. Summaries are derived, labelled as such, and always link back to them.

## 10.12 Prompt versioning

*Answers Q10.23, Q10.64.*

Code: `PromptVersion`, `PromptRegistry` in [files/context.py](files/context.py).

| Rule | Source |
| --- | --- |
| A prompt version is `prompt_id@version`, such as `diagnose@7` | `PromptVersion.ref` |
| A version carries its text, output schema name, model alias and the evaluation run that approved it | `PromptVersion` |
| Its fingerprint is the SHA-256 of its text | `PromptVersion.sha256` |
| Prompts live in the repository, one file per version | `PromptRegistry` |
| A version is never edited after use; re-adding one with different text is an error (tested) | `PromptRegistry.add` |
| A new version goes live only after it passes the evaluation suite (Artifact 9, 9.11) | `PromptRegistry` |
| Old versions stay readable for replay (tested) | `PromptRegistry.get` |
| Changing the model is a configuration change with its own evaluation run | [Q10.2](conversation.md) |

**Gap:** no question covers the prompt release process directly. The sources don't say who approves a version, how an active version is rolled back, or how the repository files are laid out.

**What must be kept to reproduce a diagnosis** ([Q10.64](conversation.md)):

| What | Where |
| --- | --- |
| Prompt ID, version and text fingerprint | `model_attempts.prompt_template_version`; prompt files in the repository (immutable per version) |
| Output schema and its fingerprint | Contracts in the repository; `ModelIdentity.output_schema_sha256` |
| Provider, exact model ID, checkpoint, adapter version | `ModelIdentity` |
| Temperature and output limit | Stored request (artifact store) |
| The exact context sent | Model input artifact, plus the context manifest (items, refs, fingerprints, exclusions) |
| Evidence records and snapshots | `evidence` + artifact store (fingerprinted) |
| Retrieval index revision, and the chunks returned | Manifest `index_revision`; chunks stored by version |
| Coordinator decisions and limits | Workflow decisions, copied to the timeline |
| The model's raw output and its validation result | Model output artifact; `model_attempts` |
| Code version | Release tag on the timeline |

Replay = same prompt version + same context + same model, then compare outputs. If the model ID has been retired, the stored output is the record, and a re-run on a current model is a labelled comparison. Conflicts C10–C12 affect this table.

## 10.13 Retrieval evaluation

*Answers Q10.42, Q10.51.*

Code: `recall_at_k`, `mrr`, `ndcg_at_k` in [files/retrieval.py](files/retrieval.py).

Measured against a labelled query set: queries taken from real alerts and evidence, each with the chunks an SRE marks relevant ([Q10.51](conversation.md)).

| Metric | Meaning | Target |
| --- | --- | --- |
| Recall@5 | Share of relevant chunks in the top 5 | ≥ 0.9 |
| MRR | How high the first relevant chunk ranks | ≥ 0.7 |
| nDCG@5 | Ranking quality | ≥ 0.8 |
| Wrong-service and wrong-permission hits | Results outside the incident's service or the caller's groups | 0 |

**When it runs:** on every new index revision before activation ([Q10.42](conversation.md)), and on tokenizer or chunking changes. In production, also track how often cited runbook passages appear in accepted diagnoses.

**Result:** if recall falls short on paraphrased queries, add embeddings with hybrid fusion, as a recorded decision ([Q10.50](conversation.md)).

**Gap:** the labelled query set does not exist yet, and no source sets its size or owner. `files/` has no function for wrong-service and wrong-permission hits, and no activation gate.

## Assumptions

| Assumption | Status | Owner | Validated when |
| --- | --- | --- | --- |
| A change-caused investigation saves about 30 minutes of engineer time ([Q10.28](conversation.md)) | Provisional | SRE lead | Pilot incidents are timed |

## Decisions and conflicts

**Decisions.** All decisions in this layer are in the sections above.

**Verification.** The 28 tests in `test_layer10.py` pass on a scratch copy of `files/`. The code behaviours below were confirmed by running that copy. `files/` was not changed.

**Disagreements.** `conversation.md` records none between Claude and ChatGPT; the team fills that in.

### Limits check against System Design section 9

| Limit | Section 9 value | Code | Result |
| --- | --- | --- | --- |
| Model calls per incident | 30 | `IncidentBudget.max_calls = 30` | Matches |
| Tokens per incident | 200,000 input, 20,000 output | `max_input_tokens = 200_000`, `max_output_tokens = 20_000` | Matches |
| Tokens per call | 16,000 input | Not enforced: a request of about 30,000 estimated input tokens went through. `assemble` takes `budget_tokens` with no default | **Differs** (C2) |
| Attempts per step | 3 (original + 2 retries) | `GatewayConfig.max_attempts = 3` | Matches |
| Time per model call | 60 s | `call_timeout_s` default 60, maximum 60 | Matches |
| Investigation time | 15 min | No constant: the gateway enforces the `deadline_epoch_s` the caller passes | Matches in the answers; not in code |
| Whole workflow | 24 h | Not in Layer 10 code | Outside the gateway |
| Concurrent investigations | 5; queue up to 50 by severity | Not in Layer 10 code; Q10.30 states the 5 but not the 50 | Outside the gateway |
| Daily spend, all incidents | Monthly budget ÷ 30; alert at 80%, evidence-only at 100% | Not in code | **Differs** (C2) |
| Monthly spend | Budget ceiling; alerts at 50%, 80%, 100% | Not in code | **Differs** (C2) |
| Spend per incident | No figure; "about 10%" of engineer time saved | `max_cost_micros = 2_000_000`, no source | **Differs** (C2) |
| Reservation before each call | Reserved, then actual replaces it | `BudgetLedger.reserve`, `settle` | Matches |
| Database rejects totals above the limit | Stated | In-memory stand-in; the check is in Artifact 7's schema | Matches by design |

Limits in code that section 9 does not list: per-call output ≤ 16,000 (`max_output_tokens`), 8 global slots, 2 per-incident slots, backoff 2 s and 4 s. A 16,000-token reservation holds most of the 20,000 output cap until it settles.

### Conflicts

None of these is resolved here. Sections show the Layer 10 answer unless stated, and each needs a team decision.

| # | Conflict | Sources |
| --- | --- | --- |
| C1 | **Unsettled reservations.** Layer 10 says unknown usage is charged at the maximum and only a never-sent call is released. The Technical Specification, Layers 7 and 8 and the schema (`expires_at`) say an unsettled reservation is released when it expires. `gateway.py` has no expiry | [Q10.36](conversation.md); Tech Spec section 7; Q7.15 of [Layer 7](../layer-07-data-apis-events/conversation.md); Q8.11 of [Layer 8](../layer-08-workflows-state-machines/conversation.md) |
| C2 | **Limits missing from code.** Per-call input cap (NFR-37), daily and monthly ceilings, and a sourced per-incident cost cap are stated but not implemented | Limits check above; [Q10.27](conversation.md), [Q10.61](conversation.md) |
| C3 | **Over-limit context.** Section 9 and Q4.33 say "trim or summarise evidence to fit". Q10.60 says no model-written summarisation, and raises an error if required items exceed the budget, so the coordinator narrows the scope | System Design section 9; [Q10.60](conversation.md) |
| C4 | **Truncation retries.** Q10.18 says one retry. The code doubles on each `TRUNCATED` while below 16,000, so three attempts ran at 500, 1,000 and 2,000. The test checks only the first doubling | [Q10.18](conversation.md); `gateway.py` |
| C5 | **Context exceeded.** Q10.18 says assembly must shrink the input. The code treats it like `max_tokens` and doubles the output limit, with the same input | [Q10.18](conversation.md); `gateway.py` |
| C6 | **Broken stream cost.** Q10.19 says usage is unknown, so the reservation is charged in full. `parse` uses any usage in the body regardless of `stream_completed`; in a test-shaped case it settled at reported usage with nothing queued for reconciliation | [Q10.19](conversation.md); `adapters.py` |
| C7 | **Timeout attempt record.** Q10.35 marks usage as estimated. On timeout, the ledger charges the full reservation, but the logged result has `reported=True` and `cost_micros=0`, so a `model_attempts` row would under-report it | [Q10.35](conversation.md); `gateway.py` `_final` |
| C8 | **Gateway features named elsewhere.** Layers 4, 8 and 12 put a circuit breaker in the gateway (provisional: opens at 50% failures of at least 10 calls in 60 s, for 30 s). Layer 8 also has it reuse a recorded result with the same `input_sha256`. Neither is in Layer 10's answers or `gateway.py` | NFR-28 (Q4.27); Q8.11, Q8.17, Q8.44 of [Layer 8](../layer-08-workflows-state-machines/conversation.md); Q12.23 of [Layer 12](../layer-12-reliability-evaluation/conversation.md) |
| C9 | **Where BM25 runs.** ADR-09 says PostgreSQL's built-in full-text search. Q10.50 and `retrieval.py` run BM25 in the worker. System Design says only "keyword search" | Q6.29 of [Layer 6](../layer-06-system-architecture/conversation.md); [Q10.50](conversation.md) |
| C10 | **Retention versus replay.** Q10.64 relies on model input and output artifacts for a six-month-old diagnosis, and Q10.52 keeps revisions 1 year. The Technical Specification keeps raw evidence and model input and output 30 days (provisional) | [Q10.52](conversation.md), [Q10.64](conversation.md); Tech Spec section 10 |
| C11 | **`model_attempts` is narrower than Layer 10 needs.** The schema has no columns for checkpoint, adapter version, output schema fingerprint, provider request ID, stop reason, cache tokens or the reported/estimated flag. It uses task `review` where `ModelRequest.task` uses `critique` | [Q10.14](conversation.md), [Q10.23](conversation.md); [schema-tables.sql](../layer-07-data-apis-events/files/schema-tables.sql) |
| C12 | **Prompt fingerprint.** Q10.23 and Q10.64 keep the prompt fingerprint. `ModelIdentity` and `Manifest` hold the version only; the fingerprint exists only on `PromptVersion` | [Q10.23](conversation.md); `gateway_contracts.py`, `context.py` |
| C13 | **Tinker capabilities.** Q10.5 and Q10.22 say an adapter declares only what is verified, with no silent downgrade. `TinkerAdapter` declares usage reporting and pinned version up front, and removes `output_config` and `tools` when unverified: a request with a schema but no `required_capabilities` went through without the schema. While disabled, `build` raises `RuntimeError` through the gateway rather than returning a result | [Q10.5](conversation.md), [Q10.22](conversation.md); `adapters.py` |
| C14 | **Revision ID versus content.** Q10.52 hashes `doc_id@version` only. Two builds with the same ID returned different results when only the build date (stale flags) or a document's permitted groups changed. This also conflicts with re-syncing permissions at each build (Q10.56) | [Q10.50](conversation.md), [Q10.52](conversation.md), [Q10.56](conversation.md); `retrieval.py` |
| C15 | **Tool proposals with a schema.** Q10.18 sends `TOOL_USE` to the gate. If the request also names an output schema, the code validates the absent text and returns `INVALID_OUTPUT`, so the proposal is not delivered | [Q10.18](conversation.md); `gateway.py` |
| C16 | **Estimators.** `context.py` says "same conservative estimate as the gateway", but adds 4 where `gateway.py` adds 16 | `context.py`, `gateway.py` |
| C17 | **ADR references.** ADR-27 is cited as Artifact 8's, and ADR-38 is cited in Layers 10 and 12, but no definition of either was found | [Q10.37](conversation.md), [Q10.39](conversation.md) |
| C18 | **Answers that claim more than `files/` shows.** Q10.29's parallel refusal has no parallel test, though a run confirmed it. Q10.51's "implemented and tested" does not cover wrong-service and wrong-permission hits. Q10.54's "changed since review" is in `stale_report` but does not set a chunk's stale flag. Q10.55's purge and Q10.56's restricted display are not in code | [Q10.29](conversation.md), [Q10.51](conversation.md), [Q10.54](conversation.md)–[Q10.56](conversation.md) |

## Acceptance checks

- [ ] Every required section above is present and complete.
- [x] Every section lists the questions it answers, and every question in this layer is answered by at least one section.
- [x] Every assumption is marked provisional and has a named owner and a condition under which it will be validated.
- [ ] Every disagreement between Claude and ChatGPT that affected a decision is recorded with the team's resolution.
- [ ] The artifact is consistent with every artifact it is built from; any conflict is recorded as an explicit decision.
- [x] The artifact was produced with the team's coding agent and is saved in the team repository.

Reviewed by: TBD
Date: TBD
