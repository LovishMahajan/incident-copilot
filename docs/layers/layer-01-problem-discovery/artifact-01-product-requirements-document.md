# Artifact 1: Product Requirements Document

- **Layer:** 1, Problem Discovery
- **Built from:** None. This layer begins the worksheet.
- **Status:** Proposed: gaps resolved and reference code aligned 2026-11-02 per [decisions.md](../../decisions.md); pending team sign-off
- **Sources:** [conversation.md](conversation.md), the team's written 1.1 text, [Layer 2 conversation](../layer-02-scope-and-constraints/conversation.md), [Layer 3 conversation](../layer-03-sre-fundamentals/conversation.md), [Layer 4 conversation](../layer-04-requirements/conversation.md), [system-design.md](../../reference/system-design.md), [decisions.md](../../decisions.md)

## Required sections

| Required section | Questions this section answers | Done |
| --- | --- | --- |
| 1.1 Problem statement | Q1.1, Q1.3–Q1.4, Q1.7–Q1.8 | Yes |
| 1.2 Target users | Q1.2–Q1.3, Q1.11 | Yes |
| 1.3 User journey | Q1.4, Q1.9–Q1.10, Q1.20 | Yes |
| 1.4 Core use cases | Q1.5, Q1.7, Q1.9–Q1.10, Q1.12–Q1.13, Q1.16, Q1.20–Q1.21 | Yes |
| 1.5 Product boundaries | Q1.5–Q1.6, Q1.11, Q1.14–Q1.15, Q1.19, Q1.22 | Yes |
| 1.6 Success definition | Q1.15, Q1.17–Q1.18 | Partial |
| 1.7 Explicit non-goals | Q1.5–Q1.6, Q1.16 | Yes |

## 1.1 Problem statement

*Answers Q1.1, Q1.3–Q1.4, Q1.7–Q1.8.*

**When a production service fails, the on-call engineer has to investigate by hand, piecing together what changed, what the service is doing and when it started from many separate sources. Most of their time goes on gathering and cross-checking evidence, not on the decision that needs their judgement.**

**Goal:** cut the time from alert to a verified recovery for change-caused failures. The product will connect the failure to the change automatically, propose undoing it, have a named person approve it, and confirm recovery against the original symptom ([Q1.1](conversation.md)). It gets there in two releases: Release A advises and people make every production change; Release B adds applying the approved fix (ADR-44, see 1.5).

**Reference scenario:** a new release changes a configuration value and the application starts returning server errors. The product links the errors to that release and that value, proposes reverting the value, waits for approval, and confirms the errors have stopped ([Q1.1](conversation.md)).

| Why it matters | Cost | Source |
| --- | --- | --- |
| Users | Requests fail, work is lost, and trust drops | [Q1.3](conversation.md) |
| Business | Revenue stops, service commitments may be missed, and reputation suffers | [Q1.3](conversation.md) |
| Engineers | A tired on-call engineer is woken and makes slower, riskier decisions, and others get pulled in | [Q1.3](conversation.md) |
| Scattered evidence | When it started, the errors and what changed sit in separate places, and that separation is the delay | [Q1.7](conversation.md) |
| Wrong diagnosis | The outage lasts longer, a new failure is introduced, data can be damaged, the real cause stays hidden and comes back, more people are pulled in | [Q1.8](conversation.md) |

Every one of these costs grows the longer recovery takes, and a tired engineer is more likely to apply a wrong fix. So the product must make recovery both faster and safer ([Q1.3](conversation.md)).

## 1.2 Target users

*Answers Q1.2–Q1.3, Q1.11.*

For the failing service, identify these people and notify all of them. Each holds something the others don't: authority, duty to respond, or context ([Q1.2](conversation.md)).

| Person | Holds | Role in an incident |
| --- | --- | --- |
| The owning team (service owner) | Authority | The service owner approves the fix ([Q1.2](conversation.md), [Q1.11](conversation.md), ADR-49) |
| The on-call engineer | Duty to respond | Responds now; may approve the fix ([Q1.2](conversation.md), [Q1.11](conversation.md)) |
| The author of the last change | Context | Has the most context; is notified ([Q1.2](conversation.md)) |
| The incident lead | Fallback | Receives the incident if any of the above can't be found or doesn't respond; confirms severity; may approve a fix; accepts recovery and closes the incident ([Q1.2](conversation.md), [Q1.11](conversation.md), ADR-49, ADR-50, ADR-66) |

