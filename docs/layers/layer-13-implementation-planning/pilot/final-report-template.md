# Final implementation report (T5.6): TEMPLATE

> **NOT YET RUN.** This is the template for Artifact 13.10's final report. The pilot (T5.3) has not run, so no section below holds a result. Every `<...>` is filled in from the source named under it once the pilot has run; until then nothing here is evidence. Owner: Engineering manager. Created 2026-11-19 (M5 pass 1, ADR-75).

The test of this report (13.5, T5.6): an outside engineer can run the system from section 2 and name its limits from section 10.

| Field | Value |
| --- | --- |
| Pilot period | `<from>` to `<to>`, the `--from`/`--to` of every report below |
| Pilot services | `<service, ...>`, as named to `aisre pilot check --service` |
| Tag | `<tag>`, CI run `<link>` |
| Go-live check | `aisre pilot check` output on the day Release A went live (T5.1, HG-09): `<attach>` |
| Reports | `aisre pilot baseline`, `aisre pilot report` and `aisre pilot approvals` with `--out`, each `.md` and `.json` attached; each must say "pilot", not "synthetic" |
| Written by | `<name>`, `<date>` |

## 1. What was built

Per milestone, what shipped and the tag. From the evidence records in `../milestones/` (m0.md to m4.md, and m5.md).

| Milestone | Shipped | Tag | Evidence record |
| --- | --- | --- | --- |
| M0 to M5 | `<from each record's result section>` | `<tag>` | `../milestones/mN.md` |

## 2. How to run it

Commands, environments and runbooks: README.md (local stack, tests), `docs/onboarding.md` (adding a service, the go-live check), `ops/` (runbooks, Alertmanager). `<What differs in the pilot's production environment.>`

## 3. Architecture as built

Each difference from Artifacts 6 to 12, with the ADR that records it (ADR-71 to ADR-75 list the stand-ins). `<List; "none" is a claim to check, not a default.>`

## 4. Decisions

The register's final state (`docs/decisions.md`), including what was overturned and why. `<Changes since M4; each Proposed ADR accepted, revised or withdrawn.>`

## 5. Requirements

Each of the 106 active IDs and its result, carried from the milestone records; pilot-measured ones from section 8. `<Table or link.>`

## 6. Security

All 40 cases (ST-01 to ST-40) with their evidence, and any bypass found during the pilot with the fence it set (ADR-50) and the fix. `<From the security suite run on the tag, and the pilot's incident list.>`

## 7. Evaluation

Suite version, baseline, every ADR-65 gate result, and the two accuracy measures reported separately (ADR-65): suite accuracy from the frozen suite run; confirmation by incident review from section 8, never merged into one number.

| Measure | Value | Source |
| --- | --- | --- |
| Suite, version and baseline | `<suite-1.0, baseline-1>` | Artifact 12.10, `scorecard.json` |
| Each ADR-65 gate | `<result>` | the held-out run |
| Proposed cause confirmed by incident review | `<accuracy.text>` | `pilot-report.json`, `accuracy` |

## 8. Pilot

Fed by `aisre pilot report` (T5.3) and `aisre pilot baseline` (T5.2) for the pilot period. Copy each value with its denominator and interval, as the JSON gives it; a value with no denominator is not a result. Targets are Artifact 1.6's and ADR-65's.

| Measure | Pilot | Baseline | Target | Met? | JSON key |
| --- | --- | --- | --- | --- | --- |
| Proposed cause confirmed by the incident review | `<>` | n/a | ≥ 80% | `<>` | `accuracy` |
| The same, change-caused incidents | `<>` | n/a | ≥ 80% | `<>` | `accuracy_change_caused` |
| False-established | `<>` | n/a | ≤ 2% | `<>` | `false_established` |
| Under-escalation | `<>` | n/a | < 2% | `<>` | `under_escalation` |
| Alert to identified cause (change-caused) | `<platform median>` | `<baseline median>` | 50% lower | `<>` | `time_to_cause.platform`; baseline `time_to_identified_cause` |
| Alert to first summary | `<p95>` | n/a | p95 ≤ 300 s | `<>` | `first_summary` |
| Model cost per incident | `<mean>` | `<baseline-1's>` | ≤ 1.2× baseline | `<>` | `cost` |
| Engineer hours per incident | `<median>` | `<baseline>` | lower | `<>` | `engineer_hours` |
| Fixes later reversed | `<rate>` | `<baseline>` | lower | `<>` | `fixes_reversed` |
| Incidents, reviewed, proposed but not reviewed | `<>` | | all reviewed | `<>` | `incidents`, `reviewed`, `proposed_not_reviewed` |
| Hand-overs by reason | `<>` | | | | `hand_overs` |
| Approvals and refusals | `<none in Release A>` | | | | `approvals` |
| Engineers who want to keep using it; who rate the summary useful | `<>` | | majority each | `<>` | the pilot survey (not in the record) |

Baselines (T5.2): `<from earlier records, or the first 30 pilot incidents; which, and when they were fixed (1.6)>`.

**Approval review (T5.4, Demo D3).** From `aisre pilot approvals`: each request, what the approver was shown and how long they took, and the approver's own judgement written under each. `<Who reviewed, on which requests (test target in Release A), and their conclusion.>`

**Scenario revert (T5.5, Demo D8).** `<Done or not, through the real release process (Release B).>`

**Where the pilot missed** (13.9, M5's failure shown): `<each measure below target, and why.>`

## 9. Reliability

Drill results and measured RPO and RTO, from the M4 record and any drill during the pilot. `<>`

## 10. Limitations

What it doesn't do (Artifact 1.7), and the known risks: late apply, a refused patch, provisional patterns rejecting real text, legal deletion; plus every stand-in still in place (ADR-71 to ADR-75). `<>`

## 11. Provisional values

Which were validated by the pilot, and which still aren't (the Assumptions tables in each Artifact). `<>`

## 12. The Release B decision

The decision record (T5.7, `decision-record-template.md`, recorded in `docs/decisions.md`) and its reasoning. `<Link once written. This template does not make or suggest it.>`

## 13. Open dependencies and owners

`<From 13.11 and each ADR's "Revisit when".>`
