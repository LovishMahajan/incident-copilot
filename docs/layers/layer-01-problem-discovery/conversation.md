# Layer 1: Problem Discovery — Conversation

- **Central question:** What problem are we actually solving?
- **Built from:** None. This layer begins the worksheet.
- **Focus for this layer:** Answers must describe people, behaviour and outcomes. Do not name any technology, framework, database, model or vendor.
- **Deliverable:** [Artifact 1: Product Requirements Document](artifact-01-product-requirements-document.md)
- **Questions:** 22

## Conversation log

| Questions | Agent | Conversation link | Decision reached | Open unknowns and owner | Artifact section |
| --- | --- | --- | --- | --- | --- |
| Q1.1–Q1.22 | TBD | [Shared conversation](https://claude.ai/share/c6a48a50-d511-4886-b4f5-0a3000f6a725) (needs sign-in; not read for these docs) | Recorded per question below | Listed under each question's Unknowns and Assumptions | [Artifact 1: Product Requirements Document](artifact-01-product-requirements-document.md) |

## Where Claude and ChatGPT disagreed

TBD: the source documents do not record any disagreements. The team fills this in.

## Questions and answers

### Q1.1 An organization operates several software services, and engineers frequently receive alerts about failures. How would you identify the underlying problem that needs to be solved?

**Frame:** When a recent release or configuration change breaks a production service, the on-call engineer has to find the cause by hand. They line up when the errors started against what changed, one source at a time. That delays recovery, leads to wrong fixes under pressure, and leaves no independent confirmation that the service has actually recovered.

**Goal:** cut the time from alert to a verified recovery for change-caused failures. The product will connect the failure to the change automatically, propose undoing it, have a named person approve it, and confirm recovery against the original symptom.

**In the reference scenario:** a new release changes a configuration value and the application starts returning server errors. The product links the errors to that release and that value, proposes reverting the value, waits for approval, and confirms the errors have stopped.

### Q1.2: Different teams experience service failures, but ownership of resolving them is unclear. How would you identify the people responsible for handling these failures?

**Frame.** When a service fails and no one clearly owns the fix, who should act?

**Answer.** Identify three people for the failing service and notify all of them:

- **The owning team**, who approves the fix.
- **The on-call engineer**, who responds now.
- **The author of the last change**, who has the most context.

If any of them can’t be found or doesn’t respond, the incident goes to the incident lead.

**Reasoning.** Each person holds something the others don’t: authority, duty to respond, or context. Having a fallback means unclear ownership never stalls the response.

### Q1.3 A production service goes down at 2 AM. What happens to the business, its users and the engineering team?

**Frame.** What does a night-time failure cost, and who pays for it?

**Answer**

- **Users:** requests fail, work is lost, and trust drops.
- **Business:** revenue stops, service commitments may be missed, and reputation suffers.
- **Engineers:** a tired on-call engineer is woken and makes slower, riskier decisions, and others get pulled in.

**Reasoning.** Every one of these costs grows the longer recovery takes, and a tired engineer is more likely to apply a wrong fix. So the product must make recovery both faster and safer.

Additional point : agent does the root cause analysis and notifies the developer responsible for the environment handling.

### Q1.4 An engineer receives an incident alert. Walk through everything they currently have to do before identifying the cause.

**Frame.** What steps does an engineer go through by hand, from alert to identified cause?

**Answer**

1. Wake up and acknowledge the alert.  
    (Notifying the customers for accountability)
2. Work out which service is affected and how badly.  
    (Pointing out services which exactly failed)
3. Check its health signals and error records, one source at a time.  
    (health metrics to check the services is up or down )
4. Search for what changed recently, and who changed it.  
    (Recent commit history)
5. Find and wake the owner if it isn’t them.
6. Form a theory, test it, and often start again.
7. Keep stakeholders updated throughout.

**Reasoning.** Steps 2–6 are gathering evidence and lining it up in time. They take most of the effort and repeat on almost every incident, so they are what the product should automate.

### Q1.5 An engineer spends hours repeatedly checking logs, metrics and deployment history. Which parts of this investigation could be automated?

**Frame.** Of the repeated checking, what can be done without a person?

**Answer**

- Collecting error samples and health signals.
- Listing recent releases and configuration changes, with times and authors.
- Lining up when the errors started against what changed.
- Comparing the service’s behaviour before and after the failure began.
- Writing a first summary for the engineer.
- After a fix, checking that the original symptom has gone.

**Reasoning.** These steps are read-only, the same every time, and harmless if repeated. Choosing and applying a fix stays with a person.

### Q1.6 Some debugging decisions require experience and judgment. How would you distinguish repetitive investigation from decisions that require human expertise?

**Frame.** Where is the line between routine investigation and decisions that need an experienced person?

**Answer. Ask two questions about each step:**

1. Would two experienced engineers always do the same thing here?
2. Is the step read-only, or easy to undo?
- Yes to both: repetitive, so the product does it. Examples: gathering evidence, building the timeline, checking recovery.
- No to either: judgement, so a person decides. Examples: choosing between undoing the release or fixing forward, accepting business risk, and anything hard to undo.

**Reasoning.** This test can be applied to any step, and it puts decisions with people exactly where mistakes are costly. In the scenario, finding the changed value is routine; deciding how to fix it is judgement.

### Q1.7 Engineers take too long to resolve incidents because information is scattered across different systems. What information would they need to identify the root cause?

**Frame.** What evidence would let an engineer find the cause quickly?

**Answer**

1. When it started and how widespread it is: which requests, which users, what share.
2. The errors themselves: messages, where they occur, examples.
3. What changed recently: releases, configuration, dependencies, with times and authors.
4. Health of the services it depends on.
5. Behaviour before and after the start: speed, volume, error rate.
6. Similar past incidents and how they were resolved.
7. Who owns the service, and any written procedures for it.

**Reasoning.** In the scenario, items 1–3 together are enough: errors began right after a release that changed one value. Today these sit in separate places, and that separation is the delay.

### Q1.8 An engineer makes an incorrect diagnosis and applies an unnecessary fix. What could happen to the application and its users?

**Frame.** What goes wrong for the application and its users when the wrong cause is fixed?

**Answer**

- **The outage lasts longer**, because the real cause is still there.
- **A new failure is introduced** by the unnecessary change.
- **Data can be damaged**, for example by undoing a change that had already moved or rewritten data.
- **The real cause stays hidden** and comes back later.
- **More people are pulled in**, and confidence in the team drops.

In the scenario, restarting the service or undoing an unrelated change would leave the bad configuration value in place while looking like progress.

**Reasoning.** This is why the product must show evidence for the cause it proposes, test fixes first, require approval, and verify recovery independently.

### Q1.9 Engineers receive an incident alert but have no automated mechanism to investigate it. What capabilities should a system provide immediately after an incident occurs?

**Frame.** Once an alert arrives, what should the system do before anyone asks?

**Answer**

1. Open one incident record, merging duplicate alerts.
2. Assess impact: which service, how many requests failing, since when.
3. Notify the right people: the owner, the on-call engineer and the author of the last change.
4. Gather evidence automatically: errors, health signals and recent changes.
5. Build a timeline lining up when errors started against what changed.
6. Propose likely causes, ranked, with evidence and confidence for each.
7. Keep everything in that one record as the incident progresses.

**Reasoning.** By the time the engineer is awake, the gathering should already be done. They should start from “here is what we know”, not a blank screen.

### Q1.10 A debugging assistant can explain an error but cannot help resolve it. What additional capabilities would be required to move from explanation to remediation?

**Frame.** What is missing between “here is why it broke” and “it’s fixed”?

**Answer**

1. **Propose a specific fix:** in the scenario, revert the configuration value.
2. **Show its effect and risk:** what should change, what could go wrong, and how to undo it.
3. **Test it safely first**, in a disposable copy of the service.
4. **Get approval** from a named, accountable person before touching production.
5. **Apply it in a controlled way**, so it can be reversed.
6. **Verify recovery** against the original symptom, and undo the fix if the symptom remains.

**Reasoning.** An explanation is advice; a fix changes production. Each step removes one way a correct explanation can still lead to a bad outcome.

### Q1.11: An AI system identifies a possible fix, but applying it could affect production. Who should be responsible for authorizing that action?

**Frame.** When the system finds a fix that could affect production, who is allowed to say “do it”?

**Answer.** A named person accountable for the affected service: the service owner or the on-call engineer. For high-impact changes, the incident lead approves as well. The system can never approve its own production changes. Every approval is recorded with who approved, when, and what.

**Reasoning.** Accountability has to sit with someone who knows the service and can answer for the decision afterwards. A system that approves itself removes the check that catches its own mistakes.

### Q1.12 An AI system claims that it has repaired an application, but the application is still returning errors. How would you independently establish whether the repair worked?

**Frame.** The system says it’s fixed but errors continue. How do we find the truth?

**Answer**

1. **Define “recovered” before the fix**, in terms of the original symptom: errors back to their normal level, user-facing checks passing, no new kinds of error.
2. **Require it to hold for a set period**, not a single moment.
3. **Check with a separate step**, not the one that applied the fix.
4. **If the check fails**, undo the fix and return the incident to a person.
5. **A person accepts recovery** before the incident is closed.

**Reasoning.** “The change was applied” is not the same as “users are served again”. Checking the symptom, independently, catches a fix that applied correctly but didn’t help.

### Q1.13 An AI system cannot establish the root cause because telemetry is incomplete. How should it behave?

**Frame.** What should the system do when it can’t see enough evidence to find the cause?

**Answer**

- Say plainly that the cause cannot be established.
- Show what it did find, with a confidence level.
- List what evidence is missing, and how to get it.
- Suggest only safe, information-gathering steps.
- Never name a cause or propose a production fix on weak evidence.
- Hand the incident over to a person.

**Reasoning.** A confident wrong answer leads straight to the harms of a wrong fix (Q1.8). Being honest about gaps is also what builds trust.

### Q1.14 An ordinary AI debugging assistant provides suggestions, while an engineering platform can investigate, coordinate actions and verify recovery. What additional responsibilities distinguish these two products?

**Frame.** What extra responsibilities does an engineering platform carry that an assistant doesn’t?

| **Responsibility** | **Assistant** | **Platform** |
| --- | --- | --- |
| Explains an error it is shown | Yes | Yes |
| Gathers evidence on its own | No | Yes |
| Owns the incident from alert to close | No | Yes |
| Finds and notifies the right people | No | Yes |
| Tests a fix safely, then applies it after approval | No | Yes |
| Verifies recovery | No | Yes |
| Keeps a complete record | No | Yes |
| Knows when to stop and hand over | Rarely | Required |

**AnswerReasoning.** An assistant answers when asked; a platform is responsible for the outcome. Every extra responsibility needs a matching safeguard: approval, verification, a record.

### Q1.15 Engineers refuse to trust an AI system with production incidents. What evidence, controls and guarantees would make them comfortable using it?

**Frame.** What evidence, controls and guarantees would make engineers comfortable using it on real incidents?

**Answer**

- **Evidence:** every claim shows its source, and every proposed cause shows the evidence for and against it.
- **Controls:**
    - read-only by default
    - named-person approval for any production change
    - a test in a disposable copy first
    - easy undo
    - a list of prohibited actions it can’t override
- **Guarantees:** independent verification of recovery, a complete record of what it saw and did, and an honest “I don’t know” when evidence is missing.
- **Track record:** start in advisory mode alongside engineers, and publish how often its proposed cause matched the incident review.

**Reasoning.** Engineers trust what they can check and what can’t surprise them. Advisory mode lets the system build a track record before it is allowed to act.

### Q1.16 The team has six months to build the platform, but the initial budget only supports a small implementation. Which capabilities would constitute the smallest genuinely useful product?

**Frame.** With six months and a small budget, what is the least we can build that is still truly useful?

**Answer.** Handle one kind of incident end to end: **failures caused by a recent release or configuration change.**

1. Open the incident and notify the right people.
2. Gather evidence and build the timeline.
3. Link the failure to the change that caused it.
4. Propose undoing that change, and test it in a disposable copy.
5. Apply it after a named person approves.
6. Verify recovery and keep the full record.

Other kinds of incident, many services failing at once, and a polished interface all come later.

**Reasoning.** Investigation alone still leaves the riskiest steps to tired people, and covering every kind of incident can’t be done well in six months. Change-caused failures are common, have clear evidence, and are usually reversible, so they are the safest place to earn trust.

### Q1.17 Stakeholders want to see the platform working end to end. Design a demonstration that begins with an incident and ends with a verified recovery.

**Frame.** How do we show stakeholders the whole loop, from incident to verified recovery?

**Answer**

*Run 1: the full loop*

1. The web application is healthy, and its normal error level is shown.
2. An engineer releases a version that changes one configuration value.
3. Server errors start and the alert fires.
4. The system opens the incident and notifies the owner and the engineer who released.
5. It shows the timeline: errors began right after the release, with the changed value highlighted.
6. It proposes reverting the value, with evidence and risk, and shows the result of the test in a copy.
7. The engineer approves, and the value is reverted.
8. Errors return to their normal level and hold for the agreed period; the engineer accepts recovery.
9. The closed incident shows the full record, with the time each step took.

*Run 2: the honesty check*

- Repeat the same failure, but withhold key evidence.
- The system says it can’t establish the cause and hands over, instead of guessing.

**Reasoning.** Run 1 proves the loop works. Run 2 proves the system won’t bluff, which is usually what cautious stakeholders care about most.

### Q1.18 Management wants to establish whether the platform is worth investing in. Which measure-able outcomes would demonstrate its effectiveness?

**Frame.** Which measurable results would prove to management the platform is worth the money?

**Answer**

| **Measure** | **Starting target (provisional)** |
| --- | --- |
| Time from alert to identified cause | 50% lower than the measured baseline |
| Time from alert to verified recovery | 30% lower than baseline |
| Proposed cause confirmed by the incident review | 80% or more |
| Fixes that later had to be reversed | Lower than baseline |
| Recoveries independently verified | 100% |
| Engineer hours per incident | Lower than baseline |
| Engineers who want to keep using it | Majority after the pilot |

**Reasoning.** Measuring speed alone would reward fast wrong answers, so speed is paired with correctness, safety and adoption. Nothing here counts activity, such as alerts handled or suggestions made.

### Q1.19 An AI system is capable of performing actions that could damage production. Which actions should the platform explicitly prohibit?

**Frame.** Since the platform could damage production, what must it never be allowed to do?

**Answer**

1. Change production without a recorded approval from an accountable person.
2. Delete or overwrite data, backups or history.
3. Change access rights, security settings, or its own permissions and limits.
4. Switch off or silence monitoring and alerts.
5. Act on any service other than the one in the incident.
6. Run free-form, unreviewed commands in production.
7. Contact customers or anyone outside the company.
8. Take any action involving money: billing, payments or contracts.
9. Close an incident without independent verification and a person’s acceptance.

**Reasoning.** Each of these actions is irreversible, spreads damage beyond the incident, or removes a safeguard. A single mistake in any of them is unacceptable, however accurate the system usually is.

### Q1.20 The monitoring system has detected an incident. Design the complete sequence of actions the platform should perform, from receiving the alert until the service recovers.

**Frame.** What should the platform do, step by step, from the alert until the service is back?

**Knowns.**

- In the reference scenario, an engineer deploys a new version that changes a configuration value. The Python web application starts returning HTTP 500 errors, and monitoring detects it.
- Artifact 1 decisions:
    - Any production change needs approval from a named person.
    - Recovery is verified independently against the original symptom, and must hold for a set period.
    - When evidence is too thin, the platform hands over instead of guessing.

**Unknowns.** Which tools exist; the severity levels; the service objectives; which actions the team will allow.

**Assumptions (provisional).**

- Alerts include a service name. *Owner: Platform owner. Validated when: every alert rule in scope carries a service label.*
- Every service has an on-call owner. *Owner: Engineering manager. Validated when: the on-call rota covers every service in scope.*

**Answer.**

1. **Receive the alert:** authenticate the source, store the raw alert, and acknowledge it.
2. **Deduplicate:** group related alerts into one incident, and add repeats to its timeline.
3. **Create the incident record:** give it an ID and start the timeline.
4. **Triage:** identify the service, the owner and the impact, and propose a severity. **A person confirms the severity.**
5. **Notify:** alert the on-call engineer and the author of the last change. For high severity, do this before investigating any further.
6. **Collect evidence:** gather metrics, logs, traces, recent changes, dependency health and past incidents. Record any source that is missing.
7. **Form hypotheses:** propose several possible causes, each with its supporting and contradicting evidence.
8. **Test hypotheses:** run read-only checks and update confidence. Repeat within a time limit.
9. **Recommend a fix:** propose the smallest reversible action, with its evidence, risk, expected result and undo plan.
10. **Test the fix** in a safe, isolated copy of the service: reproduce the failure, apply the fix, and confirm the symptom goes away.
11. **Get approval:** a named person approves the exact action.
12. **Execute:** run only the approved action, and record who approved it and what ran.
13. **Verify recovery independently:** check against the original symptom, using a step separate from the one that ran the fix, and confirm it holds for the agreed period.
14. **If recovery fails:** undo the change and notify the approver. **A person decides the next step.**
15. **Accept and close:** a person accepts that recovery holds. The platform drafts the timeline and the post-incident review, and follow-up actions are recorded with owners.

**Stop rule at every step:** hand over to a person if impact grows, confidence stays low, evidence runs out, the time limit passes, or there is a security risk.

### Q1.21 Multiple systems contribute information during the investigation. How should the platform preserve that information throughout the incident lifecycle?

**Frame.** How do we keep what each system contributed complete and trustworthy, until the incident is closed and reviewed?

**Knowns.**

- Several systems contribute evidence, including monitoring, logs, traces and the change history.
- Artifact 1 decisions:
    - Each incident has one record.
    - Every claim shows its evidence.
    - The record is kept after closing, for review.

**Unknowns.** Retention rules; how long the source tools keep data; privacy requirements; who may read the record.

**Assumptions (provisional).**

- One incident store can be used for every incident. *Owner: Platform owner. Validated when: the store holds a full scenario run.*
- Source tools provide links or IDs that can be referenced. *Owner: Platform owner. Validated when: each tool’s API returns stable identifiers.*

**Answer.** Keep one incident record, with an append-only timeline as its backbone.

| **What to keep** | **Detail** |
| --- | --- |
| Incident state | Status, severity and owner, and every change to them, with timestamps |
| Evidence items | Who or what contributed it, collection time, the time it refers to, the query used, a snapshot, and a link to the original |
| Duplicate alerts | Added to the timeline as evidence, never dropped |
| Hypotheses | Each one, its confidence over time, the evidence for and against, and why it was ruled out |
| Actions | What was recommended, how it was tested, who approved it, what ran, and the result |
| Human input | Notes, decisions, overrides and handoffs |
| Gaps | Sources that failed, and checks that couldn’t run |

**Rules that keep it reliable:**

- **Snapshot the evidence:** source tools may expire data, so keep a copy of what was relied on, not just a link.
- **Append, never overwrite:** a correction is a new entry, so the record shows what was known at each moment.
- **Use one ID throughout:** every entry carries the incident ID.
- **Separate fact from inference:** label observed data differently from the platform’s conclusions.
- **Protect the record:** redact secrets and personal data before storing, restrict access, and set a retention period.
- **Survive failures:** keep the record outside the systems being investigated, so an interrupted investigation can resume from it.
- **Make handover easy:** an engineer taking over can read the full state without asking anyone.

### Q1.22 Some decisions could cause irreversible production changes. At which exact points should a human become involved, and why?

**Frame.** At which exact points must a person decide, because an action could cause harm that can’t be undone?

**Knowns.**

- Artifact 1 decisions:
    - Any production change needs approval from a named, accountable person.
    - A person decides at four points: confirming severity, approving a fix, choosing the next step when a fix fails, and accepting and closing the incident.
    - The platform never approves its own actions.

**Unknowns.** Which actions the organisation will later automate; who can approve at each severity.

**Assumptions (provisional).**

- Investigation is read-only and needs no approval. *Owner: Security lead. Validated when: the platform’s access is confirmed to be read-only.*
- Approvers can be reached during an incident, including at night. *Owner: Engineering manager. Validated when: the on-call rota covers every service in scope.*

**Answer.** The platform stops and waits for a person at these points:

| **Point** | **Why a person is needed** |
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
| Choosing the next step when a fix fails | The first diagnosis was wrong, so the platform shouldn’t pick its next move alone |
| Accepting that recovery holds | The platform verifies recovery; a person accepts it |
| Closing the incident and accepting the root cause | The team owns the conclusion and the follow-up work |

**How approval works:**

- **Show the evidence:** the request states the action, the reason, the evidence, the test result, the risk, the expected result and the undo plan.
- **Approve one action:** an approval covers that exact action on that target, for a limited time.
- **Check before running:** confirm that conditions haven’t changed since the approval.
- **No response means no action:** escalate to the next approver, and never proceed by default.
- **Record it:** log who approved or rejected the action, and when.

**Starting position:** no production change happens without approval. Later, the team may allow specific low-risk, reversible actions, one at a time, once they have proven safe. Each one is a logged decision with an owner and a review trigger. Irreversible actions always need a person.
