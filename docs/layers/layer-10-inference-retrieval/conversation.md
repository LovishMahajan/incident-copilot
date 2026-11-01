# Layer 10: Inference Engineering and Knowledge Retrieval — Conversation

- **Central question:** How do we run models reliably and give them the right information?
- **Built from:** Artifacts 2 and 9
- **Focus for this layer:** This layer contains two related but separate engineering problems: running models reliably (Parts A–C), and supplying them with the right information (Parts D–E). Keep the two designs distinct.
- **Deliverable:** [Artifact 10: Inference and Retrieval Specification](artifact-10-inference-and-retrieval-specification.md)
- **Questions:** 64

## Conversation log

| Questions | Agent | Conversation link | Decision reached | Open unknowns and owner | Artifact section |
| --- | --- | --- | --- | --- | --- |
| Q10.1–Q10.64 | TBD | [Shared conversation](https://claude.ai/share/c6a48a50-d511-4886-b4f5-0a3000f6a725) (needs sign-in; not read for these docs) | Recorded per question below | Listed under each question's Unknowns and Assumptions | [Artifact 10: Inference and Retrieval Specification](artifact-10-inference-and-retrieval-specification.md) |

## Where Claude and ChatGPT disagreed

TBD: the source documents do not record any disagreements. The team fills this in.

## Questions and answers

### Q10.1 The platform needs to transform input instructions into generated tokens. Which computational model performs this operation?

**Frame.** What turns instructions into generated tokens?

**Answer.** A **large language model**: a transformer that predicts the next token from all the tokens before it, repeated until a stop condition. The same model can be delivered by different providers, so the platform names the model and the provider separately (Q10.2).

### Q10.2 The engineering team wants to use a model, but different companies provide access to models through different interfaces. How should the underlying model be distinguished from the provider delivering it?

**Frame.** How do we keep "which model" separate from "who serves it"?

**Answer.** Three separate identifiers, recorded on every call (`ModelIdentity`):

- **Provider:** who runs it and how we reach it (`anthropic`, `tinker`). The adapter is chosen by this.
- **Model:** the exact model ID the provider returns, such as `claude-opus-5-5`, or a Tinker base model.
- **Checkpoint:** for a fine-tuned model, the exact weights, such as a Tinker sampler path.

Skills never name either. They ask for a **model alias** (`default`), which configuration resolves to one pinned provider and model. Changing the model is a configuration change with its own evaluation run.

### Q10.3 The team needs to use a model without operating GPU infrastructure. Which inference delivery approach would support this?

**Frame.** How do we use a model without running GPUs?

**Knowns.** Artifact 2: no GPUs and no custom model in the first version.

**Answer.** **Managed inference through a provider's API**: Claude through the Anthropic API. We send requests over HTTPS and pay per token. The provider runs the hardware, serving software, scaling and model updates.

### Q10.4 The organization wants complete control over its model-serving infrastructure. How would self-hosted inference differ from using an external provider?

**Frame.** What changes if we host the model ourselves?

**Answer.**

|  | **External provider** | **Self-hosted** |
| --- | --- | --- |
| Hardware | Theirs | Ours: GPUs bought or rented |
| Models | Their hosted models | Open-weight models only |
| Capacity planning, scaling, failover | Theirs | Ours |
| Serving software and upgrades | Theirs | Ours (Q10.8) |
| Data location | Leaves our network (under contract) | Stays inside |
| Cost | Per token; nothing when idle | Fixed GPU cost, even when idle |
| Model quality | Frontier models | Usually below frontier for reasoning tasks |
| On-call | Theirs for serving | Ours |

Self-hosting gives control and data residency, at the cost of a new system to run, and usually weaker models.

### Q10.5 The platform currently uses Claude, but the engineering team wants to experiment with Tinker. How should their different responsibilities and execution models be evaluated?

**Frame.** How do we compare Claude and Tinker fairly?

**Knowns.**

- Artifact 6 (ADR-07): Claude only initially, with Tinker deferred.
- Tinker is a service for fine-tuning open-weight models, where you control the training loop. Every saved checkpoint can be sampled. Its inference API, compatible with the OpenAI and Anthropic interfaces, became available in beta in October 2026.

**Answer.** They solve **different problems**:

|  | **Claude** | **Tinker** |
| --- | --- | --- |
| What it is | Hosted frontier models, used as-is | Fine-tuning service for open-weight models, plus sampling from your checkpoints |
| Our job | Prompts, context, validation | Training data, a training loop, choosing checkpoints, then everything Claude needs too |
| Identity | Pinned model ID | Base model + checkpoint path |
| Structured output and strict tools | Supported | **Unverified for us** until our conformance suite passes |
| Maturity for serving | Generally available | Beta (as of October 2026) |

**Evaluation plan:**

1. Build a labelled dataset from resolved incidents (Artifact 9's evaluation set).
2. Fine-tune on Tinker in a separate experiment, never on the production path.
3. Run the **same evaluation suite** through the same gateway (same prompts, contexts and schemas), on both.
4. Compare accuracy, false-established rate, schema validity, latency and cost per correct diagnosis.

Tinker reaches production only through a recorded decision (the revisit conditions in ADR-07). Until then its adapter is disabled, and declares only the capabilities we have verified.

### Q10.6 The organization wants to use managed inference instead of maintaining its own serving infrastructure. Which operational responsibilities would be transferred to the provider?

**Frame.** What does the provider take on?

**Answer.** Taken on by the provider:

- GPU procurement and capacity
- the serving software, batching and memory management
- scaling and load balancing
- hardware failures and failover
- model hosting and updates
- security patching of the serving stack
- availability of the API

**Kept by us**, whichever provider is used:

- prompts and context
- output validation
- retries and timeouts
- budgets and concurrency
- what data we send (redaction)
- the provider agreement (data retention, training use)
- evaluation
- what happens when the provider is down (Artifact 2: evidence only)

### Q10.7 The team proposes running its own GPU cluster even though its current inference volume is small. What workload or control requirements would justify this decision?

**Frame.** When is our own GPU cluster worth it?

**Answer.** Only when one or more of these is true and measured:

- **Data must not leave the network:** a regulatory or contractual rule that no provider agreement satisfies.
- **Sustained high volume:** GPU cost per month falls below the API bill at our real, steady utilisation. Small, bursty incident traffic is the opposite case.
- **A fine-tuned open model clearly beats hosted models** on our evaluation, and must be served at scale.
- **Latency or availability needs** a provider can't meet.
- **No acceptable provider** in the required region.

At current volume (a few incidents a day, 3–6 calls each) none of these hold. A cluster would add a new system to run, with idle GPUs most of the time. This matches Artifact 6, Q6.33.

### Q10.8 A team wants to serve models efficiently using GPU memory and batching optimizations. Which model-serving infrastructure could support this?

**Frame.** What serves models efficiently on our own GPUs?

**Answer.** A dedicated inference server. The usual choice is **vLLM**:

- **PagedAttention** manages the attention cache in pages, so far more requests fit in GPU memory.
- **Continuous batching** adds and removes requests from a running batch, so GPUs stay busy.
- It exposes an OpenAI-compatible API, which our gateway could reach with an adapter.

Alternatives: Hugging Face Text Generation Inference, and NVIDIA TensorRT-LLM with Triton. None of these is in the first version (Q10.7).

### Q10.9 Different inference providers are responsible for model execution, but the platform still needs consistent application behavior. Which responsibilities should belong to the provider and which should remain inside the application?

**Frame.** Who owns what?

**Answer.**

| **Provider** | **Application (Model Gateway and above)** |
| --- | --- |
| Running the model; tokenisation; sampling | One request and result contract for every provider |
| Its own rate limits and quotas | Budgets, reservations, per-incident and global concurrency |
| Reporting usage | Token accounting; handling missing usage |
| Constrained decoding, where supported | Schema validation of every output, always |
| Its availability | Timeouts, retry policy, deadlines, provider-outage behaviour |
| Refusing unsafe requests | Interpreting refusals; never retrying them blindly |
| — | Prompts, context, versions, identity, evaluation, redaction |

Rule: **anything that affects correctness, cost or safety stays in the application**, so changing providers doesn't change behaviour.

### Q10.10 The agent directly communicates with three different model providers, each requiring a different request format. How would you introduce one internal model interface?

**Frame.** How do we give the agent one interface?

**Answer.** The **Model Gateway** exposes one call, call(ModelRequest) → ModelResult (10.2). Skills build a `ModelRequest`: task, prompt version, instructions, context, output schema, tools, capabilities, limits, deadline. They never see a provider format. **One adapter per provider** translates to and from that provider's format (10.3, 10.4).

### Q10.11 The platform needs to switch providers without rewriting the agent. How should provider-specific communication be isolated?

**Frame.** How do we keep providers swappable?

**Answer.**

- **All provider code is in one adapter per provider.** Each has two methods: build(request) → body, and parse(response) → ModelResult.
- **The adapter declares its capabilities;** the gateway checks them before calling.
- **The model alias is resolved by configuration.**
- **A conformance suite** (the adapter tests) runs against each adapter: request shape, tool proposals, stop reasons, usage, errors.

Switching providers means a new adapter, passing that suite, plus an evaluation run. The agent doesn't change.

### Q10.12 One provider returns tool calls in a different structure from another. How should response formats be normalized?

**Frame.** How do we make tool calls look the same from every provider?

**Answer.** Every adapter maps its provider's tool call to one **`ToolProposal`**: `proposal_id`, `tool`, `arguments`. For Claude, a `tool_use` block becomes a ToolProposal with its `id`, `name` and `input` (tested). Text becomes a `TextBlock`, and reasoning a `ReasoningBlock`. Stop reasons map to one `StopReason` set (Q10.18).

### Q10.13 Different agent tasks require different model instructions, parameters and capabilities. Which fields should a ModelRequest contain?

**Frame.** What goes in a request?

**Answer.**

| **Field** | **Purpose** |
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

### Q10.14 The application needs to record the generated response, token usage and execution metadata. Which fields should a ModelResult contain?

**Frame.** What goes in a result?

**Answer.**

- `request_id` and `attempt`
- `status` (Q10.17–Q10.22) and `stop_reason`
- `content` blocks, and `parsed` (the schema-valid output)
- `validation_errors`, `refusal_detail`, `error`
- `usage`: input, output, cache read, cache write, **and whether it was reported or estimated**
- `cost_micros`, `latency_ms`
- `identity`: provider, model, checkpoint, adapter version, prompt version, schema fingerprint, context fingerprint
- `provider_request_id`

Each attempt is stored as a `model_attempts` row (Artifact 7).

### Q10.15 A provider returns text, tool calls and other structured content in one response. How should these content types be represented?

**Frame.** How is mixed content represented?

**Answer.** As an ordered list of **typed blocks**:

- **`TextBlock`****:** the answer, or the JSON text to validate
- **`ToolProposal`****:** a requested tool call (Q10.16)
- **`ReasoningBlock`****:** reasoning text a provider may return. It is kept for debugging, never treated as evidence, never shown as a finding.

Order is preserved. Unknown block types are dropped, and logged.

### Q10.16 The model requests a tool operation, but the application needs to validate it before execution. How should tool proposals be represented?

**Frame.** How is a tool request kept as a proposal?

**Answer.** As a `ToolProposal`, which is **data, not an action**. The gateway never runs tools. The proposal goes to the coordinator, then to the **ToolGate** (Artifact 9), which checks the tool, schema, scope, window, size, budgets and repeats. Only then does an adapter run it. Strict tool schemas (`strict: true`) make well-formed proposals more likely, but the gate still validates every one.

### Q10.17 A provider refuses to perform a requested operation. How should the application represent and handle this refusal?

**Frame.** How is a refusal handled?

**Answer.**

- **Represented** as status `REFUSED`, stop reason `refusal`, with the provider's details in `refusal_detail`. The Claude API returns a refusal as a normal response with `stop_reason: "refusal"`, not as an error.
- **Not retried** (tested): the same request would be refused again, and it costs budget.
- **The step fails;** the coordinator treats it as inconclusive, and hands over.
- **Logged and counted:** a pattern of refusals on incident data usually means a prompt or redaction problem, which is reviewed.

### Q10.18 A model stops generating because it reached a token limit rather than completing the response. How should the application distinguish these outcomes?

**Frame.** How do we tell "finished" from "cut off"?

**Answer.** By the **normalised stop reason**:

| **StopReason** | **Claude value** | **Result status** | **Handling** |
| --- | --- | --- | --- |
| `COMPLETE` | `end_turn` | OK, then validated | Use it |
| `TOOL_USE` | `tool_use` | OK | Proposals to the gate |
| `MAX_TOKENS` | `max_tokens` | `TRUNCATED` | One retry with double the output limit (≤ 16,000), within budget (tested) |
| `CONTEXT_EXCEEDED` | `model_context_window_exceeded` | `TRUNCATED` | Context assembly must shrink the input |
| `REFUSAL` | `refusal` | `REFUSED` | Not retried |
| `PAUSED` | `pause_turn` | — | Provider-side tool loops; not used by our skills |
| `INCOMPLETE` | stream ended without a stop event | `INCOMPLETE` | Retried |

A truncated output is **never** validated as if complete.

### Q10.19 A streaming response disconnects halfway through generation. How should the application detect an incomplete response?

**Frame.** How do we spot a stream that broke?

**Answer.** A stream is complete only when the **final stop event** arrives (for Claude, `message_stop`, after a `message_delta` carrying the stop reason). The transport reports `stream_completed`. If it's false, the adapter sets `INCOMPLETE`, whatever text arrived. It is **retried** as a new attempt; the partial text is discarded. Its usage is unknown, so the reservation is charged in full (Q10.35). Tested.

### Q10.20 A provider returns truncated JSON that cannot be parsed. How should the application handle incomplete structured output?

**Frame.** What happens with JSON that won't parse?

**Answer.**

1. **It is never repaired by guessing** (closing brackets, filling fields).
2. If the stop reason was `max_tokens`, it's `TRUNCATED`: one retry with a larger limit.
3. Otherwise it's `INVALID_OUTPUT`, with the parser's error, retried with the error added to the context: "Your previous answer failed validation: …" (tested).
4. After 3 attempts, the step fails, and the coordinator records it as inconclusive.

Structured output (`output_config.format`) makes this rare on Claude, but validation stays.

### Q10.21 A model returns valid JSON, but the contents violate the application's expected schema. How should the response be validated?

**Frame.** How is valid JSON with wrong contents caught?

**Answer.** Every structured answer is validated against the skill's **Pydantic contract** (`output_schema_name`; Artifact 9), whatever the provider promises:

- wrong or missing fields, wrong types
- unknown fields (forbidden)
- out-of-range values
- the contract's own rules: 2–5 hypotheses, no action when inconclusive, leading index in range

Errors are returned as field paths (`confidence: Field required`), fed back for the retry, and stored. After 3 failures the step fails (tested). Citation checks follow in the skill (Artifact 9).

### Q10.22 One provider does not support structured output or tool calling required by an agent skill. How should the gateway handle capability incompatibility?

**Frame.** What if the provider can't do what the skill needs?

**Answer.**

- **Each skill declares** `required_capabilities`. Each adapter declares what it supports.
- **The gateway refuses before calling:** `CAPABILITY_UNSUPPORTED`, no tokens spent (tested with the unverified Tinker adapter).
- **No silent downgrade.** For example, it won't drop the schema and hope for JSON, because that changes behaviour without anyone deciding to.
- **Capabilities are "verified", not "advertised":** an adapter declares a capability only after our conformance suite passes against it.

### Q10.23 An investigation must be reproducible months later. Which model and checkpoint identity information should be preserved?

**Frame.** What identity is kept per call?

**Answer.** `ModelIdentity`, stored with each model attempt:

- the provider
- the **exact model ID returned in the response**, not just the alias
- the checkpoint path (Tinker)
- the adapter version
- the prompt version and its fingerprint
- the output schema fingerprint
- the context fingerprint (with the context manifest, Q10.64)
- the provider's request ID

Temperature 0 is used for repeatability, but outputs can still differ slightly, so the stored input and output are the record. Re-running is a comparison, not a guarantee.

### Q10.24 The organization wants to compare multiple providers before selecting one. Which capabilities and limitations should be included in a provider comparison?

**Frame.** What do we compare?

**Answer.** The **provider capability matrix** (10.1): structured output, strict tool use, streaming, usage reporting, pinned versions and deprecation notice, context window, maximum output, rate limits, data retention and training terms, region, price per input and output token, caching, availability record, and maturity. Then the **same evaluation suite** through the same gateway: accuracy, false-established rate, schema validity, refusal rate on incident data, latency at p95, and cost per correct diagnosis.

### Q10.25 The model charges according to the amount of text it processes and generates. What unit should the platform use to measure this consumption?

**Frame.** What unit measures usage?

**Answer.** **Tokens**, as reported by the provider, converted to money with the configured price per million tokens. Money is stored as whole millionths of the currency unit (Artifact 7), so there are no rounding errors.

### Q10.26 Two model requests contain different amounts of input and generated output. How should their token usage be distinguished?

**Frame.** How is usage split?

**Answer.** Four counters, priced separately: **input**, **output** (usually several times the input price), **cache read** and **cache write**. A large-context, short-answer call is input-heavy; a long answer is output-heavy. Both the per-incident token caps (200,000 input, 20,000 output; Artifact 4) and the cost cap use the split.

### Q10.27 An investigation makes 20 model calls and produces an unexpectedly large bill. How should the platform estimate and control inference expenditure?

**Frame.** How do we predict and stop runaway cost?

**Answer.**

- **Estimate before each call:** input tokens from the request size (about 3 characters per token, deliberately pessimistic), plus the full output limit, at configured prices.
- **Reserve that amount** against the incident's budget; refuse the call if it doesn't fit (Q10.29).
- **Settle with actual usage** after the call.
- **Caps:** 30 calls, token caps, a cost cap per incident; daily and monthly ceilings with alerts (Artifact 4).
- **Report:** cost per incident and per correct diagnosis (Artifact 9).

20 calls can't produce a surprise, because each was reserved within a cap before it was made.

### Q10.28 An investigation requires multiple model calls, but the organization wants to enforce a maximum expenditure. How should a per-incident budget be established?

**Frame.** How is the per-incident budget set?

**Knowns.** Artifact 2: model spend about 10% of the engineer time saved.

**Assumptions (provisional).**

- A change-caused investigation saves about 30 minutes of engineer time. *Owner: SRE lead. Validated when: pilot incidents are timed.*

**Answer.** **Budget = 10% × engineer time saved × loaded hourly cost**, then capped by the token and call caps in Artifact 4. It is set in configuration per incident category, recorded on each incident's `incident_budgets` row at creation, and reviewed after the pilot against real cost per incident.

### Q10.29 The system starts several expensive model requests simultaneously without checking available budget. How could cost reservations prevent this?

**Frame.** How do reservations stop parallel overspending?

**Answer.**

- **Reserve before sending:** each call takes its maximum cost out of the budget under a lock, in the database a row lock (`BudgetLedger.reserve`).
- **Parallel calls each reserve,** so together they can never exceed the budget: the one that doesn't fit is refused before it is sent (tested).
- **Settle afterwards:** the reservation is replaced by actual cost.
- **Release only if the call provably never left the process,** for example when no concurrency slot came free before the deadline.

### Q10.30 Hundreds of incidents arrive at once and overload the model provider. How should the platform control concurrent inference requests?

**Frame.** How do we protect the provider and ourselves?

**Answer.** Layered limits:

1. **Incident level:** at most 5 investigations run at once; the rest queue by severity (Artifact 4).
2. **Gateway global slots:** at most 8 model calls in flight per provider (configuration).
3. **Per-incident slots:** at most 2 (Q10.31).
4. **Provider rate limits** (429) are retried with backoff, within the deadline, and never multiplied (Q10.37).

Hundreds of incidents therefore produce a queue, not hundreds of simultaneous calls.

### Q10.31 One incident launches ten parallel reasoning operations while another launches only one. How should per-incident concurrency be limited?

**Frame.** How do we keep one incident from taking every slot?

**Answer.** A **per-incident semaphore of 2**, taken before a global slot, so one incident can hold at most 2 of the 8. Tested: 6 parallel calls for one incident never exceed 2 at a time. The coordinator is sequential anyway (Artifact 9), so 2 leaves room only for small parallel steps.

### Q10.32 An investigation continues making requests after its useful execution window has expired. How should a deadline be enforced?

**Frame.** How is the deadline enforced?

**Answer.**

- **Every ModelRequest carries the absolute deadline:** investigation start + 15 minutes.
- **The gateway checks it** before each attempt (`DEADLINE_PASSED`, no call; tested), before waiting for a slot, and before each backoff.
- **The call timeout is cut** to the time remaining.
- **The coordinator also stops** at the limit (Artifact 9), and the workflow's activity timeouts bound everything (Artifact 8).

### Q10.33 A model request times out after 30 seconds, but the overall incident investigation is allowed to run for 10 minutes. How should the two time limits be distinguished?

**Frame.** How do the per-call and per-investigation limits relate?

**Answer.**

- **Per-call timeout** (`call_timeout_s`, ≤ 60 s): how long one request may take. Hitting it ends one attempt, which may be retried.
- **Investigation deadline** (`deadline_epoch_s`): when all work must stop. Hitting it ends the step and the loop.

The effective timeout of each call is **the smaller of the two:** min(call_timeout, deadline − now). A retry is only scheduled if its backoff ends before the deadline.

### Q10.34 The agent, critic and revision loop each generate additional tokens. How should cumulative token consumption be tracked?

**Frame.** How is total usage tracked?

**Answer.**

- **One ledger per incident** (`incident_budgets`): calls, input tokens, output tokens, reserved and spent cost.
- **Every attempt by every skill** (hypothesise, diagnose, critique, revisions, retries) reserves and settles against it.
- **Each attempt is also a** **`model_attempts`** **row,** with task and attempt number, so totals can be broken down by skill.

The coordinator's model-call count and the ledger agree, because both count attempts.

### Q10.35 A provider fails to report usage after completing a request. How should the platform account for uncertain expenditure?

**Frame.** What if usage isn't reported?

**Answer.**

- **Mark the usage as estimated** (`Usage.reported = false`).
- **Charge the full reservation,** the pessimistic maximum, to the incident (tested).
- **Add it to** **`pending_reconcile`****:** a daily job compares these with the provider's usage records or billing export, and corrects the ledger.
- **Count it:** frequent missing usage from a provider is an alert.

### Q10.36 The provider has consumed tokens, but the application has not received the usage information. Why should the system avoid immediately releasing the reserved budget?

**Frame.** Why not release the reservation?

**Answer.** Because **the tokens were probably spent**: the request reached the provider, and it may have generated a full answer we never received. Releasing the reservation would:

- make the budget look larger than it is, so further calls could overspend the cap
- under-report cost
- let a stream of timeouts drain money while the budget appears untouched

So unknown usage is charged at the maximum, and reconciled later (tested). Only a call that provably never left the process is released.

### Q10.37 Several components retry model calls independently, causing costs to multiply. How should the system control retry amplification?

**Frame.** How do we stop retries multiplying?

**Answer.** **Only the gateway retries model calls** (Artifact 8, ADR-27):

- the provider SDK's own retries are turned off
- the Temporal activity for each model skill has 1 attempt
- the gateway allows 3 attempts per step, with backoff, within the deadline
- every attempt reserves budget, so retries are visible in cost
- the coordinator's revision loop is separate, and bounded (Artifact 9)

At most 3 provider calls per step, never 27.

### Q10.38 A request fails because of a permanent authentication error. Why should this type of failure not be retried?

**Frame.** Why not retry an authentication failure?

**Answer.** Retrying changes nothing: the key is wrong, expired or revoked, or the permission is missing. Retrying wastes time inside the deadline, adds load, may trip the provider's abuse protection, and hides an outage that needs a person. So `401` and `403` are `AUTH_ERROR`, and `400`, `404`, `413` and `422` are `BAD_REQUEST`. None are retried (tested). The platform alerts immediately, and investigations degrade to evidence only until it's fixed.

### Q10.39 One model provider becomes unavailable, and the system automatically sends every request to another provider. What risks would this introduce, and why might automatic cross-provider fallback remain disabled initially?

**Frame.** Why not fail over to another provider automatically?

**Answer.**

- **Different behaviour, unmeasured:** prompts, schemas and refusal patterns were evaluated on one model. Another model's accuracy and false-established rate are unknown.
- **Different capabilities:** the fallback may lack structured output, so validation failures spike.
- **Data and contract risk:** incident data would go to a provider with different terms, possibly one not approved.
- **Cost surprise:** different prices, and sudden full traffic.
- **Reproducibility:** one investigation would mix models.
- **It can amplify an outage:** a burst of traffic to the second provider hits its rate limits too.

So `allow_cross_provider_fallback = false` (ADR-38). The agreed outage behaviour is Artifact 2's: **evidence and timeline only, no fix proposals**. A second provider is added only after it passes the same evaluation, as a recorded decision, and with fallback per skill, not global.

### Q10.40 The agent investigates an unfamiliar application but does not know the organization's operational procedures. How should internal runbooks be made available during investigation?

**Frame.** How does the agent get the organisation's runbooks?

**Answer.** Through the **Knowledge Retriever** (Artifact 6). It searches an index of runbooks and past incident reviews, filtered to the incident's service and the platform's permissions. It returns the top passages as **evidence records** of kind `runbook`, each with document ID, version, heading path and exact text span. They enter the context like any other evidence (10.11), and can be cited.

### Q10.41 The model has no access to relevant external knowledge and produces answers using only its existing parameters. How could an external knowledge retrieval mechanism improve its responses?

**Frame.** Why retrieve, rather than rely on the model?

**Answer.** The model knows public, general practice. It **doesn't know** our services, our configuration keys, our error codes, our runbooks or what fixed last month's incident. Retrieval adds that at call time, so:

- answers follow our procedures
- claims can cite a specific runbook passage
- knowledge updates when a runbook changes, without retraining

Retrieved passages are guidance, not proof: the incident's own evidence still has to support the diagnosis.

### Q10.42 The organization has hundreds of operational documents that need to become searchable. How should the ingestion process work?

**Frame.** How are documents ingested?

**Answer.**

1. **Source:** runbooks from the repository or wiki, and past incident reviews, each read with its version (commit or revision ID), owner, services, permitted groups and last-reviewed date.
2. **Redact** secrets and personal data (Artifact 5).
3. **Chunk** with headings kept (Q10.44).
4. **Store** documents and chunks in PostgreSQL, keyed by `doc_id@version#ordinal`, each with its text span and fingerprint.
5. **Build a new immutable index revision** (Q10.52) on any change, nightly at most, and on demand after an edit.
6. **Evaluate** the new revision against the retrieval test set (10.13) before activating it.
7. **Activate:** new investigations use it; old ones keep the revision they recorded.

### Q10.43 Documents contain several topics and sections. How should they be divided into smaller retrievable units?

**Frame.** How are documents split?

**Answer.** By **structure first, size second**:

- split at headings, so each section is about one thing
- within a long section, group paragraphs up to about 220 words
- never split a code block, a list, or mid-sentence
- prefix every chunk with its **heading path**, such as "Checkout runbook › HTTP 500 after deploy › Revert config"
- record each chunk's exact character span in the source

Tested: the "Revert config" chunk keeps its full path and its whole code block.

### Q10.44 Naive fixed-size chunking separates important technical instructions from their headings. How could heading-aware chunking preserve document structure?

**Frame.** Why keep headings with chunks?

**Answer.** A fixed 500-character cut can leave "Run `deployctl config revert …`" in a chunk without the heading that says **when** to run it ("HTTP 500 after deploy"). Heading-aware chunking fixes that:

- each chunk starts at a heading boundary, and carries its **full heading path**, so its context is never lost
- the heading words are searchable with the body, so "500 after deploy" finds the steps under that heading
- a step is never cut away from the section it belongs to

### Q10.45 Engineers search for exact error codes, function names and configuration identifiers. Which lexical retrieval method would be appropriate?

**Frame.** What finds exact identifiers?

**Answer.** **BM25** keyword ranking, with a tokenizer that **keeps identifiers whole** (`err_conn_reset`, `payments.timeout_ms`) **and** adds their parts (`timeout_ms`, `timeout`, `ms`). A search for the exact code matches strongly, and a partial one still matches. Tested: the identifier query ranks the right runbook first.

### Q10.46 A document contains a rare error identifier that appears in only a few documents. How should retrieval account for the importance of rare terms?

**Frame.** How do rare terms count more?

**Answer.** Through BM25's **inverse document frequency**: idf = ln(1 + (N − df + 0.5) / (df + 0.5)). A term in few chunks (`db-failover-7`) gets a high weight, and a term in many chunks (`checkout`) a low one (tested). BM25 also caps the gain from repeating a term (`k1 = 1.2`), and normalises for chunk length (`b = 0.75`).

### Q10.47 Keyword matching fails when the user describes a problem using different terminology from the documentation. How could vector embeddings improve retrieval?

**Frame.** How would embeddings help?

**Answer.** An embedding model turns text into a vector, so texts with similar meaning sit close together. "Checkout is throwing server errors after release" can then match a runbook titled "HTTP 500 after deploy", although they share few words. Chunks are embedded at ingestion; the query is embedded at search time; the nearest chunks are returned.

### Q10.48 Engineers need to search documents based on conceptual similarity rather than exact words. Which search mechanism could support this?

**Frame.** What does meaning-based search?

**Answer.** **Dense vector search:** nearest-neighbour search over chunk embeddings (cosine similarity), stored in a vector index. The simplest option for us would be the `pgvector` extension in PostgreSQL, which needs no new system. It is not in the first version (Q10.50).

### Q10.49 Keyword retrieval and semantic retrieval each miss relevant documents in different situations. How could their results be combined?

**Frame.** How do we combine keyword and meaning search?

**Answer.** **Hybrid retrieval:** run both, then merge with **reciprocal rank fusion**. Each chunk scores `Σ 1 / (60 + rank)` across the two lists. This rewards chunks both methods rank well, needs no score calibration, and keeps exact-identifier hits from BM25 while adding paraphrase hits. Optionally a re-ranker reorders the top 20. Adopted only if evaluation shows BM25 alone misses relevant passages (Q10.50).

### Q10.50 The initial platform needs reliable retrieval of exact technical identifiers without introducing unnecessary infrastructure. Why might BM25 be a reasonable starting point?

**Frame.** Why start with BM25?

**Answer.**

- **Our queries are mostly exact terms:** alert names, error codes, configuration keys and service names copied from evidence. BM25 is strongest at exactly that.
- **No new system:** chunks live in PostgreSQL; BM25 runs in the worker over a few thousand chunks in milliseconds. There is no embedding model, vector store or extra provider.
- **Explainable and debuggable:** a hit can be traced to the matched terms.
- **Deterministic and reproducible:** the same revision and query always give the same results.
- **Measured before replaced:** if recall falls short on paraphrased queries, add embeddings with hybrid fusion (Q10.49) as a recorded decision.

### Q10.51 The retrieval system returns ten documents, but engineers do not know whether they are relevant. How should retrieval quality be measured?

**Frame.** How do we measure retrieval?

**Answer.** Against a **labelled query set:** queries taken from real alerts and evidence, each with the chunks an SRE marks relevant. Metrics (implemented and tested):

- **Recall@5:** share of relevant chunks in the top 5 (target ≥ 0.9)
- **MRR:** how high the first relevant chunk ranks (target ≥ 0.7)
- **nDCG@5:** ranking quality (target ≥ 0.8)
- **Wrong-service and wrong-permission hits:** must be 0

Run on every new index revision before activation, and on tokenizer or chunking changes. In production, also track how often cited runbook passages appear in accepted diagnoses.

### Q10.52 Runbooks change over time, but an old investigation needs to be reproduced. How should immutable retrieval index revisions be maintained?

**Frame.** How do we keep old search results reproducible?

**Answer.**

- **An index revision** is built from a fixed set of document versions. Its ID is a hash of `doc_id@version` for every live document.
- **Revisions are never modified.** Any edit, addition or deletion builds a new one (tested: deleting a document gives a new revision ID, while the old revision still returns its results).
- **Chunks are stored by version** (`doc_id@version#n`), so old chunks remain.
- **Each investigation records the revision ID** it searched, in the context manifest.
- **Retention:** keep every revision referenced by an incident for the incident retention period (1 year).

### Q10.53 The agent cites a runbook paragraph, but the original document has changed. How should the source document version and exact text span be preserved?

**Frame.** How is a runbook citation pinned?

**Answer.** Each runbook hit becomes an evidence record carrying:

- document ID and **version**
- **chunk ID**
- **character start and end** in that version
- the chunk's **fingerprint**
- the heading path

The text is stored with the evidence. Later edits create new versions, so the cited text is still exactly what the agent saw (tested: the span maps back to the source text). The display shows "version v3; the current version differs" when it has changed.

### Q10.54 An old runbook describes a procedure that is no longer valid. How should stale documentation be detected?

**Frame.** How do we find stale runbooks?

**Answer.**

- **Review age:** a document not reviewed by its owner within 180 days is marked **stale**.
- **Deprecated flag:** set by owners.
- **Changed since review:** the service it describes was deployed with relevant changes after the last review.
- **Stale chunks are excluded from search by default** (tested). They can be included explicitly, and are then labelled stale in the context.
- **A weekly report** (`stale_report`) goes to owners (tested).
- **Feedback:** a runbook passage cited in a diagnosis that was later rejected is flagged for review.

### Q10.55 An administrator deletes a document, but the retrieval system continues returning its content. How should deleted documents be handled?

**Frame.** How do deletions take effect?

**Answer.**

- **Deletion builds a new index revision without the document,** activated straight away. New searches no longer return it (tested).
- **Old revisions keep it,** only for reproducing past investigations, readable by incident reviewers, never searched by new investigations.
- **If the deletion was for legal or safety reasons** (for example, it contained a secret), the document's chunks are purged from every revision, and the affected evidence records are marked "content removed".

### Q10.56 An engineer can access only a subset of organizational runbooks. How should retrieval enforce document-level permissions?

**Frame.** How do runbook permissions apply?

**Answer.**

- **Each document carries its permitted identity groups,** copied from the source system at ingestion.
- **Search filters by permission before ranking,** never after (tested: a security-only runbook never appears for an SRE search).
- **The platform's own searches** use the incident service's responder groups.
- **Engineers viewing an incident** see a runbook citation's text only if they may read that document; otherwise "restricted runbook (owner: …)".
- **Permissions are re-synced** at each index build.

### Q10.57 The model receives thousands of log lines, several metrics and multiple runbooks. How should the application decide which information belongs in its context?

**Frame.** What goes into the context?

**Answer.** **Context assembly** (`context.assemble`) picks from canonical records under a token budget, by priority:

- **0:** computed facts: the timeline, version split, changed values
- **1:** contradictions, both sides
- **2:** gaps (missing sources)
- **3:** evidence cited by the current draft (on revision)
- **10–15:** other evidence, most decisive kind first (test result, trace, error group, metric, change, dependency, log, alert)
- **40+:** up to 3 runbook passages

Thousands of log lines never go in. The evidence collector already reduces them to error templates with counts, and assembly merges repeats (Q10.60). Tested: under an 800-token budget, low-priority items are dropped, and the drops are recorded in the manifest.

### Q10.58 The agent needs to combine live application telemetry with historical operational documentation. How should these different information sources be assembled?

**Frame.** How do live data and documentation sit together?

**Answer.** As **separately labelled blocks** in one data section, each with its kind and reference:

- `[metric from monitoring, window 09:00–09:15] …` with ref `evidence:<id>`
- `[runbook Checkout › HTTP 500 after deploy, version v3] …` with ref `runbook:<chunk>`

The instructions say what each kind is good for: live evidence shows what is happening; runbooks show what usually helps. A claim about **this** incident must cite live evidence, never only a runbook. Runbooks come last in priority, so they never push out incident evidence.

### Q10.59 Two evidence sources contradict each other. How should the context preserve this disagreement rather than silently removing it?

**Frame.** How do contradictions survive assembly?

**Answer.**

- **Contradictions are first-class items** at priority 1: "These sources disagree: A vs B", with both references and a short note. They are always included, regardless of budget (tested).
- **Both sides' evidence is kept,** never deduplicated into one.
- **The diagnosis schema** has `contradicting_evidence_ids`, and **the critic** blocks a diagnosis that ignores strong contradicting evidence (Artifact 9).

### Q10.60 The context becomes too large for the model's input limit. How should information be compressed while preserving important details?

**Frame.** How do we shrink context without losing what matters?

**Answer.** In this order:

1. **Reduce at collection:** log lines become error templates with counts and first and last seen; metrics become summary points around the change; traces become the failing span and its error.
2. **Merge repeats:** identical log templates become one item, "seen 4 times", **listing every source reference** (tested).
3. **Truncate long items** from priority 3 onwards, with "[truncated; full record: evidence:\<id>]".
4. **Drop by priority** from the bottom, recording each drop in the manifest.
5. **Never drop** facts, contradictions, gaps or cited evidence. If those alone exceed the budget, assembly raises an error, and the coordinator narrows the scope rather than silently cutting.

There is no model-written summarisation of evidence in the first version: compression is deterministic code.

### Q10.61 An investigation produces more information than the available token budget can accommodate. How should the system prioritize evidence?

**Frame.** What wins when there's too much?

**Answer.** By **decisiveness for the question being asked**, then recency:

1. what the current draft cites, which the critic must check
2. mechanism evidence: test results, traces, error groups, version-split metrics
3. change records and dependency health, which separate deployment from non-deployment causes
4. logs, then alerts
5. past incidents and runbooks

The per-call input cap is 16,000 tokens (Artifact 4); assembly targets about 12,000, leaving room for instructions and schema.

### Q10.62 A context summarizer removes source references while compressing evidence. How should provenance be preserved?

**Frame.** How do we keep references through compression?

**Answer.**

- **Every context item carries** **`refs`****:** the list of every source it stands for (tested: all items have refs, and a merged item lists all 4).
- **Truncated items name their full record.**
- **The manifest lists each included item's reference and fingerprint,** plus everything excluded and why.
- **Any future model-based summariser** must output a schema in which every sentence carries refs, validated like any other output. A summary sentence without refs is rejected.

### Q10.63 The agent generates a conversational summary of an incident, but engineers need to verify the original observations. Why should summaries not replace canonical evidence records?

**Frame.** Why keep the originals?

**Answer.**

- **A summary is an interpretation:** it may be wrong, may drop the detail that matters, and may carry injected text. It isn't an observation.
- **Verification needs the original:** the exact log line, metric values and query, so an engineer can check or rerun it.
- **Audit and disputes** (Artifact 5) need what was actually seen, with fingerprints.
- **Later revisions and critics** must reason from facts, not from an earlier model's paraphrase.

So evidence records are canonical and immutable. Summaries are derived, labelled as such, and always link back to them.

### Q10.64 An engineer wants to reproduce a diagnosis from six months ago. Which prompt, model, context and retrieval versions should be preserved?

**Frame.** What must be kept to reproduce a diagnosis?

**Answer.**

| **What** | **Where** |
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

Replay = same prompt version + same context + same model, then compare outputs. If the model ID has been retired, the stored output is the record, and a re-run on a current model is a labelled comparison.