Anyone approving must hold the approve permission for that service. High-impact changes need two different people with that permission (ADR-49).

Users of the failing service and the business bear the cost of the failure ([Q1.3](conversation.md)) but do not operate the product.

## 1.3 User journey

*Answers Q1.4, Q1.9–Q1.10, Q1.20.*

**Today, by hand ([Q1.4](conversation.md)):** wake up and acknowledge the alert; work out which service is affected and how badly; check health signals and error records, one source at a time; search for what changed recently, and who changed it; find and wake the owner if it isn't them; form a theory, test it, and often start again; keep stakeholders updated throughout. Steps 2–6 are gathering evidence and lining it up in time. They take most of the effort and repeat on almost every incident.

**With the product ([Q1.20](conversation.md)):** by the time the engineer is awake, the gathering should already be done. They start from "here is what we know", not a blank screen ([Q1.9](conversation.md)).

| Step | What happens | Who |
| --- | --- | --- |
| 1. Receive the alert | Authenticate the source, store the raw alert, and acknowledge it | Platform |
| 2. Deduplicate | Group related alerts into one incident, and add repeats to its timeline | Platform |
| 3. Create the incident record | Give it an ID and start the timeline | Platform |
| 4. Triage | Identify the service, the owner and the impact, and propose a severity | Platform; **the incident lead confirms the severity** (ADR-50) |
| 5. Notify | Alert the service owner, the on-call engineer and the author of the last change. For high severity, do this before investigating any further | Platform |
| 6. Collect evidence | Gather metrics, logs, traces, recent changes, dependency health and past incidents. Record any source that is missing | Platform |
| 7. Form hypotheses | Propose several possible causes, each with its supporting and contradicting evidence | Platform |
| 8. Test hypotheses | Run read-only checks and update confidence. Repeat within a time limit | Platform |
| 9. Recommend a fix | Propose the smallest reversible action, with its evidence, risk, expected result and undo plan | Platform |
| 10. Test the fix | In a safe, isolated copy of the service: reproduce the failure, apply the fix, and confirm the symptom goes away | Platform |
| 11. Get approval | A named person approves the exact action and, in the same decision, signs a conditional approval to undo it if recovery fails (ADR-48). High-impact changes need two people (ADR-49) | **Person** |
| 12. Execute | Run only the approved action, and record who approved it and what ran | Platform |
| 13. Verify recovery independently | Check against the original symptom, using a step separate from the one that ran the fix, and confirm it holds for the hold period: 10 minutes by default, longer per service (ADR-66) | Platform |
| 14. If recovery fails | Undo the change under the conditional approval, or a fresh one if none was signed, and notify the approver. If there is too little data to decide, hand over without undoing (ADR-48, ADR-66) | Platform; **a person decides the next step** |
| 15. Accept and close | The platform drafts the timeline and the post-incident review, and follow-up actions are recorded with owners | **The incident lead accepts that recovery holds** (ADR-66) |

**By release (ADR-44):** Release A runs steps 1–10, then hands the tested recommendation to a person, who makes any production change by hand. The incident lead still closes the incident, but the platform has not verified recovery in production. Steps 11–14 run against production only in Release B; until then they are built and tested on synthetic targets.

**Stop rule at every step:** hand over to a person if impact grows, confidence stays low, evidence runs out, the time limit passes, or there is a security risk ([Q1.20](conversation.md)).

**From explanation to fix ([Q1.10](conversation.md)):** an explanation is advice; a fix changes production. Steps 9–13 are what is missing between "here is why it broke" and "it's fixed".

**Unknowns from [Q1.20](conversation.md), now settled or tracked:** the tools are the read-only sources in [System Design 5](../../reference/system-design.md). The allowed actions are configuration revert, commit revert and release rollback ([System Design 10](../../reference/system-design.md)). Severity levels and service objectives are tracked in Assumptions.

## 1.4 Core use cases

*Answers Q1.5, Q1.7, Q1.9–Q1.10, Q1.12–Q1.13, Q1.16, Q1.20–Q1.21.*

Handle one kind of incident end to end: **failures caused by a recent release or configuration change.** They are common, have clear evidence, and are usually reversible, so they are the safest place to earn trust ([Q1.16](conversation.md)). The numbers UC1–UC8 below are confirmed, and later layers cite them.

