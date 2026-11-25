# Release B decision record (T5.7): TEMPLATE

> **NO DECISION IS RECORDED HERE.** This is the shape of the record HG-10 needs (Artifact 13.12; ADR-44). The decision is the Engineering manager's, after the pilot accuracy and approval review, and is written into `docs/decisions.md` as a new ADR when it is made. Until then the production write credential doesn't exist. Created 2026-11-19 (M5 pass 1, ADR-75).

Copy the block below into `docs/decisions.md`, before "## Not conflicts", as the next ADR number, and fill in every field. A field left empty means the record isn't ready.

```markdown
### ADR-NN: Release B decision (YYYY-MM-DD)

| Field | Value |
| --- | --- |
| Decision | Go / No go / Go with conditions |
| Decided by | Engineering manager: <name> (ADR-44, HG-10) |
| Date | <date> |
| Pilot period and services | <from the final report's header> |
| Evidence | Final report (T5.6): <link>. Pilot report, baselines and approval review (`.md` and `.json`, labelled "pilot"): <links>. M5 evidence record: <link> |
| Pilot accuracy | Proposed cause confirmed by the incident review: <k/n, %, 95% CI> against ≥ 80% (Artifact 1.6) |
| ADR-65 gates in the pilot | False-established <k/n> (≤ 2%), under-escalation <k/n> (< 2%) |
| Approval review | <who read which requests, and whether diff, evidence, risk and flagged items could be judged in the time (D3)> |
| Scenario revert through the real release process | <D8: done, by whom, when> |
| Where the pilot missed | <each measure below target, from the final report> |
| Conditions | <for "go with conditions": each condition, its owner and how it is checked> |
| What changes if go | The production write credential is provisioned (by whom, scoped to which deployments); `remediation_enabled` and the release adapter allowed in production (code and ADR change, reviewed); `aisre pilot check` no longer the go-live check for Release B |
| Revisit when | <date or trigger> |

- **Rationale:** <why, in terms of the evidence above>.
- **Owner:** Engineering manager. **Reversal:** <how a go is undone: credential revoked, write fence set (ADR-50)>.
```
