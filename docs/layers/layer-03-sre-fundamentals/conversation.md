# Layer 3: SRE Fundamentals — Conversation

- **Central question:** How does an incident actually happen?
- **Built from:** Artifacts 1 and 2
- **Focus for this layer:** Understand the domain before designing the automation. Describe how software fails and how engineers investigate it; do not design the platform in this layer.
- **Deliverable:** [Artifact 3: Incident and Evidence Catalog](artifact-03-incident-and-evidence-catalog.md)
- **Questions:** 37

## Conversation log

| Questions | Agent | Conversation link | Decision reached | Open unknowns and owner | Artifact section |
| --- | --- | --- | --- | --- | --- |
| Q3.1–Q3.37 | TBD | [Shared conversation](https://claude.ai/share/c6a48a50-d511-4886-b4f5-0a3000f6a725) (needs sign-in; not read for these docs) | Recorded per question below | Listed under each question's Unknowns and Assumptions | [Artifact 3: Incident and Evidence Catalog](artifact-03-incident-and-evidence-catalog.md) |

## Where Claude and ChatGPT disagreed

TBD: the source documents do not record any disagreements. The team fills this in.

## Questions and answers

### Q3.1 A user opens an application, but nothing appears on the screen. Trace the journey of their request through the underlying software components.

**Frame.** Where can a request fail between the user opening the app and the screen rendering?

**Knowns.** The user opened the app and nothing appeared. S: it is a web application.

**Shared knowns referenced in this answer:**

- **S:** A Python web application is in production. An engineer deploys a new version that changes a configuration value. The application starts returning HTTP 500 errors. The monitoring system detects the incident. *(Reference scenario)*

**Unknowns.** All users or some; whether the page's HTML arrived; whether the browser shows errors; which browser or device; whether other pages load.

**Assumptions (provisional).**

- A load balancer or content delivery layer sits in front of the application. *Owner: Platform owner. Validated when: the reference application's request path is documented.*

**Answer.** The request passes through these stages; a blank screen means one of them failed:

1. **Name lookup:** the browser resolves the domain to an address. *Failure:* the site can't be found.
2. **Connection and encryption:** a secure connection is set up. *Failure:* certificate or connection errors.
3. **Edge:** the content delivery layer serves cached files, or forwards the request. *Failure:* stale or missing files.
4. **Load balancer:** routes the request to a healthy application instance. *Failure:* no healthy instances, so a 502/503 error.
5. **Web server and application:** middleware runs (authentication, logging), the handler runs, and it reads data. *Failure:* an exception, so an HTTP 500 error, as in S.
6. **Data store and dependencies:** the database and other services answer. *Failure:* timeouts, which surface as errors or slow pages.
7. **Response back to the browser:** HTML, then scripts and styles. *Failure:* the HTML arrives, but a script fails, so the page stays blank.
8. **Rendering in the browser:** scripts build the page. *Failure:* a browser-side error.

Engineers narrow it down from outside in: does the page load at all, what status code came back, does the server log show the request, and does the browser show errors.

### Q3.2 A web application receives thousands of simultaneous requests. Which responsibilities must the application components perform to process these requests?

**Frame.** What must the components do to handle high concurrency safely?

**Knowns.** Thousands of requests arrive at once. S: a Python web application.

**Shared knowns referenced in this answer:**

- **S:** A Python web application is in production. An engineer deploys a new version that changes a configuration value. The application starts returning HTTP 500 errors. The monitoring system detects the incident. *(Reference scenario)*

**Unknowns.** Peak request rate; mix of reads and writes; current capacity; the slowest dependency.

**Assumptions (provisional).**

- Application instances keep no user state in memory, so more can be added. *Owner: Platform owner. Validated when: the reference application is reviewed for in-memory state.*

**Answer.**

| **Component** | **Responsibility under load** |
| --- | --- |
| Load balancer | Spread requests across healthy instances; take failing instances out |
| Web server | Accept connections; limit concurrent work per instance; queue or reject the excess |
| Application | Handle each request independently; use timeouts on every outbound call; fail fast |
| Connection pools | Reuse a bounded number of database and service connections |
| Cache | Serve repeated reads without hitting the database |
| Database | Handle concurrent reads and writes safely; protect data consistency |
| Rate limiting | Protect the system from overload by shedding or delaying excess requests |
| Monitoring | Record request counts, errors and durations, so overload is visible |

The rule is **bounded work everywhere**: limited concurrency, limited queue size and timeouts. Without them, one slow dependency makes every request wait, and the whole service stalls.

### Q3.3 A user sends an HTTP request, but the application returns an error. What happens between receiving the request and returning the response?

**Frame.** Which steps run between receiving the request and returning the error?

**Knowns.** The request reached the system and an error came back. S: the application returns HTTP 500 errors.

**Shared knowns referenced in this answer:**

- **S:** A Python web application is in production. An engineer deploys a new version that changes a configuration value. The application starts returning HTTP 500 errors. The monitoring system detects the incident. *(Reference scenario)*

**Unknowns.** The exact status code; which layer produced it; whether it repeats for every request; whether the proxy's status matches the application's.

**Assumptions (provisional).**

- Requests pass through a proxy, middleware, and then a handler. *Owner: Platform owner. Validated when: the reference application's request path is documented.*

**Answer.**

1. **Proxy or load balancer** receives the request. It may return 502/503/504 itself if no instance answers.
2. **Web server** hands the request to the application.
3. **Middleware** runs: authentication, request parsing, logging. It may return 400, 401 or 403.
4. **Routing** finds the handler for the path. It may return 404.
5. **Handler** runs the business logic and **reads configuration**, the step where S's changed value takes effect.
6. **Calls to the database and other services.** Timeouts or failures here often become 500 or 504.
7. **Error handling:** an unhandled exception becomes a 500, and the error and stack trace are logged.
8. **Response** goes back through the middleware and proxy to the user.

**Status codes tell you where to look:**

- **4xx:** the request was rejected; likely a client problem.
- **500:** the application failed.
- **502/503/504:** the proxy couldn't get a good answer from the application.

In S, a 500 points to step 5 or 6.

### Q3.4 An application needs to receive requests from external clients. What component should expose the communication interface?

**Frame.** Which component should external clients talk to?

**Knowns.** External clients must reach the application. S: communication is over HTTP, since the application returns HTTP 500 errors.

**Shared knowns referenced in this answer:**

- **S:** A Python web application is in production. An engineer deploys a new version that changes a configuration value. The application starts returning HTTP 500 errors. The monitoring system detects the incident. *(Reference scenario)*

**Unknowns.** The types of client; authentication needs; whether a gateway already exists.

**Assumptions (provisional).**

- Clients reach one public address, not individual instances. *Owner: Platform owner. Validated when: the network layout of the reference application is documented.*

**Answer.** A single public entry point, a **load balancer or API gateway**, exposes the interface. The application instances sit behind it, and are not reachable directly.

The entry point is responsible for:

- the one stable address clients use
- secure connections
- routing to healthy instances
- basic protection: rate limits, and rejecting malformed requests

The application itself defines the interface: its paths, request and response formats, and status codes.

**Why it matters for incidents:** the entry point sees every request, so its counts and status codes are a primary signal. It can also produce its own errors (502/503/504), which differ from the application's 500s.

### Q3.5 An application needs to persist customer information even after restarting. What component should manage that information?

**Frame.** What should own data that must survive restarts?

**Knowns.** Customer data must outlive the application process.

**Unknowns.** Data volume; how much data may be lost and how fast it must be restored (recovery point and time objectives); legal or residency rules.

**Assumptions (provisional).**

- The data is structured and needs transactions. *Owner: Platform owner. Validated when: the reference application's data model is reviewed.*

**Answer.** A **database**, separate from the application process. It owns durable storage, transactions, consistency, backups and recovery. The application connects to it and holds no data it can't afford to lose. That is also why instances can be restarted or replaced freely (Q3.2).

**Why it matters for incidents:** the database is a common failure point. Connection failures, slow queries, exhausted connection pools and failed migrations all surface in the application as errors or slowness (Q3.35). Database changes in a release (migrations) also affect whether a rollback is safe (Q3.14).

### Q3.6 An engineer introduces a new version of an application. What sequence of operations takes place when that version is deployed?

**Frame. What steps take a new version from code to live traffic?**

**Knowns. An engineer is releasing a new version. S: the release changes a configuration value.**

**Shared knowns referenced in this answer:**

- **S: A Python web application is in production. An engineer deploys a new version that changes a configuration value. The application starts returning HTTP 500 errors. The monitoring system detects the incident.** ***(Reference scenario)***

**Unknowns. The rollout method; approvals required; whether database migrations are included; the rollback path.**

**Assumptions (provisional).**

- **An automated build-and-release pipeline exists.** ***Owner: Platform owner. Validated when: the release process is documented.***
- **Rollout is gradual, not all at once.** ***Owner: Platform owner. Validated when: the same review. If false, version-split evidence (Q3.34) is unavailable.***

**Answer.**

1. **Change merged: code and configuration changes are reviewed and merged.**
2. **Build: the application is packaged as a versioned artifact.**
3. **Tests: automated tests run; failures stop the release.**
4. **Approval, if required.**
5. **Configuration applied: new values are prepared for the target environment. This is S's change.**
6. **Database migrations, if any, run before or with the new code.**
7. **Rollout: new instances start with the new version and configuration. Traffic shifts to them, gradually or all at once.**
8. **Health checks: new instances must pass before they take traffic.**
9. **Old instances removed once the new ones are healthy.**
10. **Release recorded: version, time, author and configuration changes.**

**Each step is a point where a failure can be introduced, and step 10 is the record that later links an incident to its change (Q3.26).**

### Q3.7 An application behaves differently after an environment variable changes, even though its source code remains identical. How would you distinguish the impact of configuration from the impact of application code?

**Frame. How do we separate a configuration effect from a code effect?**

**Knowns.**

- **A variable changed, behaviour changed, and the source code is identical.**
- **S: this is the reference scenario, where the deployment changed a configuration value.**
- **A2-c: changes can be tested in a disposable environment.**

**Shared knowns referenced in this answer:**

- **S: A Python web application is in production. An engineer deploys a new version that changes a configuration value. The application starts returning HTTP 500 errors. The monitoring system detects the incident.** ***(Reference scenario)***
- **A2-c: Fixes are tested in a disposable, isolated environment; synthetic test cases come first.** ***(Artifact 2, Q2.7, Q2.14)***

**Unknowns. Which variable changed; when the application reads it (at startup or per request); how widely it applies; whether configuration changes are recorded with time and author.**

**Assumptions (provisional).**

- **Configuration changes are recorded separately from code changes.** ***Owner: Platform owner. Validated when: the release record shows configuration differences per release.***

**Answer.**

1. **Compare versions: confirm the code is identical between the working and failing versions, and list every configuration difference.**
2. **Find where the value is used: locate the code that reads the changed variable, and check whether the failing requests go through it.**
3. **Match the error: check whether errors or stack traces mention the variable, or the behaviour it controls.**
4. **Test in isolation: in a disposable environment, run the same code with the old value, then the new value. If the old value works and the new one fails, configuration is the cause.**
5. **Check timing: behaviour changed when the configuration took effect, which may be at restart, not at deploy.**

**Same code with a different value producing different behaviour is the controlled comparison that proves the configuration caused it.**

### Q3.8 A deployment introduces a regression and causes previously working functionality to fail. How would you investigate and isolate the change responsible?

Frame. How do we find which change caused the regression?

Knowns.

- A deployment happened, and functionality that worked now fails.
- S: the deployment changed a configuration value, and the application returns HTTP 500 errors.

Shared knowns referenced in this answer:

- S: A Python web application is in production. An engineer deploys a new version that changes a configuration value. The application starts returning HTTP 500 errors. The monitoring system detects the incident. *(Reference scenario)*

Unknowns. The exact start time; which code commits and configuration changes are in the release; any other changes in the same time window.

Assumptions (provisional).

- The deployment is the likely cause, but this is not yet proven. *Owner: SRE lead. Validated when: the evidence in Q3.34 is collected.*

Answer.

1. Pin the start time from metrics, which may be earlier than the alert.
2. List every change in that window: the release (commits and configuration), other deployments, infrastructure and dependency changes.
3. Check the version split: do errors come only from instances on the new version?
4. Match errors to changes: stack traces and failing paths that point at changed code or configuration.
5. Narrow it down: if the release has several changes, test them one at a time in a disposable environment, configuration first, since that's cheapest to test. Alternatively, search through the commits in halves.
6. Confirm by reproducing the failure with the suspect change and clearing it without, in a safe environment.

In S, step 2 finds one configuration change, and step 5 confirms it.

### Q3.9 A production application suddenly stops responding to users. What conditions would cause the engineering team to declare and track this situation as an incident?

**Frame.** What conditions make this a tracked incident?

**Knowns.** Production has stopped responding to users. S: monitoring detects incidents.

**Shared knowns referenced in this answer:**

- **S:** A Python web application is in production. An engineer deploys a new version that changes a configuration value. The application starts returning HTTP 500 errors. The monitoring system detects the incident. *(Reference scenario)*

**Unknowns.** The organisation's severity levels; who may declare an incident; its service-level objectives; how many users are affected.

**Assumptions (provisional).**

- The team has an on-call process. *Owner: Engineering manager. Validated when: the on-call rota is reviewed.*

**Answer.** Declare an incident when any of these is true:

- **Users are affected:** a critical journey fails, or the error rate or latency breaches the service objective.
- **The objective is at risk:** the error budget is burning fast enough to be exhausted soon.
- **The impact is significant:** many users, important customers, revenue or data are affected.
- **It needs coordination:** more than one person or team is needed.
- **It's ongoing or uncertain:** the cause is unknown, or the problem is spreading.

Once declared, the incident gets one record, an owner, a severity and a timeline. A service that has stopped responding meets the first condition immediately.

### Q3.10 The application is technically running, but 40% of requests are failing. How should the engineering team distinguish this from a complete outage?

**Frame.** How does partial failure differ from a full outage?

**Knowns.** The application is running, and 40% of requests fail.

**Unknowns.** Which users, paths, regions or instances fail; whether failures are random or patterned; how severity levels are defined (Q3.9).

**Assumptions (provisional).**

- The 40% figure is measured over all requests, not a sample. *Owner: SRE lead. Validated when: the error-rate metric's definition is checked.*

**Answer.**

|  | **Complete outage** | **Partial failure (this case)** |
| --- | --- | --- |
| Health checks | Fail | Often pass |
| Error rate | About 100% | Partial (40%) |
| Users | Everyone affected | Some users, paths or regions |
| Typical causes | Crash, network, total dependency loss | One bad instance, one failing path, a bad version on some instances, a partly failing dependency |

To tell them apart, break the error rate down:

- **By instance:** is it one bad host?
- **By version:** is it a partial rollout?
- **By path:** is it one feature?
- **By region or customer group.**

A pattern points to the cause. Random failure across everything points to a shared dependency. Partial failure is often harder to detect, because health checks pass while users suffer.

### Q3.11 An application remains available, but its response time increases from 100 ms to 8 seconds. How should the engineering team characterize this situation?

**Frame.** What do we call a working but very slow service?

**Knowns.** Latency rose from 100 ms to 8 seconds, and the service is still up.

**Unknowns.** Which percentile these numbers describe; the latency objective; whether clients are timing out.

**Assumptions (provisional).**

- 100 ms and 8 seconds describe typical requests (the median), not rare outliers. *Owner: SRE lead. Validated when: the metric's percentile is confirmed.*

**Answer.** A **latency degradation**, and almost certainly an incident.

- An 80-fold rise breaks any reasonable latency objective.
- Many clients time out before 8 seconds, so for them it is effectively an outage.
- Slow requests hold connections and threads, so it can turn into a full outage under load (Q3.2).

Characterise it by:

- **How slow:** the median and the 95th/99th percentile.
- **How widespread:** all requests, or some paths or users.
- **Whether timeouts are happening:** errors hidden behind the slowness.

Usual causes: a slow dependency, resource saturation (Q3.23), exhausted connection pools, or a change that added expensive work.

### Q3.12 Engineers have collected logs, traces and deployment information but have not yet identified the underlying cause. What investigation process should they follow?

**Frame.** How do we move from collected evidence to a tested cause?

**Knowns.** Logs, traces and deployment data exist, and the cause is unknown. S: there was a deployment with a configuration change.

**Shared knowns referenced in this answer:**

- **S:** A Python web application is in production. An engineer deploys a new version that changes a configuration value. The application starts returning HTTP 500 errors. The monitoring system detects the incident. *(Reference scenario)*
- **A1-a:** Any change to production needs approval from a named, accountable person. *(Artifact 1, 1.5)*
- **A2-c:** Fixes are tested in a disposable, isolated environment; synthetic test cases come first. *(Artifact 2, Q2.7, Q2.14)*

**Unknowns.** What evidence is still missing; what is still changing; whether impact is growing.

**Assumptions (provisional).**

- The evidence is accurate, and timestamps across sources use the same clock. *Owner: Platform owner. Validated when: clocks across sources are checked against each other.*

**Answer.**

1. **Limit the damage first:** if impact is growing, consider a safe, approved mitigation (A1-a) before the cause is proven.
2. **Build a timeline:** when it started, and what changed just before.
3. **Describe the symptom precisely:** what fails, for whom, and since when.
4. **List hypotheses:** at least two, including "not the deployment".
5. **For each, write down** what you would expect to see if it were true, and what would rule it out.
6. **Check the evidence against each:** look for confirming evidence (Q3.34) and contradicting evidence (Q3.35).
7. **Test the leading hypothesis** in a safe environment (A2-c).
8. **Conclude only when the evidence separates the hypotheses.** If it can't, escalate (Q3.37).

The discipline is to test hypotheses, not to collect more data hoping the answer appears.

### Q3.13 Engineers identify the cause of an incident but still need to restore normal service. What actions could resolve the problem?

**Frame.** What actions restore service once the cause is known?

**Knowns.** The cause is identified, and service is not yet normal. A1-a: production changes need approval.

**Shared knowns referenced in this answer:**

- **S:** A Python web application is in production. An engineer deploys a new version that changes a configuration value. The application starts returning HTTP 500 errors. The monitoring system detects the incident. *(Reference scenario)*
- **A1-a:** Any change to production needs approval from a named, accountable person. *(Artifact 1, 1.5)*
- **A1-b:** Recovery is verified independently against the original symptom, held for a set period. *(Artifact 1, UC7)*

**Unknowns.** Which options are available for this service; how risky each is; who approves each.

**Assumptions (provisional).**

- The identified cause is correct. *Owner: SRE lead. Validated when: recovery is verified after the fix (A1-b).*

**Answer.**

| **Action** | **When it fits** | **Risk** |
| --- | --- | --- |
| **Revert the configuration value** | The cause is a configuration change (S) | Low; fast |
| **Roll back the release** | A code or bundled change is the cause | Low, unless the database changed (Q3.14) |
| **Turn off a feature flag** | The failing feature is behind a flag | Low |
| **Roll forward with a fix** | Rollback isn't safe, and the fix is small | Medium; needs a review |
| **Restart or replace instances** | Stuck processes, or leaked resources | Low, but may only buy time |
| **Scale out** | The cause is overload | Low; costs money |
| **Fail over or route around** | A dependency or region has failed | Medium |
| **Shed load or rate-limit** | Protect core features during overload | Some users degraded |

Prefer the **smallest, most reversible action** that targets the confirmed cause. Every action is approved (A1-a) and verified against the original symptom (A1-b). In S, that means reverting the configuration value.

### Q3.14 A new deployment causes failures across the application. How could the engineering team return to a previously working version?

**Frame.** How do we get back to a working version?

**Knowns.** A new deployment caused failures across the application. S: the deployment changed a configuration value. A1-a: approval required.

**Shared knowns referenced in this answer:**

- **S:** A Python web application is in production. An engineer deploys a new version that changes a configuration value. The application starts returning HTTP 500 errors. The monitoring system detects the incident. *(Reference scenario)*
- **A1-a:** Any change to production needs approval from a named, accountable person. *(Artifact 1, 1.5)*
- **A1-b:** Recovery is verified independently against the original symptom, held for a set period. *(Artifact 1, UC7)*

**Unknowns.** Whether the previous build is still available; whether database migrations can be reversed; how long a rollback takes; whether configuration is versioned with the release and can be reverted on its own.

**Assumptions (provisional).**

- The previous version was working before the deployment. *Owner: SRE lead. Validated when: metrics before the deployment show normal behaviour.*

**Answer.**

1. **Choose the smallest reversal.** If only configuration changed (S), revert that value, without redeploying the code.
2. **Otherwise, redeploy the previous version** from its stored build, using the normal release process.
3. **Check database changes first.** If the release ran a migration, confirm the old version works with the new schema, or reverse the migration safely. This is the main way a rollback can make things worse.
4. **Roll out the reversal** with health checks, as in a normal release.
5. **Verify recovery** against the original symptom (A1-b).
6. **Record the rollback**, and keep the failing version for investigation.

Configuration and code should be revertable separately. In S, that makes the fix a single value change rather than a full redeploy.

### Q3.15 An incident passes through several operational stages, from detection to closure. How would you define and track this lifecycle?

**Frame.** What stages does an incident pass through, and how are they tracked?

**Knowns.** Incidents run from detection to closure. A1-b: recovery is verified before closing.

**Shared knowns referenced in this answer:**

- **A1-b:** Recovery is verified independently against the original symptom, held for a set period. *(Artifact 1, UC7)*

**Unknowns.** Who owns each stage; which timings the organisation reports; how incidents are recorded today.

**Assumptions (provisional).**

- One record per incident is acceptable to the teams. *Owner: Engineering manager. Validated when: the teams agree on the incident record.*

**Answer.**

| **Stage** | **Ends when** | **Timing measured** |
| --- | --- | --- |
| Detected | Alert fires or a user reports | — |
| Acknowledged | A person takes ownership | Time to acknowledge |
| Triaged | Severity and impact are set | — |
| Investigating | A cause is identified | Time to identify the cause |
| Mitigating | A fix is applied | Time to mitigate |
| Verifying | Recovery is proven and holds | — |
| Resolved | The owner accepts recovery | Time to resolve |
| Reviewed | A post-incident review is complete | — |
| Closed | Follow-up actions are tracked | — |

Each incident has one record, with a timestamped entry for every stage change, the owner, decisions and actions. That record is what gives the timings, and what the review uses.

### Q3.16 An incident cannot be resolved by the first engineer who investigates it. How should responsibility move to another engineer or team?

**Frame.** How does ownership transfer without anything being dropped?

**Knowns.** The first engineer can't resolve the incident.

**Unknowns.** The on-call structure; escalation time limits; who the next owner is.

**Assumptions (provisional).**

- A second engineer or team can be reached. *Owner: Engineering manager. Validated when: the escalation path is documented and tested.*

**Answer.**

1. **Escalate on clear triggers:**
    - a time limit passes without progress
    - the severity increases
    - the cause is in another team's area
    - the needed access or expertise is missing
2. **Choose the next owner** from the escalation path: the owning team's on-call, then a senior engineer, then the incident lead.
3. **Hand over explicitly:**
    - current impact
    - the timeline
    - what was checked
    - hypotheses tested and ruled out
    - actions taken
    - open questions
4. **Make the new owner confirm** they have taken it over. Until they do, the first engineer still owns it.
5. **Record the transfer** in the incident record.

The record carries the context, so the next engineer doesn't start from zero.

### Q3.17 Engineers need to understand what happened inside an application after an error occurred. What information should the application record about individual events?

**Frame.** What should each logged event contain?

**Knowns.** Engineers need to reconstruct what happened after an error. S: the application returns HTTP 500 errors.

**Shared knowns referenced in this answer:**

- **S:** A Python web application is in production. An engineer deploys a new version that changes a configuration value. The application starts returning HTTP 500 errors. The monitoring system detects the incident. *(Reference scenario)*

**Unknowns.** The current log format; how long logs are kept; privacy rules.

**Assumptions (provisional).**

- Logs can be structured and searched centrally. *Owner: Platform owner. Validated when: the logging setup is reviewed.*

**Answer.** Each event should record:

- **Timestamp:** precise, with a time zone.
- **Severity:** debug, info, warning or error.
- **Service, instance and version:** so errors can be tied to a release.
- **Request ID and trace ID:** to link events in one request, across services.
- **What happened:** a message and an event type.
- **Request context:** path, method, status code and duration.
- **For errors:** the error type, the message, and the stack trace.
- **Relevant configuration:** for example, which settings were in effect. In S, this links the error to the changed value.
- **A user or tenant reference:** hashed or pseudonymous, never personal data in clear.

Use a structured format (key–value), so events can be searched and counted. Never log passwords, tokens or personal data.

### Q3.18 Management wants to know how many requests failed during the previous hour. What numerical measurements should the application expose?

**Frame.** Which numbers answer "how many failed in the last hour?"

**Knowns.** Management wants a count for the previous hour. S: failures are HTTP 500 errors.

**Shared knowns referenced in this answer:**

- **S:** A Python web application is in production. An engineer deploys a new version that changes a configuration value. The application starts returning HTTP 500 errors. The monitoring system detects the incident. *(Reference scenario)*

**Unknowns.** What counts as "failed"; whether 4xx responses are included.

**Assumptions (provisional).**

- Metrics are kept for at least the reporting period. *Owner: Platform owner. Validated when: metric retention is checked.*

**Answer.** Expose **request counters**, broken down by status code, path, service and version:

- **Total requests:** needed to turn a count into a rate.
- **Failed requests:** usually server errors (5xx). Count client errors (4xx) separately, since they're often not the service's fault.

From these, two numbers answer the question:

- **Failures in the last hour:** the increase in the failed-request counter over that hour.
- **Error rate:** failed requests divided by total requests.

Agree the definition of "failed" once, and use it everywhere.

### Q3.19 A request passes through five different services, and the final service returns an error. How could engineers reconstruct the journey of that request?

**Frame.** How do we follow one request across five services?

**Knowns.** The request crossed five services, and the last one returned an error.

**Unknowns.** Whether tracing exists; the sampling rate; whether queues are involved.

**Assumptions (provisional).**

- All five services can be instrumented. *Owner: Platform owner. Validated when: each service's team confirms.*

**Answer.** Use **distributed tracing**:

1. **Assign an ID at the edge:** the first service gives the request a unique trace ID.
2. **Pass it along:** every service passes the ID to the next, in request headers or in queue messages.
3. **Record timed steps:** each service records the steps it ran ("spans") with the trace ID, start time, duration and outcome.
4. **Assemble the journey:** a tracing system groups the spans by trace ID, and shows which service took how long and where the error started.

Logs should carry the same trace ID (Q3.17), so a trace links to the detailed log events.

**Watch for:**

- **Sampling:** the failing request may not have been recorded. Keep all error traces.
- **Queues:** these break the chain unless the ID is carried in the message.

### Q3.20 Engineers have application logs but cannot determine how frequently a failure occurs. Why are logs alone insufficient?

**Frame.** Why can't logs alone show how often a failure happens?

**Knowns.** Logs exist, but failure frequency can't be determined.

**Unknowns.** Whether logs are sampled or dropped; whether total request counts exist anywhere.

**Assumptions (provisional).**

- Logs record individual events, but there is no total request count to compare against. *Owner: Platform owner. Validated when: available metrics are listed.*

**Answer.**

- **No denominator:** logs show failures, but not how many requests succeeded. "500 errors" means nothing without knowing whether there were 1,000 requests or 1,000,000.
- **Incomplete:** logs are often sampled, rate-limited or dropped under load, exactly when failures spike.
- **Inconsistent:** not every path logs its errors, and some errors are logged more than once.
- **Expensive to count:** counting means searching large volumes of text, which is slow and costly at scale.

**Metrics** solve this: counters for total and failed requests give rates and trends cheaply (Q3.18). Logs explain *why* one event failed; metrics show *how often*.

### Q3.21 Users complain that the application is slow, but the engineering team has no measurement of response duration. Which metric should be collected?

**Frame.** Which metric measures slowness?

**Knowns.** Users report slowness, and response duration is not measured.

**Unknowns.** Which journeys are slow; whether the delay is on the server or in the browser.

**Assumptions (provisional).**

- The slowness is at least partly on the server side. *Owner: SRE lead. Validated when: server-side duration is measured.*

**Answer.** Collect **request duration as a histogram**, broken down by path, status code, service and version. From it, report the 50th, 95th and 99th percentiles:

- Averages hide slow outliers.
- Percentiles show what most users see, and what the slowest see.

Also measure:

- **The duration of calls to dependencies:** database and other services, to find where the time goes.
- **Browser-side load time:** if the server is fast but users still see slowness.

### Q3.22 An application suddenly starts returning errors for 30% of requests. Which measurements would help establish the severity of the problem?

**Frame.** Which measurements establish how bad a 30% error rate is?

**Knowns.** 30% of requests suddenly fail. S: the failures are HTTP 500 errors.

**Shared knowns referenced in this answer:**

- **S:** A Python web application is in production. An engineer deploys a new version that changes a configuration value. The application starts returning HTTP 500 errors. The monitoring system detects the incident. *(Reference scenario)*

**Unknowns.** How many users are affected; which critical journeys are affected; how long it has lasted; the business cost.

**Assumptions (provisional).**

- The 30% is measured correctly, and isn't mostly retries or automated traffic. *Owner: SRE lead. Validated when: errors are broken down by client type.*

**Answer.**

- **Error rate and its trend:** is it rising, steady or falling?
- **Users affected:** unique users or sessions with errors, not just request counts.
- **Critical journeys affected:** for example, sign-in and checkout versus minor pages.
- **Duration so far:** how much of the error budget is being consumed.
- **Breakdown:** by path, region, customer group and version (Q3.10).
- **Business signals:** failed transactions, lost revenue, support contacts.
- **Spread:** whether other services or dependencies are affected.

Severity combines **breadth** (how many), **depth** (how critical) and **duration** (how long). A 30% error rate on checkout is far worse than 30% on a rarely used page.

### Q3.23 A service becomes unavailable because its CPU is saturated. Which resource measurements would help identify this condition?

**Frame.** Which resource measurements reveal CPU saturation?

**Knowns.** The service is unavailable, and CPU saturation is given as the reason.

**Unknowns.** Whether the high CPU started before the unavailability (cause) or after (symptom); whether limits are throttling the service.

**Assumptions (provisional).**

- The service runs in containers with CPU limits. *Owner: Platform owner. Validated when: the reference application's runtime settings are checked.*

**Answer.**

- **CPU utilisation:** per instance and per container, against its limit.
- **CPU throttling:** time the container was prevented from running because it hit its limit.
- **Run queue or load:** work waiting for CPU.
- **Request rate and duration:** to see whether load rose, or the work per request rose.
- **Process-level CPU:** which part of the application is using the CPU.
- **Garbage-collection time:** often shows up as CPU in managed runtimes such as Python.

Confirm the order of events: CPU rose **before** errors and slowness began, not after. Then ask what drove it: more traffic, or a change that made each request more expensive.

### Q3.24 An application starts crashing because it consumes increasing amounts of memory. Which measurements would help diagnose this behavior?

**Frame.** Which measurements diagnose crashes caused by growing memory use?

**Knowns.** The application crashes, and its memory use keeps growing.

**Unknowns.** Whether it's a leak or load-related; when the growth began; the memory limit.

**Assumptions (provisional).**

- The crashes are caused by running out of memory. *Owner: SRE lead. Validated when: crash records show out-of-memory kills.*

**Answer.**

- **Memory use over time** per instance, against the limit. A steady climb that never falls is a leak; one that tracks traffic is load.
- **Out-of-memory kill and restart counts:** confirm memory is the cause of the crashes.
- **Memory after each garbage collection:** if even that keeps rising, objects are being kept.
- **Request rate:** to separate load from leak.
- **When the growth began:** compare with release times (Q3.26).
- **Memory profile or snapshot:** which objects are accumulating. Take it in a safe environment if possible.

### Q3.25 A background processing system accumulates thousands of pending jobs. How would engineers measure and detect this accumulation?

**Frame.** How do we measure and detect a job backlog?

**Knowns.** Thousands of jobs are pending.

**Unknowns.** Normal queue size; acceptable delay; whether the workers are healthy.

**Assumptions (provisional).**

- The queue exposes its depth and the age of its jobs. *Owner: Platform owner. Validated when: the queue's metrics are checked.*

**Answer.**

- **Queue depth:** jobs waiting.
- **Oldest job's age:** how long the oldest has waited. This matters more than depth, because it is what users feel.
- **Arrival rate vs completion rate:** a backlog grows when arrival exceeds completion.
- **Worker health:** number of active workers, failures, retries and processing time per job.
- **Failed and retried jobs:** retries can multiply the load.

**Alert on age, and on sustained growth:** for example, the oldest job is older than the agreed delay, or depth has grown for a set period. A large but stable depth may be normal.

### Q3.26 A failure begins immediately after a deployment. What deployment information should be available to correlate changes with incidents?

**Frame.** What deployment data lets us connect changes to incidents?

**Knowns.**

- The failure started right after a deployment.
- S: the deployment changed a configuration value, so configuration changes must be recorded alongside code changes.

**Shared knowns referenced in this answer:**

- **S:** A Python web application is in production. An engineer deploys a new version that changes a configuration value. The application starts returning HTTP 500 errors. The monitoring system detects the incident. *(Reference scenario)*

**Unknowns.** What is recorded today; whether feature-flag changes are included.

**Assumptions (provisional).**

- Deployment events can be captured automatically from the release process. *Owner: Platform owner. Validated when: the release process emits a record for each deployment.*

**Answer.** For every deployment, record:

- **When:** start time, the time each stage was reached, and the finish time.
- **What:** version, commits, and **configuration differences**: old and new values per setting.
- **Who:** author and approver.
- **Where:** service, environment, and which instances got the new version, and when.
- **How:** rollout method and percentage over time.
- **Other changes:** database migrations, feature-flag changes and infrastructure changes, recorded the same way.
- **Outcome:** succeeded, rolled back or failed, with links to the change itself.

All of these need the same clock as the monitoring data, so changes and errors can be lined up exactly.

### Q3.27 A deployment happens at exactly the same time that error rates increase. Why is this insufficient by itself to prove that the deployment caused the incident?

**Frame.** Why isn't matching timing proof of cause?

**Knowns.** A deployment and a rise in errors happened at the same time.

**Unknowns.** What else changed in that window; whether the errors are limited to the new version; whether rollback helps.

**Assumptions (provisional).**

- Deployment and error timestamps are accurate, and use the same clock. *Owner: Platform owner. Validated when: clocks across sources are compared.*

**Answer.** Timing shows correlation, not cause:

- **Other changes coincide:** infrastructure, dependencies, traffic or other teams' deployments in the same window.
- **The deployment itself is a disruption:** restarting instances can expose an unrelated, existing fault.
- **Shared triggers:** a traffic spike may have triggered both the deployment and the errors.
- **Coincidence:** deployments are frequent, so overlaps happen by chance.
- **Clock differences:** sources out of sync can create false alignment.

Proof needs evidence that links the change to the failure mechanism (Q3.34) and the absence of contradicting evidence (Q3.35).

#### F. Tools the platform reads from

### Q3.28 An engineering team needs to collect numerical measurements from its applications and expose them for querying. Which responsibilities should its metrics collection system perform?

**Frame.** What must a metrics system do?

**Knowns.** Measurements must be collected and made queryable. A2-b: the organisation already has monitoring, which the platform reads from.

**Shared knowns referenced in this answer:**

- **A2-b:** Evidence is read from existing monitoring with read-only access; alerts come from existing monitoring. *(Artifact 2, Q2.4–Q2.6)*

**Unknowns.** Number of services; data volume; retention needs; existing tools.

**Assumptions (provisional).**

- Applications can expose metrics. *Owner: Platform owner. Validated when: the reference application exposes request metrics.*

**Answer.**

- **Collect:** gather measurements from applications and infrastructure on a schedule, or as they are sent.
- **Label:** attach service, instance, version, path and status, so data can be broken down (Q3.10).
- **Store:** keep time series with retention that suits the use, short for detail and long for trends.
- **Query:** compute rates, percentiles and comparisons over time windows.
- **Alert:** evaluate rules and send alerts when thresholds or objectives are breached.
- **Expose to other tools:** dashboards and, read-only, the AI SRE platform (A2-b).
- **Stay reliable:** it must keep working during incidents, the moment it's needed most.

### Q3.29 Engineers want to visualize service health, error rates and resource consumption. Which capabilities should their monitoring dashboard provide?

**Frame.** What must a monitoring dashboard offer?

**Knowns.** Engineers want to see health, errors and resource use. A2-b: existing monitoring stays in place.

**Shared knowns referenced in this answer:**

- **S:** A Python web application is in production. An engineer deploys a new version that changes a configuration value. The application starts returning HTTP 500 errors. The monitoring system detects the incident. *(Reference scenario)*
- **A2-b:** Evidence is read from existing monitoring with read-only access; alerts come from existing monitoring. *(Artifact 2, Q2.4–Q2.6)*

**Unknowns.** The audiences; data sources; existing dashboards.

**Assumptions (provisional).**

- The underlying metrics already exist. *Owner: Platform owner. Validated when: metrics are listed per service.*

**Answer.**

- **Service overview:** request rate, error rate and latency per service. These are the core signals.
- **Resources:** CPU, memory, saturation and restarts per instance.
- **Breakdowns:** by version, path, region and instance, to spot partial failures (Q3.10).
- **Time controls:** zoom to the incident window, and compare with an earlier normal period.
- **Change markers:** deployments and configuration changes drawn on the graphs. These are crucial for S.
- **Objectives:** the targets and error budget shown on the graphs.
- **Links:** from a graph to the related logs and traces.
- **Views for different audiences:** a summary for management, and detail for on-call engineers.

### Q3.30 An organization operates multiple services and needs to reconstruct request journeys across them. Which instrumentation and telemetry standards would support this?

**Frame.** Which standards support tracing across services?

**Knowns.** There are multiple services, and request journeys must be reconstructed (Q3.19). A2-a: Python applications only, for now.

**Shared knowns referenced in this answer:**

- **A2-a:** One organisation; Python applications only; one deployment environment. *(Artifact 2, Q2.1–Q2.3)*

**Unknowns.** Other languages in use; any existing tracing vendor; asynchronous paths.

**Assumptions (provisional).**

- All teams can adopt one standard. *Owner: Engineering manager. Validated when: the teams agree.*

**Answer.**

- **OpenTelemetry:** an open, vendor-neutral standard for producing traces, metrics and logs, with libraries for Python and most languages. Teams instrument once, and can send the data to any compatible backend.
- **W3C Trace Context:** the standard request headers that carry the trace ID between services, so traces join up across services and tools.
- **Consistent naming:** shared names for services, operations and attributes (OpenTelemetry's semantic conventions), so data from different teams lines up.
- **Carrying context through queues:** the trace context is placed in messages, as well as in HTTP headers.

Using open standards keeps the data portable, and means the AI SRE platform can read it without depending on one vendor.

### Q3.31 The organization already uses an external error-tracking platform. How should the AI SRE platform consume information from it?

**Frame.** How should the AI SRE platform read from the error tracker?

**Knowns.**

- An external error tracker is already in use.
- A2-b: the platform reads existing tools with read-only access, and doesn't replace them.

**Shared knowns referenced in this answer:**

- **S:** A Python web application is in production. An engineer deploys a new version that changes a configuration value. The application starts returning HTTP 500 errors. The monitoring system detects the incident. *(Reference scenario)*
- **A2-b:** Evidence is read from existing monitoring with read-only access; alerts come from existing monitoring. *(Artifact 2, Q2.4–Q2.6)*

**Unknowns.** Which tracker; its API limits; access rules; what personal data it holds.

**Assumptions (provisional).**

- It offers an API or event notifications. *Owner: Platform owner. Validated when: the tracker's interface is reviewed.*

**Answer.**

- **Read-only access** through the tracker's official API, with its own named, least-privilege identity (A2-b).
- **On demand, for the incident:** fetch the error groups for the affected service and time window. Don't copy everything.
- **Key fields:** error type and message, stack trace, first and last seen, count, affected version, and the release it first appeared in. The last two are decisive for S.
- **Event notifications, optionally:** use them only to learn about new error types quickly.
- **Respect limits:** stay within the API's rate limits, and remove personal data before storing anything.
- **Keep the source:** save a link to the original error with each piece of evidence.
- **Handle outages:** if the tracker is unavailable, record the gap and continue (Q3.36).

### Q3.32 The AI SRE platform needs information from several operational tools without directly depending on their internal implementations. How should this integration be designed?

**Frame.** How do we connect to many tools without depending on their internals?

**Knowns.** The platform needs data from several operational tools. A2-b: read-only access to existing tools.

**Shared knowns referenced in this answer:**

- **A2-b:** Evidence is read from existing monitoring with read-only access; alerts come from existing monitoring. *(Artifact 2, Q2.4–Q2.6)*

**Unknowns.** Which tools; which fields they have in common; how each one fails.

**Assumptions (provisional).**

- Each tool has a stable public interface. *Owner: Platform owner. Validated when: each tool's interface is reviewed.*

**Answer.** At the level this layer allows:

- **One adapter per tool** that talks only to its official public interface, never its database or internal formats.
- **A common shape for evidence:** every adapter returns the same kinds of record (alert, metric series, log events, trace, change event, error group), each with source, time and a link.
- **Each adapter handles its own tool's quirks:** authentication, limits, paging and time zones.
- **Failures are explicit:** an adapter reports "unavailable" or "partial data", never empty results that look like "healthy" (Q3.36).
- **Replaceable:** changing a tool means changing one adapter; the investigation logic doesn't change.

The detailed design belongs to Layers 6 and 7.

### Q3.33 For each incident, which information should the platform collect before proposing a diagnosis?

**Frame.** What's the minimum evidence needed before the platform can suggest a cause?

**Knowns.**

- There is an incident, and the platform proposes a diagnosis after collecting information.
- A1-c: thin evidence means hand over, not guess.
- A2-d: first summary within 5 minutes.

**Shared knowns referenced in this answer:**

- **A1-c:** When evidence is too thin, the cause is not guessed; the incident is handed to a person. *(Artifact 1, UC4)*
- **A2-d:** First summary within 5 minutes; full investigation within 15 minutes; a model-call budget per incident. *(Artifact 2, Q2.16, Q2.18)*

**Unknowns.** Which sources it can reach; how far back to look; how much evidence is enough.

**Assumptions (provisional).**

- Alerts can be linked to a service, and timestamps across tools use the same clock. *Owner: Platform owner. Validated when: alerts carry a service label, and clocks are compared.*

**Answer.** Collect these in order, recording anything unavailable as missing:

1. **The alert:** what fired, when, its severity and threshold.
2. **Impact:** users, regions, paths and journeys affected, and whether it's spreading.
3. **Service context:** owner, dependencies, normal behaviour, and any runbook.
4. **Timeline:** when the problem began, which may be earlier than the alert.
5. **Changes:** deployments, configuration, feature flags, migrations and infrastructure changes.
6. **Telemetry:** metrics, logs and traces from before and during the incident.
7. **Dependencies:** database, third-party and platform health.
8. **History:** similar past incidents and how they were resolved.

Each item keeps its source, its time, and a link to the original record.

### Q3.34 A hypothesis suggests that a recent deployment caused the incident. Which observations would strengthen this hypothesis?

**Frame.** What evidence would show the deployment really caused the incident, beyond matching timing?

**Knowns.**

- There was a recent deployment, and it is only a hypothesis.
- S: the deployment changed a configuration value.
- A1-a: production changes need approval.
- A2-c: tests run in a disposable environment.

**Shared knowns referenced in this answer:**

- **S:** A Python web application is in production. An engineer deploys a new version that changes a configuration value. The application starts returning HTTP 500 errors. The monitoring system detects the incident. *(Reference scenario)*
- **A1-a:** Any change to production needs approval from a named, accountable person. *(Artifact 1, 1.5)*
- **A2-c:** Fixes are tested in a disposable, isolated environment; synthetic test cases come first. *(Artifact 2, Q2.7, Q2.14)*

**Unknowns.** What exactly changed; how far the rollout went; whether anything else changed.

**Assumptions (provisional).**

- Telemetry is labelled by version, and deployment records are accurate. *Owner: Platform owner. Validated when: metrics and logs carry the version.*

**Answer.** These observations strengthen it, strongest first:

1. **Reproduction:** in a disposable environment, the failure appears with the new version or configuration, and disappears with the old one. **Re-triggering is done only in that environment, never in production.**
2. **Version split:** errors come from instances on the new version, while old-version instances stay healthy. *This is only available if the rollout was gradual (Q3.6).*
3. **Config match:** the changed configuration value is read on the failing code path, and the error refers to it, or to what it controls. *Decisive for S.*
4. **Code match:** stack traces and failing traces point to the changed code.
5. **New error signature:** this error didn't exist before the release.
6. **Scale match:** impact grows with the rollout percentage.
7. **Rollback clears it:** errors stop after an approved rollback. Recovery could still be coincidence, so confirm against the evidence above.
8. **Timing:** the failure starts once the rollout reaches traffic. On its own this is the weakest evidence (Q3.27).

### Q3.35 The deployment happened immediately before the incident, but the application logs show an unrelated database connection failure. Which observations would contradict the original hypothesis?

**Frame.** Given the timing and the database failure, what would show the deployment wasn't the cause?

**Knowns.** The deployment came immediately before the incident, and the logs show a database connection failure described as unrelated.

**Shared knowns referenced in this answer:**

- **S:** A Python web application is in production. An engineer deploys a new version that changes a configuration value. The application starts returning HTTP 500 errors. The monitoring system detects the incident. *(Reference scenario)*

**Unknowns.** Whether the database fault is truly unrelated. The release could have caused it, for example through a new query, a connection leak, a changed connection setting, or a migration.

**Assumptions (provisional).**

- Log timestamps are accurate. *Owner: Platform owner. Validated when: clocks are compared across sources.*

**Answer.** These observations contradict it:

- Database errors began **before** the deployment reached traffic.
- Old-version instances fail the same way.
- Other services that **weren't deployed** but share the database also fail.
- The database shows its own cause: a failover, maintenance, a storage limit, or a provider outage.
- Rolling back makes no difference.
- The failing paths don't touch the changed code or configuration.
- The release contains no database, connection, pool or query changes, **including configuration**. In S, check whether the changed value is a database setting.

**Test the label "unrelated" too.** If connections rose only on the new version, or the changed configuration value controls the database connection, the deployment may still be the cause, through the database failure. Report the two explanations side by side until the evidence separates them.

### Q3.36 The monitoring provider becomes unavailable during the investigation. How should the platform proceed?

**Frame.** How should the investigation continue when a key evidence source disappears?

**Knowns.**

- The provider went down during an investigation.
- A1-c: hand over when evidence is too thin.
- A2-d: the investigation is time-limited.

**Shared knowns referenced in this answer:**

- **A1-c:** When evidence is too thin, the cause is not guessed; the incident is handed to a person. *(Artifact 1, UC4)*
- **A2-d:** First summary within 5 minutes; full investigation within 15 minutes; a model-call budget per incident. *(Artifact 2, Q2.16, Q2.18)*

**Unknowns.** How long the outage will last; what other sources exist; whether the outage is related to the incident.

**Assumptions (provisional).**

- Other sources (logs, deployment records, the error tracker) remain reachable. *Owner: Platform owner. Validated when: each source's availability is checked independently.*

**Answer.**

1. **Record the gap:** note the provider, the time, and the checks that couldn't run. Missing data must never be read as "no errors" or "healthy".
2. **Retry within limits:** a short, bounded retry, so the investigation doesn't stall (A2-d).
3. **Use other sources:** logs, traces, deployment records, the error tracker and platform status.
4. **Lower confidence:** say which conclusions are weaker and why, and keep open any hypotheses that needed the missing data.
5. **Hold risky actions:** don't recommend actions that depended on the missing evidence.
6. **Tell a person:** report the outage, since the team may be blind to other alerts too. Escalate if the remaining evidence can't support a diagnosis (A1-c).
7. **Resume when it returns:** collect the missed period and recheck the conclusions.

Also consider whether the provider's outage is linked to the incident, such as a shared network or cloud failure.

### Q3.37 An investigation has exhausted its evidence sources but still cannot establish a root cause. Under which conditions should it stop and escalate?

**Frame.** When should the platform stop investigating and hand over to a person?

**Knowns.**

- The evidence sources are exhausted, and no root cause is established.
- A1-c: hand over rather than guess.
- A2-d: first summary within 5 minutes; full investigation within 15 minutes.

**Shared knowns referenced in this answer:**

- **A1-a:** Any change to production needs approval from a named, accountable person. *(Artifact 1, 1.5)*
- **A1-c:** When evidence is too thin, the cause is not guessed; the incident is handed to a person. *(Artifact 1, UC4)*
- **A2-d:** First summary within 5 minutes; full investigation within 15 minutes; a model-call budget per incident. *(Artifact 2, Q2.16, Q2.18)*

**Unknowns.** Whether the time limits should vary by severity; the confidence level required; who receives the escalation for each service.

**Assumptions (provisional).**

- An on-call owner can be reached, and severity levels are defined. *Owner: Engineering manager. Validated when: the on-call rota and severity definitions are documented.*

**Answer.** Stop and escalate when any of these is true:

- All available sources have been checked, and no new evidence is appearing.
- No hypothesis meets the agreed confidence level.
- Several hypotheses remain, and the available evidence can't separate them.
- The time limit has been reached (15 minutes, A2-d).
- User impact is growing while the investigation continues.
- The next step needs human approval (A1-a), or access the platform doesn't have.
- A required evidence source is unavailable (Q3.36).
- There are signs of a security or data risk. Escalate immediately, without waiting for the evidence to run out.

For high-severity incidents, escalate at the start and keep investigating alongside the engineer.

**The handover should include:**

- impact and a timeline
- evidence checked
- hypotheses tested and ruled out
- missing evidence
- the confidence level
- suggested next steps

It must state "root cause not established". The platform must never present its best guess as a conclusion.
