# Synthetic runbook corpus (T1.17)

**SYNTHETIC. No real runbook, service, team or incident is described.** No runbooks exist for this project yet, so these documents were invented to exercise ingestion, BM25 search and the 10.13 evaluation (Artifact 10). Synthetic runbooks are enough at M1 (Artifact 13.4).

| File | Holds |
| --- | --- |
| `*.md` | One document each: a front-matter block (`doc_id`, `version`, `title`, `doc_type`, `services`, `allowed_groups`, `owner`, `reviewed_at`, `deprecated`), then Markdown. `doc_type` is `runbook` or `past_incident`; `services` `*` means every service |
| `queries-v1.json` | The labelled query set, version 1: 50 queries, 30 `exact_identifier` and 20 `paraphrase`, each with the service and groups it searches as and the passages (`doc_id` plus heading path) an SRE marked relevant. 13 are scope traps: a matching passage exists in another service's document, a group the caller lacks, or a deprecated document |

Ingest with `python -m aisre.retrieval.corpus tests/fixtures/runbooks --org <org-id>` (see `aisre.retrieval.corpus`). Any label change is a new set version (`queries-v2.json`), never an edit (10.13).
