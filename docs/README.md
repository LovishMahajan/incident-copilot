# AI SRE Platform: Worksheet Docs

One folder per worksheet layer. Each holds the team's question answers (`conversation.md`), the artifact that layer's sheet requires, and the files used in that layer.

Start with a layer's `conversation.md` to read the reasoning, then its artifact for the consolidated result.

## Layers

Answers are complete for Layers 1–12. Artifacts are drafts compiled from those answers and are pending team review.

| # | Layer | Central question | Qs | Conversation | Artifact | Sections complete | Files |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Problem Discovery | What problem are we actually solving? | 22 | [conversation](layers/layer-01-problem-discovery/conversation.md) | [Product Requirements Document](layers/layer-01-problem-discovery/artifact-01-product-requirements-document.md) | 5 of 7 | — |
| 2 | Scope and Constraints | What are we allowed to build? | 23 | [conversation](layers/layer-02-scope-and-constraints/conversation.md) | [Scope and Constraints Document](layers/layer-02-scope-and-constraints/artifact-02-scope-and-constraints-document.md) | 3 of 7 | — |
| 3 | SRE Fundamentals | How does an incident actually happen? | 37 | [conversation](layers/layer-03-sre-fundamentals/conversation.md) | [Incident and Evidence Catalog](layers/layer-03-sre-fundamentals/artifact-03-incident-and-evidence-catalog.md) | 1 of 10 | — |
| 4 | Functional and Non-Functional Requirements | What must the system do, and how well must it do it? | 34 | [conversation](layers/layer-04-requirements/conversation.md) | [Requirements Specification](layers/layer-04-requirements/artifact-04-requirements-specification.md) | 5 of 7 | — |
| 5 | Threat Modeling | What can go wrong? | 28 | [conversation](layers/layer-05-threat-modeling/conversation.md) | [Threat Model](layers/layer-05-threat-modeling/artifact-05-threat-model.md) | 5 of 8 | — |
| 6 | System Architecture | What components do we need? | 39 | [conversation](layers/layer-06-system-architecture/conversation.md) | [System Architecture Document](layers/layer-06-system-architecture/artifact-06-system-architecture-document.md) | 8 of 8 | — |
| 7 | Data Architecture, APIs and Event Design | How is the incident lifecycle represented as data? | 64 | [conversation](layers/layer-07-data-apis-events/conversation.md) | [Data and Interface Specification](layers/layer-07-data-apis-events/artifact-07-data-and-interface-specification.md) | 8 of 9 | 3 |
| 8 | Distributed Workflows and State Machines | How does long-running work survive failure? | 48 | [conversation](layers/layer-08-workflows-state-machines/conversation.md) | [Workflow Specification](layers/layer-08-workflows-state-machines/artifact-08-workflow-specification.md) | 7 of 10 | 4 |
| 9 | Agent Architecture and Engineering Reasoning | How does the system investigate like an engineer? | 53 | [conversation](layers/layer-09-agent-architecture/conversation.md) | [Agent Design Specification](layers/layer-09-agent-architecture/artifact-09-agent-design-specification.md) | 7 of 11 | 5 |
| 10 | Inference Engineering and Knowledge Retrieval | How do we run models reliably and give them the right information? | 64 | [conversation](layers/layer-10-inference-retrieval/conversation.md) | [Inference and Retrieval Specification](layers/layer-10-inference-retrieval/artifact-10-inference-and-retrieval-specification.md) | 8 of 13 | 6 |
| 11 | Patch Generation, Sandboxing, Approval and Execution | How does a recommendation become a controlled, verified change? | 92 | [conversation](layers/layer-11-safe-remediation/conversation.md) | [Safe Remediation Specification](layers/layer-11-safe-remediation/artifact-11-safe-remediation-specification.md) | 10 of 13 | 6 |
| 12 | Reliability, Observability and Evaluation | How do we establish that it works under realistic failure? | 83 | [conversation](layers/layer-12-reliability-evaluation/conversation.md) | [Reliability and Evaluation Specification](layers/layer-12-reliability-evaluation/artifact-12-reliability-and-evaluation-specification.md) | 5 of 11 | 5 |
| 13 | Implementation Planning and Delivery | How do we build it? | 58 | Not started | Complete Implementation Plan | — | — |