| ID | Use case | What the product does | Source |
| --- | --- | --- | --- |
| UC1 | Open the incident and notify the right people | Opens one incident record, merging duplicate alerts; assesses impact (which service, how many requests failing, since when); notifies the owner, the on-call engineer and the author of the last change | [Q1.9](conversation.md), [Q1.16](conversation.md) |
| UC2 | Gather evidence and build the timeline | Collects errors, health signals and recent changes with times and authors; lines up when errors started against what changed; compares behaviour before and after; writes a first summary | [Q1.5](conversation.md), [Q1.7](conversation.md), [Q1.9](conversation.md) |
| UC3 | Link the failure to the change that caused it | Proposes likely causes, ranked, with evidence and confidence for each | [Q1.9](conversation.md), [Q1.16](conversation.md) |
| UC4 | Hand over when evidence is too thin | Says plainly that the cause cannot be established; shows what it did find, with a confidence level; lists what evidence is missing, and how to get it; suggests only safe, information-gathering steps; never names a cause or proposes a production fix on weak evidence; hands the incident over to a person | [Q1.13](conversation.md) |
| UC5 | Propose a fix and test it safely | Proposes undoing the change, shows its effect, risk and how to undo it, and tests it in a disposable copy of the service | [Q1.10](conversation.md), [Q1.16](conversation.md) |
| UC6 | Apply the fix after approval | **Release A:** hands the tested recommendation to a person, who applies it by hand; the platform holds no production write access. **Release B:** applies it in a controlled way, so it can be reversed, after a named person approves. In both, the platform may draft a change for review and never commits directly (ADR-44) | [Q1.10](conversation.md), [Q1.16](conversation.md) |
| UC7 | Verify recovery independently | Defines "recovered" before the fix, in terms of the original symptom: errors back to their normal level, user-facing checks passing, no new kinds of error. Requires it to hold for a set period: 10 minutes by default, longer per service (ADR-66). Checks with a separate step, not the one that applied the fix. If the check fails, undoes the fix and returns the incident to a person. If there is too little data to decide, hands over without undoing. The incident lead accepts recovery before the incident is closed. Runs against production in Release B (ADR-44) | [Q1.12](conversation.md) |
| UC8 | Keep a complete record | Keeps one incident record, with an append-only timeline as its backbone, complete and in order until the incident is closed and reviewed, and kept after closing | [Q1.21](conversation.md) |

Labels that later layers cite:

- **A1-b** (UC7): Recovery is verified independently against the original symptom, held for a set period.
- **A1-c** (UC4): When evidence is too thin, the cause is not guessed; the incident is handed to a person.

**Evidence needed for UC2 ([Q1.7](conversation.md)):** when it started and how widespread it is; the errors themselves; what changed recently; health of the services it depends on; behaviour before and after the start; similar past incidents and how they were resolved; who owns the service, and any written procedures for it. In the scenario, the first three are enough.

**What UC8 keeps ([Q1.21](conversation.md)):**

| What to keep | Detail |
| --- | --- |
| Incident state | Status, severity and owner, and every change to them, with timestamps |
| Evidence items | Who or what contributed it, collection time, the time it refers to, the query used, a snapshot, and a link to the original |
| Duplicate alerts | Added to the timeline as evidence, never dropped |
| Hypotheses | Each one, its confidence over time, the evidence for and against, and why it was ruled out |
| Actions | What was recommended, how it was tested, who approved it, what ran, and the result |
| Human input | Notes, decisions, overrides and handoffs |
| Gaps | Sources that failed, and checks that couldn't run |

**Rules for the record:** snapshot the evidence; append, never overwrite; use one ID throughout; separate fact from inference; protect the record (redact secrets and personal data before storing, restrict access, set a retention period); keep it outside the systems being investigated, so an interrupted investigation can resume from it; make handover easy ([Q1.21](conversation.md)).

**Unknowns from [Q1.21](conversation.md), now settled or tracked:** retention periods are set, provisionally, in ADR-57. Read access follows ADR-50, and the full audit export is restricted. Snapshots mean the record doesn't depend on how long source tools keep data. How often real logs hold personal data is tracked in Assumptions.

## 1.5 Product boundaries

*Answers Q1.5–Q1.6, Q1.11, Q1.14–Q1.15, Q1.19, Q1.22.*

**A1-a:** Any change to production needs approval from a named, accountable person.

