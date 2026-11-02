# AI SRE Platform: Worksheet Docs

This folder is the artifact filing. The code the team builds on is in `src/`, `db/`, `ops/` and `tests/` at the repo root (ADR-67), and each layer's `files/` folder keeps the copy that artifact was written against.

One folder per worksheet layer. Each holds the team's question answers (`conversation.md`), the artifact that layer's sheet requires, and the files used in that layer.

Start with a layer's `conversation.md` to read the reasoning, then its artifact for the consolidated result.

## Layers

Answers are complete for Layers 1–12. Artifacts have had their gaps and conflicts resolved through the proposed [decision register](decisions.md), and all of it is pending team sign-off. Layer 13's answers are a Claude draft for the team to review, not team answers.

| # | Layer | Central question | Qs | Conversation | Artifact | Sections complete | Files |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Problem Discovery | What problem are we actually solving? | 22 | [conversation](layers/layer-01-problem-discovery/conversation.md) | [Product Requirements Document](layers/layer-01-problem-discovery/artifact-01-product-requirements-document.md) | 6 of 7 | — |
| 2 | Scope and Constraints | What are we allowed to build? | 23 | [conversation](layers/layer-02-scope-and-constraints/conversation.md) | [Scope and Constraints Document](layers/layer-02-scope-and-constraints/artifact-02-scope-and-constraints-document.md) | 5 of 7 | — |
| 3 | SRE Fundamentals | How does an incident actually happen? | 37 | [conversation](layers/layer-03-sre-fundamentals/conversation.md) | [Incident and Evidence Catalog](layers/layer-03-sre-fundamentals/artifact-03-incident-and-evidence-catalog.md) | 8 of 10 | 9 |
| 4 | Functional and Non-Functional Requirements | What must the system do, and how well must it do it? | 34 | [conversation](layers/layer-04-requirements/conversation.md) | [Requirements Specification](layers/layer-04-requirements/artifact-04-requirements-specification.md) | 6 of 7 | — |
| 5 | Threat Modeling | What can go wrong? | 28 | [conversation](layers/layer-05-threat-modeling/conversation.md) | [Threat Model](layers/layer-05-threat-modeling/artifact-05-threat-model.md) | 7 of 8 | — |
| 6 | System Architecture | What components do we need? | 39 | [conversation](layers/layer-06-system-architecture/conversation.md) | [System Architecture Document](layers/layer-06-system-architecture/artifact-06-system-architecture-document.md) | 8 of 8 | — |
| 7 | Data Architecture, APIs and Event Design | How is the incident lifecycle represented as data? | 64 | [conversation](layers/layer-07-data-apis-events/conversation.md) | [Data and Interface Specification](layers/layer-07-data-apis-events/artifact-07-data-and-interface-specification.md) | 9 of 9 | 5 |
| 8 | Distributed Workflows and State Machines | How does long-running work survive failure? | 48 | [conversation](layers/layer-08-workflows-state-machines/conversation.md) | [Workflow Specification](layers/layer-08-workflows-state-machines/artifact-08-workflow-specification.md) | 10 of 10 | 4 |
| 9 | Agent Architecture and Engineering Reasoning | How does the system investigate like an engineer? | 53 | [conversation](layers/layer-09-agent-architecture/conversation.md) | [Agent Design Specification](layers/layer-09-agent-architecture/artifact-09-agent-design-specification.md) | 10 of 11 | 5 |
| 10 | Inference Engineering and Knowledge Retrieval | How do we run models reliably and give them the right information? | 64 | [conversation](layers/layer-10-inference-retrieval/conversation.md) | [Inference and Retrieval Specification](layers/layer-10-inference-retrieval/artifact-10-inference-and-retrieval-specification.md) | 9 of 13 | 6 |
| 11 | Patch Generation, Sandboxing, Approval and Execution | How does a recommendation become a controlled, verified change? | 92 | [conversation](layers/layer-11-safe-remediation/conversation.md) | [Safe Remediation Specification](layers/layer-11-safe-remediation/artifact-11-safe-remediation-specification.md) | 12 of 13 | 6 |
| 12 | Reliability, Observability and Evaluation | How do we establish that it works under realistic failure? | 83 | [conversation](layers/layer-12-reliability-evaluation/conversation.md) | [Reliability and Evaluation Specification](layers/layer-12-reliability-evaluation/artifact-12-reliability-and-evaluation-specification.md) | 9 of 11 | 5 |
| 13 | Implementation Planning and Delivery | How do we build it? | 58 | [conversation](layers/layer-13-implementation-planning/conversation.md) | [Complete Implementation Plan](layers/layer-13-implementation-planning/artifact-13-complete-implementation-plan.md) | 10 of 12 | — |