"Sections complete" counts sections with no open `**Gap:**`. Each artifact's own "Decisions and conflicts" section lists what needs a team decision.

## Reference

- [System Design](reference/system-design.md)
- [Technical specification](reference/technical-specification.md)

## Where things came from

| Output | Source |
| --- | --- |
| `conversation.md` headers | Layer map and method pages of `AI_SRE_Engineering_Worksheet_1.pdf` |
| `conversation.md` questions and answers | Main tab of `worksheet - room 7.docx`, unchanged |
| `artifact-*.md` | Compiled from the answers, the code tabs and the reference docs. Sections 6.4–6.6 and 7.4–7.9 and ADR-19 to ADR-24 are the team's own artifact text |
| `files/` | One code tab each from Room 7, copied verbatim |
| `layer-06`, `layer-07`, `layer-09` `assets/` | Images from Room 7 |
| `layer-07` `files/schema.mmd` | `schema.mmd`, identical to its Room 7 tab |
| `reference/` | The two `.docx` specs, identical to their Room 7 tabs apart from one code block |

**File names.** Most come from the code itself: imports, `Run:` lines and `alerts.yml` in `test_layer12.py`. Two are inferred, because nothing names them: `schema-tables.sql` and `contracts.py`.

**Left out.** The team phone list in Room 7 is personal data and is not in this repo. The Layer 13 questions are not included because that layer has not been started.

**Not read.** The shared Claude conversation needs sign-in, so it was not used. It is linked from each conversation log.

## Open items

### Needs the team

- **Conversation logs:** the agent used per conversation and every Claude vs ChatGPT disagreement are `TBD`. No source records them.
- **Artifact review:** every artifact has "Reviewed by" and "Date" as `TBD`, and the consistency acceptance check is open.
- **Artifacts 1–5:** no written artifact existed in Room 7, so these are assembled from answers. Use case numbers UC1–UC3, UC5 and UC6 and assumption IDs A1–A13 were assigned while compiling and need confirming.

### Content that does not exist in any source

- **`schema.sql`:** the full schema with triggers and indexes. Only tables, types and keys exist, in `schema-tables.sql`.
- **The 40 security tests:** only ST-27 and seven categories are defined, while Q12.61 and the M4 criteria depend on all 40.
- **Sample incident data:** no example alert, log, metric, trace or release record for the reference scenario (Artifact 3).
- **Runbooks:** of 12 referenced IDs, only RB-03 and RB-04 have written steps.
- **Evaluation:** no frozen suite or recorded baseline, and no labelled retrieval query set.
- **Unset values:** confidence threshold, baseline measures, monthly budget and cost figures.

### Conflicts to settle before Layer 13

- **Execution key:** one per approval (System Design, Technical specification, Layers 5, 7, 8) or the action ID (Layer 11 and its code).
- **State model:** `states.py` has states and transitions that the Technical specification and the Artifact 7 enums do not.
- **Rollback:** its own action and approval (ADR-43) or the same action with `is_undo` (Layer 8).
- **First-version scope:** whether the platform applies fixes after approval, or people do (Layer 1 vs Layer 2 and the System Design).
- **Intake latency:** NFR-08 says p95 ≤ 2 s and NFR-30 says p95 ≤ 300 ms for the same interval.

### Where the code does not match the answers

Treat these as reference code findings. `files/` is unchanged.

| Finding | File | Detail |
| --- | --- | --- |
| Agent not connected | `layer-08/files/workflow.py` | The workflow never calls the Layer 9 coordinator |
| Test verdict too lenient | `layer-11/files/sandbox.py` | `classify` returns `PASSED` for `exit_code=137` or `reproduction_after=None`, while Q11.25 says never |
| Per-call input cap missing | `layer-10/files/gateway.py` | The 16,000-token limit per call is not enforced |
| Evaluation gates incomplete | `layer-12/files/eval_harness.py` | No 80% accuracy floor, and no honesty or conflicting-evidence gate |

Each artifact lists the rest of its findings.

### Source quirks kept as written

- Q1.18 says "measure-able"; the worksheet says "measurable".
- The Q2.23 reasoning stops mid-sentence.
- Question headings keep Room 7's punctuation, such as the colon in `Q1.2:`.