**Product or person ([Q1.6](conversation.md)).** Ask two questions about each step: would two experienced engineers always do the same thing here? Is the step read-only, or easy to undo?

| Answer | Who | Examples |
| --- | --- | --- |
| Yes to both | Repetitive, so the product does it | Gathering evidence, building the timeline, checking recovery |
| No to either | Judgement, so a person decides | Choosing between undoing the release or fixing forward, accepting business risk, and anything hard to undo |

**An engineering platform, not an assistant ([Q1.14](conversation.md)).** An assistant answers when asked; a platform is responsible for the outcome. Beyond explaining an error, the platform gathers evidence on its own, owns the incident from alert to close, finds and notifies the right people, tests a fix safely and, from Release B, applies it after approval and verifies recovery in production, keeps a complete record, and knows when to stop and hand over. Every extra responsibility needs a matching safeguard: approval, verification, a record.

**Who authorises a production change ([Q1.11](conversation.md), ADR-49).** A named person accountable for the affected service: the service owner, the on-call engineer or the incident lead, holding the approve permission for that service. High-impact changes need two different people with that permission; the incident lead may be one, but isn't required to be. If severity isn't confirmed yet, the change counts as high-impact. Nobody approves their own request, and the system can never approve its own production changes. Every approval is signed and recorded with who approved, when, and what.

**Prohibited actions ([Q1.19](conversation.md)).** The platform must never:

1. Change production without a recorded approval from an accountable person.
2. Delete or overwrite data, backups or history.
3. Change access rights, security settings, or its own permissions and limits.
4. Switch off or silence monitoring and alerts.
5. Act on any service other than the one in the incident.
6. Run free-form, unreviewed commands in production.
7. Contact customers or anyone outside the company.
8. Take any action involving money: billing, payments or contracts.
9. Close an incident without independent verification and a person's acceptance.

**Where the platform stops and waits for a person ([Q1.22](conversation.md)):**

| Point | Why a person is needed |
| --- | --- |
| Confirming severity | Severity sets who is notified and how urgent the work is; a wrong value misdirects the whole response |
| Any production change: rollback, restart, scaling, configuration change | Even routine actions can make an incident worse, and the diagnosis may be wrong |
| Data changes: deletion, migration, repair, restore | These may not be reversible, and data loss can be permanent |
| Rollback across a schema change | The old version may not work with the new schema |
| Failover or traffic shifts | These affect all users and can risk data loss or split data |
| Permissions, secrets or security settings | Mistakes can widen access or expose data |
| Suspected security incident or data exposure | This needs legal, privacy and security judgement, and evidence must be preserved |
| Customer or external communication | It creates commitments and may have legal effect |
| Low confidence or conflicting evidence | The platform must not act on a guess |
| Choosing the next step when a fix fails | The first diagnosis was wrong, so the platform shouldn't pick its next move alone |
| Accepting that recovery holds | The platform verifies recovery; the incident lead accepts it (ADR-66) |
| Closing the incident and accepting the root cause | The team owns the conclusion and the follow-up work; the incident lead closes (ADR-50) |

**How approval works ([Q1.22](conversation.md)):**

- **Show the evidence:** the request states the action, the reason, the evidence, the test result, the risk, the expected result and the undo plan.
- **Approve one action:** an approval covers that exact action on that target, for 15 minutes (ADR-49).
- **Check before running:** confirm that conditions haven't changed since the approval.
- **No response means no action:** escalate to the next approver after 15 minutes, expire the request after 60 minutes, and never proceed by default (ADR-49).
- **Record it:** log who approved or rejected the action, and when.

**Controls engineers rely on ([Q1.15](conversation.md)):** read-only by default; named-person approval for any production change; a test in a disposable copy first; easy undo; a list of prohibited actions it can't override. Every claim shows its source, and every proposed cause shows the evidence for and against it.

**Release boundaries (ADR-44).** "First version" means everything built through the fourth milestone, shipped in two releases. Production write stays impossible until the second is switched on.

| | Release A: advisory | Release B: controlled remediation |
| --- | --- | --- |
| What the platform does | Takes in alerts, gathers evidence, diagnoses, reviews, drafts a fix, tests it in the disposable copy, and recommends | Adds approval, applying the fix, undoing it, and independent verification against production |
| Who changes production | People, by hand | The platform, on a signed approval |
| Production write access | Not provisioned at all | Provisioned by a recorded decision |
| How an incident ends | The tested recommendation is handed to a person | It waits for approval and continues through journey steps 11–15 |
| Built and tested | First and second milestones, on synthetic incidents | Third and fourth milestones, on synthetic targets |
| Goes live | Advisory pilot, after the fourth milestone gates pass | After the Engineering manager completes the pilot accuracy and approval review |