**Layers 1–12: 99 of 114 sections are complete.** The other 15 are marked `**Needs the team:**` or `**Needs measurement:**`, because each depends on something only the team can supply. Every former gap and conflict appears in its artifact's "Decisions and conflicts" table, with how it was resolved.

## Start here for decisions

- [decisions.md](decisions.md): the decision register, which also says which document wins when two disagree
- [code-changes-required.md](code-changes-required.md): test results, what the code now does, and what remains
- [tests/security/](../tests/security/README.md): the 40 security tests

## Reference

Both are the original documents, with a note at the top listing what is amended.

- [System Design](reference/system-design.md)
- [Technical specification](reference/technical-specification.md)

## Where things came from

| Output | Source |
| --- | --- |
| `conversation.md` headers | Layer map and method pages of `AI_SRE_Engineering_Worksheet_1.pdf` |
| `conversation.md` questions and answers | Main tab of `worksheet - room 7.docx`, unchanged |
| `artifact-*.md` | Compiled from the answers, the code tabs and the reference docs. Sections 6.4–6.6 and 7.4–7.9 and ADR-19 to ADR-24 are the team's own artifact text |
| `files/` | One code tab each from Room 7, since changed to implement the decisions. Originals are in git commit `013bcf4` |
| `layer-06`, `layer-07`, `layer-09` `assets/` | Images from Room 7 |
| `layer-07` `files/schema.mmd` | `schema.mmd`, identical to its Room 7 tab |
| `reference/` | The two `.docx` specs, identical to their Room 7 tabs apart from one code block, plus the amendment note at the top |
| `layer-07` `files/schema.sql`, `schema-tests.sql` | New and proposed (ADR-64). Tested on PostgreSQL 17.11: 182 of 182 checks pass. The spec targets 16, which was not tested |
| `layer-03` `files/reference-incident/` | New and proposed: a synthetic `orders-api` incident. No real data |

**File names.** Most come from the code itself: imports, `Run:` lines and `alerts.yml` in `test_layer12.py`. Two are inferred, because nothing names them: `schema-tables.sql` and `contracts.py`.

**Left out.** The team phone list in Room 7 is personal data and is not in this repo. 

**Not read.** The shared Claude conversation needs sign-in, so it was not used. It is linked from each conversation log.

## Open items

### Needs the team

Nothing below can be settled by a document. Each has an owner in the [register](decisions.md#what-no-decision-can-settle).

- **Sign-off:** every ADR is Proposed, and every artifact's "Reviewed by" and "Date" are `TBD`.
- **Names:** owners are roles. The sources name no people.
- **Conversation logs:** the agent used per conversation and any Claude vs ChatGPT disagreements. No source records them.
- **Real-world facts:** release process lookup by key, on-call rotas, provider terms, actual budget.
- **Measurements:** baselines, real alert samples, peak concurrency, personal data in logs.
- **To build:** the frozen evaluation suite and the retrieval query set are specified, not built.

### Placeholder values

ADR-56's money values, including the $500 monthly ceiling and $2.00 per incident, exist so limits can be built and tested. They are not a budget. The Engineering manager confirms them before the pilot.

### Not yet proven

- The code in `files/` implements these decisions and its tests pass, but it is a set of in-memory reference modules. There is no running application, API or database connection yet. See [what remains](code-changes-required.md#what-remains).
- 21 security test functions are skipped, because they need a running API, gVisor, evidence adapters, or the review or notifier systems.
- The sandbox has not been proven under gVisor, and `schema.sql` has been tested on PostgreSQL 17.11, not the 16 the spec targets.

### Layer 13

Drafted, and it needs the most review. Tool choices (ruff, mypy, GitHub Actions), the milestone and task owners, and the M0 and M5 criteria are proposals with little source behind them. No ChatGPT comparison has been done. Artifact 13 has 81 tasks across M0–M5, with every active requirement, security test and remaining code item assigned to one.

### Source quirks kept as written

- Q1.18 says "measure-able"; the worksheet says "measurable".
- The Q2.23 reasoning stops mid-sentence.
- Question headings keep Room 7's punctuation, such as the colon in `Q1.2:`.