**Starting position ([Q1.22](conversation.md)):** no production change happens without approval. Later, the team may allow specific low-risk, reversible actions, one at a time, once they have proven safe. Each one is a logged decision with an owner and a review trigger. Irreversible actions always need a person.

**Unknowns from [Q1.22](conversation.md), now settled:** ADR-49 sets who can approve at each severity. No action is planned for automation; any later one follows the starting position above.

## 1.6 Success definition

*Answers Q1.15, Q1.17–Q1.18.*

**Measurable outcomes ([Q1.18](conversation.md), [System Design 10](../../reference/system-design.md), ADR-65):** one set. Two accuracy measures stay, because they answer different questions: suite accuracy gates each release, and confirmation by incident review is the pilot business measure.

| Measure | Target (provisional) | Measured on | Applies from | Source |
| --- | --- | --- | --- | --- |
| Time from alert to identified cause | 50% lower than the measured baseline | Pilot incidents | Release A | [Q1.18](conversation.md) |
| Time from alert to verified recovery | 30% lower than baseline | Pilot incidents and audit | Release B, when verification first runs against production | [Q1.18](conversation.md) |
| Time to first summary | ≤ 5 min p95 | Test suite, then pilot | Release A | System Design 10, ADR-65 |
| Leading cause correct, change-caused incidents | ≥ 80%, and no more than 2 points below the suite's baseline run | Synthetic test suite | Release A, then every release | System Design 10, ADR-65 |
| Proposed cause confirmed by the incident review | 80% or more | Pilot incident reviews | Release A | [Q1.18](conversation.md), ADR-65 |
| Withheld-evidence cases marked inconclusive | 100% | Honesty tests | Release A, then every release | System Design 10, ADR-65 |
| Unsafe actions from injected log text | 0 | Injection test set | Release A, then every release | System Design 10, ADR-65 |
| Production changes without approval | 0 | Audit | Release A | System Design 10 |
| Recoveries independently verified | 100% | Audit | Release B | [Q1.18](conversation.md), System Design 10 |
| Fixes that later had to be reversed | Lower than baseline | Pilot incidents | Release A | [Q1.18](conversation.md) |
| Engineer hours per incident | Lower than baseline | Pilot incidents | Release A | [Q1.18](conversation.md) |
| Engineers who want to keep using it, and who rate the summary useful | Majority on each question after the pilot | Pilot survey | Release A | [Q1.18](conversation.md), System Design 10 |

The other promotion gates, such as conflicting evidence, citations, cost and the investigation limit, are in ADR-65.

Measuring speed alone would reward fast wrong answers, so speed is paired with correctness, safety and adoption. Nothing here counts activity, such as alerts handled or suggestions made.

**Baselines.** Four targets are relative to a baseline: time to identified cause, time to verified recovery, reversed fixes and engineer hours.

| Question | Answer |
| --- | --- |
| Who measures | SRE lead |
| How | Median per incident from the last 6 months of incident records (provisional), change-caused incidents only. Reversed fixes is a rate, so its baseline is the share of fixes reversed in the same records. If records are missing, use the first 30 pilot incidents (provisional) |
| When | Before Release A. The fallback can only finish during the pilot, so the SRE lead records before Release A which baselines depend on it, and those are fixed once the 30 incidents have run |

**Needs measurement:** baseline values for time to identified cause, time to verified recovery, reversal rate and engineer hours per incident, Owner: SRE lead, settled when: measured from the incident records before Release A, or from the first 30 pilot incidents if records are missing.

**Track record ([Q1.15](conversation.md)):** start in advisory mode alongside engineers, and publish how often the proposed cause matched the incident review. Release A is that advisory mode (ADR-44).

**Stakeholder demonstration ([Q1.17](conversation.md)):**

| Run | What it shows | Ends with |
| --- | --- | --- |
| Run 1: the full loop | Healthy application and its normal error level; a release changes one configuration value; server errors and the alert; incident opened; owner, on-call engineer and releasing engineer notified; timeline with the changed value highlighted; proposed revert with evidence, risk and the test result; engineer approves and the value is reverted | Errors return to their normal level and hold for the agreed period; the incident lead accepts recovery; the closed incident shows the full record, with the time each step took |
| Run 2: the honesty check | The same failure, with key evidence withheld | The system says it can't establish the cause and hands over, instead of guessing |

Run 1 proves the loop works. Run 2 proves the system won't bluff, which is usually what cautious stakeholders care about most. Both runs use a test target, not production, so Run 1 can show approval and the revert before Release B (ADR-44).

## 1.7 Explicit non-goals

*Answers Q1.5–Q1.6, Q1.16.*

This list is definitive. "First version" covers both releases (ADR-44).

| Non-goal for the first version | Source |
| --- | --- |
| Other kinds of incident: capacity, hardware, security incidents. They are recognised and handed over, never fixed (ADR-58) | [Q1.16](conversation.md), [System Design 10](../../reference/system-design.md) |
| Many services failing at once | [Q1.16](conversation.md) |
| Applications outside the one type the first version supports | [System Design 10](../../reference/system-design.md) |
| Serving many independent organisations | [Q2.1](../layer-02-scope-and-constraints/conversation.md), [System Design 10](../../reference/system-design.md) |
| Covering several environments | [System Design 10](../../reference/system-design.md) |
| Replacing any monitoring or logging tool | [System Design 10](../../reference/system-design.md) |
| A polished interface or a product dashboard | [Q1.16](conversation.md), [System Design 10](../../reference/system-design.md) |
| New code written by the model. Fixes come only from the allowed list | [System Design 10](../../reference/system-design.md) |
| Data, permission or infrastructure changes | [System Design 10](../../reference/system-design.md) |
| Customising models, running our own models, or dedicated model hardware | [System Design 10](../../reference/system-design.md) |
| Replacing engineers: the platform assists engineers; it doesn't replace them. If it fails, engineers handle incidents manually as today | [Q4.25](../layer-04-requirements/conversation.md), citing Artifact 1 |
| Choosing and applying a fix without a person; accepting business risk | [Q1.5](conversation.md), [Q1.6](conversation.md) |

The actions the platform must never take are in 1.5.

## Assumptions

| Assumption | Status | Owner | Validated when |
| --- | --- | --- | --- |
| Alerts include a service name ([Q1.20](conversation.md)) | Provisional | Platform owner | Every alert rule in scope carries a service label |
| Every service has an on-call owner ([Q1.20](conversation.md)) | Provisional | Engineering manager | The on-call rota covers every service in scope |
| One incident store can be used for every incident ([Q1.21](conversation.md)) | Provisional | Platform owner | The store holds a full scenario run |
| Source tools provide links or IDs that can be referenced ([Q1.21](conversation.md)) | Provisional | Platform owner | Each tool's API returns stable identifiers |
| Investigation is read-only and needs no approval ([Q1.22](conversation.md)) | Provisional | Security lead | The platform's access is confirmed to be read-only |
| Approvers can be reached during an incident, including at night ([Q1.22](conversation.md)) | Provisional | Engineering manager | The on-call rota covers every service in scope |
| Severity levels and service objectives are defined for every service in scope ([Q1.20](conversation.md)) | Provisional | Engineering manager | Both are written down for every service in scope |
| Redacting before storage meets privacy needs, though how often real logs hold personal data is unknown ([Q1.21](conversation.md)) | Provisional | Security lead | Seeded and sampled corpus reviewed |
| The last 6 months of records hold enough change-caused incidents for a baseline; if not, 30 pilot incidents are enough (1.6) | Provisional | SRE lead | Records checked before Release A, or 30 pilot incidents complete |
| Pilot incidents are a fair fallback baseline, although the platform advises on them (1.6) | Provisional | SRE lead | Compared with any partial records before the baseline is fixed |
| "Majority" means more than half of surveyed on-call engineers (1.6) | Provisional | Engineering manager | Survey wording agreed with on-call teams before the pilot |

## Decisions and conflicts

| Item | Resolution | Decided by |
| --- | --- | --- |
| Use case numbers: later layers fix UC4, UC7 and UC8; the rest had no number | UC1–UC8 confirmed. UC1–UC3, UC5 and UC6 follow journey order. UC6 states both releases (ADR-44) | Layer decision (proposed) |
| Labels A1-a, A1-b, A1-c, defined in Layer 3 | Layer 3's wording kept exactly | Layer decision (proposed) |
| Layer focus: Q1.20's Knowns name the application's language and error code | Left out, as are all technology and vendor names. Release behaviour is described in plain terms | Layer decision (proposed) |
| 1. Does the first version apply fixes to production? (Q1.16, Q1.15 vs Layer 2, System Design 1) | Two releases. Release A: people apply fixes by hand. Release B: the platform applies them after approval | ADR-44 |
| 2. Second approver for high-impact changes: incident lead, or any second approver (Q1.11 vs System Design 8) | Two different people with approve rights. The incident lead may be one, but isn't required to be | ADR-49 |
| 3. Who approves: owning team, or service owner or on-call engineer (Q1.2 vs Q1.11) | Service owner, on-call engineer or incident lead, holding the approve permission for that service | ADR-49 |
| 4. Who is notified: three people, or subsets of them (Q1.2, Q1.9 vs Q1.20, Q1.17) | All three: service owner, on-call engineer, author of the last change. UC1, journey step 5 and Run 1 now match. Q1.2 is the most complete answer | Layer decision (proposed) |
| 5. Who accepts recovery: a person, the engineer, or the incident lead; same for confirming severity (Q1.12, Q1.20, Q1.17 vs System Design 8) | The incident lead, in all cases, for both | ADR-49, ADR-50, ADR-66 |
| 6. Customers: Q1.4 adds notifying customers; the platform must never contact them (Q1.4 vs Q1.19, Q1.22) | Q1.4 describes manual steps today, so it stays out of 1.3. The prohibition stands | Layer decision (proposed) |
| 7. Human decision points: four, or the twelve in the table (Q1.22 Knowns vs its Answer) | The twelve-row table is used. The four are in the journey; the rest arise only when that action or condition occurs | Layer decision (proposed) |
| 8. Reversibility: Q4.12's "every change is reversible" vs irreversible actions under a person's control in Q1.22 | Both hold. Every fix the platform proposes comes from the allowed list and is reversible; anything irreversible stays with a person | Layer decision (proposed) |
| 9. Accuracy target: incident review, or synthetic suite (Q1.18 vs FR-13, System Design 10) | Both kept as separate measures: the suite gates each release, the incident review is the pilot business measure | ADR-65 |
| 10. Success measures differ between Q1.18 and System Design 10 | Reconciled into one table in 1.6, with what each is measured on and when it applies | ADR-65 |
| 11. Scope wording: any production failure, or a recent release or configuration change (team's 1.1 text vs Q1.1) | 1.1 text kept verbatim as the problem. The goal, 1.4 and 1.7 narrow the first version to change-caused failures | Layer decision (proposed) |
| 12. Approval detail: "recorded" vs "signed"; "set period" vs 10 minutes (Q1.11, Q1.12 vs System Design) | Approvals are signed and recorded. The hold period is 10 minutes by default, longer per service | ADR-49, ADR-66 |
| Former gap in 1.6: no baseline values, measurer or timing | SRE lead measures before Release A, as stated in 1.6, or from the first 30 pilot incidents where records are missing. Values stay open as a Needs measurement item | Layer decision (proposed) |
| Baseline method for a rate | A median doesn't fit reversed fixes, so that baseline is the share of fixes reversed in the same records | Layer decision (proposed) |
| Measures that depend on Release B | Time to verified recovery and recoveries independently verified start at Release B, since verification runs against production only there | Layer decision (proposed) |
| Adoption target: "majority" vs "to be set with on-call teams" (Q1.18 vs System Design 10) | One pilot survey covers both questions, with a majority target on each (provisional) | Layer decision (proposed) |
| Former gap in 1.7: list assembled from later citations | List made definitive by merging Q1.16 and later citations with System Design 10 | Layer decision (proposed) |
| Run 1 shows a revert, which Release A can't do in production | Both demonstration runs use a test target | Layer decision (proposed) |

## Acceptance checks

- [ ] Every required section above is present and complete.
- [x] Every section lists the questions it answers, and every question in this layer is answered by at least one section.
- [x] Every assumption is marked provisional and has a named owner and a condition under which it will be validated.
- [ ] Every disagreement between Claude and ChatGPT that affected a decision is recorded with the team's resolution.
- [ ] The artifact is consistent with every artifact it is built from; any conflict is recorded as an explicit decision.
- [x] The artifact was produced with the team's coding agent and is saved in the team repository.

Reviewed by: TBD
Date: TBD
