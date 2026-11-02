# Artifact 3: Incident and Evidence Catalog

- **Layer:** 3, SRE Fundamentals
- **Built from:** Artifact 1, Artifact 2
- **Status:** Proposed: gaps resolved and reference code aligned 2026-11-02 per [decisions.md](../../decisions.md); pending team sign-off
- **Sources:** [conversation.md](conversation.md), [decisions.md](../../decisions.md), [synthetic reference incident](files/reference-incident/README.md) (proposed), [system-design.md](../../reference/system-design.md), [technical-specification.md](../../reference/technical-specification.md), and, for consistency with later citations of this artifact only, [Layer 4](../layer-04-requirements/conversation.md), [Layer 5](../layer-05-threat-modeling/conversation.md), [Layer 9](../layer-09-agent-architecture/conversation.md), [validation.py](../layer-09-agent-architecture/files/validation.py), [context.py](../layer-10-inference-retrieval/files/context.py)

## Required sections

| Required section | Questions this section answers | Done |
| --- | --- | --- |
| 3.1 Initial alert | Q3.9–Q3.11, Q3.22, Q3.28, Q3.33 | Partial |
| 3.2 Service context | Q3.1–Q3.5, Q3.33 | Yes |
| 3.3 Logs | Q3.17, Q3.20, Q3.31 | Yes |
| 3.4 Metrics | Q3.18, Q3.20–Q3.25, Q3.28, Q3.29 | Yes |
| 3.5 Traces | Q3.19, Q3.30 | Yes |
| 3.6 Deployment changes | Q3.6–Q3.8, Q3.26 | Yes |
| 3.7 Expected root cause | Q3.3, Q3.7, Q3.8, Q3.12–Q3.14 | Yes |
| 3.8 Supporting evidence | Q3.27, Q3.32–Q3.34 | Yes |
| 3.9 Contradicting evidence | Q3.27, Q3.35 | Yes |
| 3.10 Expected escalation behavior | Q3.12, Q3.15, Q3.16, Q3.36, Q3.37 | Partial |

**Reference scenario (S):** A Python web application is in production. An engineer deploys a new version that changes a configuration value. The application starts returning HTTP 500 errors. The monitoring system detects the incident.

**Synthetic data:** the sources hold no incident data for S, so the parts of 3.1 to 3.7 labelled *Synthetic* come from an invented [reference incident](files/reference-incident/README.md) for the service `orders-api`. Nothing labelled that way was measured. The generic content around them is the team's.

## 3.1 Initial alert

*Answers Q3.9–Q3.11, Q3.22, Q3.28, Q3.33.*

In S, the monitoring system detects the incident. Alerts come from existing monitoring (A2-b), which evaluates rules and sends alerts when thresholds or objectives are breached ([Q3.28](conversation.md)).

**What the alert must tell us** ([Q3.33](conversation.md)): what fired, when, its severity and threshold. When the problem began may be earlier than the alert ([Q3.8](conversation.md), [Q3.33](conversation.md)).

**Conditions for declaring an incident** (any one; [Q3.9](conversation.md)):

| Condition | Meaning |
| --- | --- |
| Users are affected | A critical journey fails, or the error rate or latency breaches the service objective |
| The objective is at risk | The error budget is burning fast enough to be exhausted soon |
| The impact is significant | Many users, important customers, revenue or data are affected |
| It needs coordination | More than one person or team is needed |
| It's ongoing or uncertain | The cause is unknown, or the problem is spreading |

Once declared, the incident gets one record, an owner, a severity and a timeline.

**Characterising the symptom** ([Q3.10](conversation.md), [Q3.11](conversation.md)):

| | Complete outage | Partial failure | Latency degradation |
| --- | --- | --- | --- |
| Health checks | Fail | Often pass | Not stated |
| Error rate | About 100% | Partial (40% in Q3.10) | Errors may be hidden behind the slowness |
| Users | Everyone affected | Some users, paths or regions | All requests, or some paths or users |
| Typical causes | Crash, network, total dependency loss | One bad instance, one failing path, a bad version on some instances, a partly failing dependency | A slow dependency, resource saturation, exhausted connection pools, or a change that added expensive work |

Break the error rate down by instance, version, path, and region or customer group. A pattern points to the cause; random failure across everything points to a shared dependency.

**Establishing severity** ([Q3.22](conversation.md)): error rate and its trend; users affected; critical journeys affected; duration so far; breakdown; business signals; spread. Severity combines **breadth** (how many), **depth** (how critical) and **duration** (how long).

**Severity levels (provisional, layer decision).** These are the `sev1`..`sev4` values of the schema. An alert proposes a level, and the incident lead confirms it (ADR-50).

| Level | Depth and breadth | Duration | Example |
| --- | --- | --- | --- |
| `sev1` | A critical journey fails for at least 80% of its requests, or there is data loss or a security breach | Any | Complete outage |
| `sev2` | A critical journey fails for 5% to 80% of its requests | 3 minutes or more | One bad version on some instances |
| `sev3` | A critical journey fails for 1% to 5% of its requests, or a non-critical path fails for 5% or more | 15 minutes or more | One slow report |
| `sev4` | Limited effect with a workaround, or no user effect yet | Any | A rising internal error |

The higher level applies if spreading is likely. `sev1` and `sev2` are high severity for early escalation (3.10). A confirmed `sev1` is high-impact for approval, and so is any incident whose severity isn't confirmed yet (ADR-49).

**Service-level objectives (provisional, layer decision).** Both are over a rolling 30 days, for each critical journey:

| Objective | Target | Error budget |
| --- | --- | --- |
| Availability | 99.9% of requests aren't HTTP 5xx | 0.1% of requests |
| Latency | 95% of requests finish within 500 ms | 5% of requests |

An alert condition of a 5% error rate is a burn rate of 50 times, which would use a 30-day budget in about 14 hours.

**Synthetic reference alert** ([alert.json](files/reference-incident/alert.json)):

| Field | Value |
| --- | --- |
| Alert | `OrdersApiHighErrorRate`, service `orders-api`, `production` |
| Rule | HTTP 500 share of all responses over 1 minute above 5%, for 3 minutes |
| Condition began | 14:07:00 UTC, when the rollout reached 25% |
| Fired | 14:10:00 UTC, value 14.9%, threshold 5% |
| Problem actually began | 14:02:04 UTC, about 8 minutes before the alert (Q3.33). At a 5% rollout, the rate of about 3% stayed under the threshold |
| Proposed severity | `sev2`: at most half of order requests failed, for more than 3 minutes |
| Pattern | Partial failure. Health checks pass, and only one version fails |

**Needs measurement:** validate the synthetic fixture against real alert samples, Owner: SRE lead, settled when: real samples are inspected and the fixture's payload, fields and thresholds match them or are corrected.

## 3.2 Service context

*Answers Q3.1–Q3.5, Q3.33.*

**Service context to collect** ([Q3.33](conversation.md)): owner, dependencies, normal behaviour, and any runbook. First-version scope: one organisation, Python applications only, one deployment environment (A2-a).

**Request path and where it fails** ([Q3.1](conversation.md), [Q3.3](conversation.md)):

| Stage | What happens | Failure |
| --- | --- | --- |
| Name lookup | The browser resolves the domain to an address | The site can't be found |
| Connection and encryption | A secure connection is set up | Certificate or connection errors |
| Edge | The content delivery layer serves cached files, or forwards the request | Stale or missing files |
| Load balancer or proxy | Routes the request to a healthy application instance | No healthy instances, so a 502/503 error; 502/503/504 if no instance answers |
| Web server | Hands the request to the application | — |
| Middleware | Authentication, request parsing, logging | May return 400, 401 or 403 |
| Routing | Finds the handler for the path | May return 404 |
| Handler | Runs the business logic and **reads configuration**, the step where S's changed value takes effect | An exception, so an HTTP 500 error, as in S |
| Data store and dependencies | The database and other services answer | Timeouts or failures here often become 500 or 504 |
| Error handling | An unhandled exception becomes a 500, and the error and stack trace are logged | — |
| Response and rendering | HTML, then scripts and styles; scripts build the page | The HTML arrives, but a script fails, so the page stays blank; a browser-side error |

**Status codes tell you where to look** ([Q3.3](conversation.md)): 4xx, the request was rejected, likely a client problem; 500, the application failed; 502/503/504, the proxy couldn't get a good answer from the application. In S, a 500 points to the handler or to its calls to the database and other services.

**Components under load** ([Q3.2](conversation.md)):

| Component | Responsibility under load |
| --- | --- |
| Load balancer | Spread requests across healthy instances; take failing instances out |
| Web server | Accept connections; limit concurrent work per instance; queue or reject the excess |
| Application | Handle each request independently; use timeouts on every outbound call; fail fast |
| Connection pools | Reuse a bounded number of database and service connections |
| Cache | Serve repeated reads without hitting the database |
| Database | Handle concurrent reads and writes safely; protect data consistency |
| Rate limiting | Protect the system from overload by shedding or delaying excess requests |
| Monitoring | Record request counts, errors and durations, so overload is visible |

The rule is **bounded work everywhere**: limited concurrency, limited queue size and timeouts.

**Entry point** ([Q3.4](conversation.md)): a single public entry point, a load balancer or API gateway, exposes the interface. It sees every request, so its counts and status codes are a primary signal, and it can produce its own errors (502/503/504), which differ from the application's 500s.

**Database** ([Q3.5](conversation.md)): customer data is owned by a database, separate from the application process. Connection failures, slow queries, exhausted connection pools and failed migrations all surface in the application as errors or slowness.

**Synthetic reference service** ([service-catalog.json](files/reference-incident/service-catalog.json)). The service catalogue is the source of this context ([System Design §5](../../reference/system-design.md)):

| Item | Value |
| --- | --- |
| Service | `orders-api`: a Python web service behind one load balancer, with six instances |
| Owner, on-call | `orders-team`, `synthetic-oncall-c` |
| Dependencies | `orders-db` (PostgreSQL), `payment-gateway` (third party) |
| Critical journey | Place an order: `POST /v1/orders` |
| Normal behaviour | About 1,200 requests a minute, 60% of them `POST /v1/orders`; HTTP 500 rate about 0.08%; one known signature, a payment gateway timeout |
| Health check | `GET /healthz`, which doesn't read the changed setting |
| Marked `critical` for approval | No, so a confirmed `sev2` configuration revert needs one approver (ADR-49) |
| Hold period | 600 seconds, the ADR-66 default |
| Runbook | `runbook-orders-api-5xx` (named only; no content) |

## 3.3 Logs

*Answers Q3.17, Q3.20, Q3.31.*

**Fields each logged event should record** ([Q3.17](conversation.md)):

| Field | Why or what |
| --- | --- |
| Timestamp | Precise, with a time zone |
| Severity | Debug, info, warning or error |
| Service, instance and version | So errors can be tied to a release |
| Request ID and trace ID | To link events in one request, across services |
| What happened | A message and an event type |
| Request context | Path, method, status code and duration |
| For errors | The error type, the message, and the stack trace |
| Relevant configuration | Which settings were in effect. In S, this links the error to the changed value |
| User or tenant reference | Hashed or pseudonymous, never personal data in clear |

Use a structured format (key–value). Never log passwords, tokens or personal data.

**Expected in S:** an unhandled exception becomes a 500, and the error and stack trace are logged ([Q3.3](conversation.md)). Check whether errors or stack traces mention the variable, or the behaviour it controls ([Q3.7](conversation.md)).

**Why logs alone are insufficient** ([Q3.20](conversation.md)):

- **No denominator:** logs show failures, but not how many requests succeeded.
- **Incomplete:** often sampled, rate-limited or dropped under load, exactly when failures spike.
- **Inconsistent:** not every path logs its errors, and some errors are logged more than once.
- **Expensive to count:** counting means searching large volumes of text.

Logs explain *why* one event failed; metrics show *how often*.

**Existing error tracker** ([Q3.31](conversation.md)):

- Read-only access through the tracker's official API, with its own named, least-privilege identity (A2-b).
- On demand: fetch the error groups for the affected service and time window. Don't copy everything.
- Key fields: error type and message, stack trace, first and last seen, count, affected version, and the release it first appeared in. The last two are decisive for S.
- Event notifications, optionally, only to learn about new error types quickly.
- Stay within rate limits, remove personal data before storing anything, and save a link to the original error with each piece of evidence.
- If the tracker is unavailable, record the gap and continue ([Q3.36](conversation.md)).

**Synthetic reference logs** ([logs.ndjson](files/reference-incident/logs.ndjson)) carry the fields above and start in the normal period, when the only error is the known payment timeout. The first new error, condensed, at 14:02:04 UTC on version 2.14.1:

```text
level=ERROR instance=orders-api-e version=2.14.1 config_revision=cfg-42 POST /v1/orders status=500
  File "/app/orders/service.py", line 142, in place_order
    totals = pricing.compute_totals(cart, settings)
  File "/app/orders/pricing.py", line 37, in compute_totals
    tax_rate_bps = int(settings.order_tax_rate_bps)
ValueError: invalid literal for int() with base 10: '8.25'
```

Successful `GET` requests on the same instance show that it is up, and that only the order path fails. One `orders-db` reconnect warning at 14:04:11 on an old-version instance is a planted distractor (3.7).

Collected log evidence is kept for 30 days, or 1 year if a diagnosis cites it (ADR-57). The rules for personal data are those above: never logged, and removed before storing.

## 3.4 Metrics

*Answers Q3.18, Q3.20–Q3.25, Q3.28, Q3.29.*

| Question | Measurements | Source |
| --- | --- | --- |
| How many requests failed? | Request counters by status code, path, service and version: total requests, and failed requests (usually 5xx; count 4xx separately). Failures in the last hour = the increase in the failed-request counter over that hour. Error rate = failed requests ÷ total requests | [Q3.18](conversation.md), [Q3.20](conversation.md) |
| How slow is it? | Request duration as a histogram by path, status code, service and version; 50th, 95th and 99th percentiles (averages hide slow outliers). Also the duration of calls to dependencies, and browser-side load time | [Q3.21](conversation.md) |
| How severe is it? | Error rate and trend, users affected, critical journeys, duration, breakdown, business signals, spread | [Q3.22](conversation.md) |
| Is CPU saturated? | CPU utilisation against the limit, CPU throttling, run queue or load, request rate and duration, process-level CPU, garbage-collection time. Confirm CPU rose **before** errors and slowness began | [Q3.23](conversation.md) |
| Is memory the crash cause? | Memory over time against the limit (a steady climb that never falls is a leak; one that tracks traffic is load), out-of-memory kill and restart counts, memory after each garbage collection, request rate, when the growth began, a memory profile | [Q3.24](conversation.md) |
| Is a job backlog growing? | Queue depth, oldest job's age, arrival rate vs completion rate, worker health, failed and retried jobs. Alert on age, and on sustained growth | [Q3.25](conversation.md) |

Agree the definition of "failed" once, and use it everywhere ([Q3.18](conversation.md)).

**Metrics collection system** ([Q3.28](conversation.md)): collect; label with service, instance, version, path and status; store with retention that suits the use; query rates, percentiles and comparisons; alert; expose to dashboards and, read-only, the AI SRE platform (A2-b); stay reliable during incidents.

**Dashboards** ([Q3.29](conversation.md)):

- Service overview: request rate, error rate and latency per service.
- Resources: CPU, memory, saturation and restarts per instance.
- Breakdowns by version, path, region and instance, to spot partial failures.
- Time controls: zoom to the incident window, and compare with an earlier normal period.
- **Change markers:** deployments and configuration changes drawn on the graphs. These are crucial for S.
- Objectives: the targets and error budget on the graphs.
- Links from a graph to the related logs and traces.
- Views for different audiences: a summary for management, detail for on-call engineers.

**Definition of "failed" (provisional, layer decision):** an HTTP 5xx response. 4xx responses are counted separately and don't use the error budget.

**Synthetic reference metrics** ([metrics.csv](files/reference-incident/metrics.csv)): requests and HTTP 500s per minute and per version, from `http_requests_total`, labelled by service, version, path and status. Approximate values:

| Period (UTC) | Rollout | 2.14.0 HTTP 500 rate | 2.14.1 HTTP 500 rate | Overall rate | Requests a minute |
| --- | --- | --- | --- | --- | --- |
| 13:02–14:01, normal | 0% | 0.08% | — | 0.08% | 1,200 |
| 14:02–14:06, canary | 5% | Baseline | 60% | 3% | 1,200 |
| 14:07–14:11 | 25% | Baseline | 60% | 15% | 1,200 |
| 14:12–14:31 | 50%, paused at 14:15 | Baseline | 60% | 30% | 1,200 |
| 14:33–14:44, after the revert | 50% | Baseline | Baseline | 0.08% | 1,200 |

The new version's 60% equals the share of requests that are `POST /v1/orders`: every order fails there, and every other request succeeds. From 14:02 to 14:32, 8,490 of 37,170 requests failed, about 16% of a 30-day error budget at this traffic. The ADR-66 recovery baseline is the median rate from 13:02 to 13:57: 0.082%.

The 40%, 30% and 100 ms to 8 seconds figures belong to their questions, not to S.

## 3.5 Traces

*Answers Q3.19, Q3.30.*

**Reconstructing a journey with distributed tracing** ([Q3.19](conversation.md)):

1. **Assign an ID at the edge:** the first service gives the request a unique trace ID.
2. **Pass it along:** every service passes the ID to the next, in request headers or in queue messages.
3. **Record timed steps:** each service records the steps it ran ("spans") with the trace ID, start time, duration and outcome.
4. **Assemble the journey:** a tracing system groups the spans by trace ID, and shows which service took how long and where the error started.

Logs should carry the same trace ID, so a trace links to the detailed log events.

**Watch for:** sampling, since the failing request may not have been recorded (keep all error traces), and queues, which break the chain unless the ID is carried in the message.

**Standards** ([Q3.30](conversation.md)): OpenTelemetry for producing traces, metrics and logs; W3C Trace Context for the request headers that carry the trace ID; consistent naming through OpenTelemetry's semantic conventions; trace context placed in messages as well as HTTP headers. Open standards keep the data portable.

**Expected in S:** failing traces point to the changed code ([Q3.34](conversation.md), code match).

**Synthetic reference trace** ([trace.json](files/reference-incident/trace.json)): one failing `POST /v1/orders` at 14:08:12 UTC, found in the logs by the same trace ID. The request crosses the load balancer and `orders-api`.

| Span | Service | Status | Detail |
| --- | --- | --- | --- |
| `POST /v1/orders` | `edge-load-balancer` | Error | HTTP 500 from `orders-api-e` |
| `POST /v1/orders` | `orders-api` | Error | Version 2.14.1, `cfg-42` |
| `orders.service.place_order` | `orders-api` | Error | — |
| `SELECT cart` | `orders-api` | OK | The database answered |
| `orders.pricing.compute_totals` | `orders-api` | Error | Reads `ORDER_TAX_RATE_BPS`; `ValueError` at `orders/pricing.py:37` |

The payment gateway is never called. The fixture keeps all error traces. Traces are optional evidence: without them confidence falls, but nothing is blocked (ADR-54).

## 3.6 Deployment changes

*Answers Q3.6–Q3.8, Q3.26.*

**Release sequence** ([Q3.6](conversation.md)):

1. Change merged: code and configuration changes are reviewed and merged.
2. Build: the application is packaged as a versioned artifact.
3. Tests: automated tests run; failures stop the release.
4. Approval, if required.
5. Configuration applied: new values are prepared for the target environment. **This is S's change.**
6. Database migrations, if any, run before or with the new code.
7. Rollout: new instances start with the new version and configuration. Traffic shifts to them, gradually or all at once.
8. Health checks: new instances must pass before they take traffic.
9. Old instances removed once the new ones are healthy.
10. Release recorded: version, time, author and configuration changes.

Each step is a point where a failure can be introduced, and step 10 is the record that later links an incident to its change.

**Deployment information to record** ([Q3.26](conversation.md)):

| Item | Content |
| --- | --- |
| When | Start time, the time each stage was reached, and the finish time |
| What | Version, commits, and **configuration differences**: old and new values per setting |
| Who | Author and approver |
| Where | Service, environment, and which instances got the new version, and when |
| How | Rollout method and percentage over time |
| Other changes | Database migrations, feature-flag changes and infrastructure changes, recorded the same way |
| Outcome | Succeeded, rolled back or failed, with links to the change itself |

All of these need the same clock as the monitoring data.

**Separating configuration from code** ([Q3.7](conversation.md)): compare versions; find where the value is used; match the error; test in isolation with the old value, then the new value (A2-c); check timing, since behaviour changes when the configuration took effect, which may be at restart, not at deploy.

**Isolating the responsible change** ([Q3.8](conversation.md)): pin the start time from metrics; list every change in that window; check the version split; match errors to changes; narrow it down, testing changes one at a time in a disposable environment, configuration first, or searching through the commits in halves; confirm by reproducing the failure with the suspect change and clearing it without. In S, listing the changes finds one configuration change, and narrowing it down confirms it.

**Synthetic reference release** ([release.json](files/reference-incident/release.json), [config-diff.txt](files/reference-incident/config-diff.txt)). Gradual rollout stays an assumption ([Q3.6](conversation.md)), and the fixture is built on it so that version-split evidence exists.

| Item | Value |
| --- | --- |
| Version | 2.14.1, replacing 2.14.0. One commit, `7d3e91a`, no code or image change |
| Author, approver | `synthetic-engineer-a`, `synthetic-reviewer-b` |
| Changed setting | `ORDER_TAX_RATE_BPS`: old `825`, new `8.25` |
| Configuration revision | `cfg-41` to `cfg-42` |
| Other changes | No migrations, feature flags or infrastructure changes |
| Rollout | Gradual by traffic weight, planned 5%, 25%, 50%, 100% |
| Times (UTC) | 5% at 14:02, 25% at 14:07, 50% at 14:12, paused at 14:15 by the on-call engineer |
| Instances | New: `orders-api-e`, then also `-f`. Old: `orders-api-a` to `-d` |
| Outcome | Paused. Health checks passed throughout, so the release process didn't stop itself |

## 3.7 Expected root cause

*Answers Q3.3, Q3.7, Q3.8, Q3.12–Q3.14.*

**Expected cause in S:** the configuration value changed by the deployment. The handler reads configuration, which is the step where S's changed value takes effect, an unhandled exception becomes a 500, and in S a 500 points to the handler or its calls to the database and other services ([Q3.3](conversation.md)).

**This stays a hypothesis until proven.** The deployment is the likely cause, but this is not yet proven ([Q3.8](conversation.md)). Same code with a different value producing different behaviour is the controlled comparison that proves the configuration caused it ([Q3.7](conversation.md)). The decisive evidence for the reference case is the changed value being read on the failing path (tier 3), and only instances with the new value returning HTTP 500 errors (tier 2). Either is mechanism evidence on its own (ADR-53).

**Reaching the cause** ([Q3.12](conversation.md)):

1. Limit the damage first: if impact is growing, consider a safe, approved mitigation (A1-a) before the cause is proven.
2. Build a timeline.
3. Describe the symptom precisely: what fails, for whom, and since when.
4. List hypotheses: at least two, including "not the deployment".
5. For each, write down what you would expect to see if it were true, and what would rule it out.
6. Check the evidence against each: confirming evidence (3.8) and contradicting evidence (3.9).
7. Test the leading hypothesis in a safe environment (A2-c).
8. Conclude only when the evidence separates the hypotheses. If it can't, escalate (3.10).

The discipline is to test hypotheses, not to collect more data hoping the answer appears.

**Restoring service** ([Q3.13](conversation.md)):

| Action | When it fits | Risk |
| --- | --- | --- |
| Revert the configuration value | The cause is a configuration change (S) | Low; fast |
| Roll back the release | A code or bundled change is the cause | Low, unless the database changed |
| Turn off a feature flag | The failing feature is behind a flag | Low |
| Roll forward with a fix | Rollback isn't safe, and the fix is small | Medium; needs a review |
| Restart or replace instances | Stuck processes, or leaked resources | Low, but may only buy time |
| Scale out | The cause is overload | Low; costs money |
| Fail over or route around | A dependency or region has failed | Medium |
| Shed load or rate-limit | Protect core features during overload | Some users degraded |

Prefer the **smallest, most reversible action** that targets the confirmed cause. Every action is approved (A1-a) and verified against the original symptom (A1-b). In S, that means reverting the configuration value.

**Returning to a working version** ([Q3.14](conversation.md)): choose the smallest reversal; if only configuration changed (S), revert that value without redeploying the code. Otherwise redeploy the previous version from its stored build. Check database changes first: this is the main way a rollback can make things worse. Roll out the reversal with health checks, verify recovery against the original symptom, record the rollback, and keep the failing version for investigation. Configuration and code should be revertable separately.

**Synthetic reference result** ([answer-key.json](files/reference-incident/answer-key.json)):

| Item | Expected |
| --- | --- |
| Root cause | Release 2.14.1 changed `ORDER_TAX_RATE_BPS` from `825` to `8.25`. `compute_totals` parses it with `int()` on every `POST /v1/orders`, which raises `ValueError`, and the unhandled exception becomes HTTP 500 |
| Why it spread | `GET /healthz` doesn't read the setting, so new instances passed their health gate |
| Result | Established, High confidence, from tier 2 and tier 3 evidence (ADR-53). Reproduction adds tier 1 during testing |
| Alternatives ruled out | `orders-db`: one reconnect, on the old version, after the first failure, and the changed value isn't a database setting. Payment gateway: a known signature at its normal rate |
| Fix | `config_revert` to `825` (`cfg-41`), without redeploying code. Reverting the commit is acceptable. Rolling back the release is acceptable, but it is high-impact (ADR-49) |
| Who changes production | A person by hand in Release A; the dispatcher on a signed approval in Release B (ADR-44) |

Configuration is versioned with the release and can be reverted on its own in the fixture. For a real service this stays an assumption, listed below.

## 3.8 Supporting evidence

*Answers Q3.27, Q3.32–Q3.34.*

**Evidence to collect before proposing a diagnosis, in order** ([Q3.33](conversation.md)), recording anything unavailable as missing:

1. **The alert:** what fired, when, its severity and threshold.
2. **Impact:** users, regions, paths and journeys affected, and whether it's spreading.
3. **Service context:** owner, dependencies, normal behaviour, and any runbook.
4. **Timeline:** when the problem began, which may be earlier than the alert.
5. **Changes:** deployments, configuration, feature flags, migrations and infrastructure changes.
6. **Telemetry:** metrics, logs and traces from before and during the incident.
7. **Dependencies:** database, third-party and platform health.
8. **History:** similar past incidents and how they were resolved.

This is the reading order. Adapters run in parallel, and change history is requested first ([Technical specification 5.1](../../reference/technical-specification.md)).

Each item keeps its source, its time, and a link to the original record. Every adapter returns the same kinds of record (alert, metric series, log events, trace, change event, error group), and reports "unavailable" or "partial data", never empty results that look like "healthy" ([Q3.32](conversation.md)).

**Required evidence for change-caused failures** (ADR-54):

| Source | Status | If missing |
| --- | --- | --- |
| The alert | Required | Record a gap and continue. The diagnosis can't be established, so it ends inconclusive |
| Change history | Required | The same |
| Logs or the error tracker, at least one | Required | The same |
| Metrics, including error rate by version | Expected, layer decision | Record a gap and lower confidence |
| Traces, dependency health, past incidents | Optional | Record a gap and lower confidence |

Other incident categories are recognised and handed over, as they're out of scope (ADR-58). A sign of a security or data risk stops collection at once.

**Enough evidence** means every required source collected, plus the Medium bar below (ADR-54).

**Observations that strengthen a deployment hypothesis, strongest first** ([Q3.34](conversation.md)). Strength belongs to the observation, not the evidence kind, and code assigns the tier when it computes the facts (ADR-53):

| Tier | Evidence | Observation | Limit |
| --- | --- | --- | --- |
| 1 | Reproduction | In a disposable environment, the failure appears with the new version or configuration, and disappears with the old one | Re-triggering is done only in that environment, never in production |
| 2 | Version split | Errors come from instances on the new version, while old-version instances stay healthy | Only available if the rollout was gradual ([Q3.6](conversation.md)) |
| 3 | Config match | The changed configuration value is read on the failing code path, and the error refers to it, or to what it controls | Decisive for S |
| 4 | Code match | Stack traces and failing traces point to the changed code | — |
| 5 | New error signature | This error didn't exist before the release | — |
| 6 | Scale match | Impact grows with the rollout percentage | — |
| 7 | Rollback clears it | Errors stop after an approved rollback | Recovery could still be coincidence, so confirm against the evidence above |
| 8 | Timing | The failure starts once the rollout reaches traffic | On its own this is the weakest evidence |

Tiers 1 to 3 are mechanism evidence, 4 to 7 supporting, and 8 weak.

**Confidence** (ADR-53, provisional threshold):

| Level | Requires | Result |
| --- | --- | --- |
| High | Tier 1–3, no unresolved contradicting evidence | Established |
| Medium | Tier 4, or two independent tier 5–7 observations, no unresolved contradiction | Established |
| Low | Anything else, or any `confidence_cap` from the critic | Inconclusive, no patch |

The bar is Medium.

**Why timing alone is not proof** ([Q3.27](conversation.md)): timing shows correlation, not cause. Other changes coincide; the deployment itself is a disruption, since restarting instances can expose an unrelated, existing fault; a shared trigger may explain both; deployments are frequent, so overlaps happen by chance; and clock differences can create false alignment. Proof needs evidence that links the change to the failure mechanism, and the absence of contradicting evidence.

## 3.9 Contradicting evidence

*Answers Q3.27, Q3.35.*

**Case:** the deployment came immediately before the incident, but the logs show a database connection failure described as unrelated. Timing doesn't settle it ([Q3.27](conversation.md)).

**Observations that contradict the deployment hypothesis** ([Q3.35](conversation.md)):

- Database errors began **before** the deployment reached traffic.
- Old-version instances fail the same way.
- Other services that **weren't deployed** but share the database also fail.
- The database shows its own cause: a failover, maintenance, a storage limit, or a provider outage.
- Rolling back makes no difference.
- The failing paths don't touch the changed code or configuration.
- The release contains no database, connection, pool or query changes, **including configuration**. In S, check whether the changed value is a database setting.

**Test the label "unrelated" too.** The release could have caused the database fault, for example through a new query, a connection leak, a changed connection setting, or a migration. If connections rose only on the new version, or the changed configuration value controls the database connection, the deployment may still be the cause, through the database failure. Report the two explanations side by side until the evidence separates them.

## 3.10 Expected escalation behavior

*Answers Q3.12, Q3.15, Q3.16, Q3.36, Q3.37.*

**Stop and escalate when any of these is true** ([Q3.37](conversation.md)):

- All available sources have been checked, and no new evidence is appearing.
- No hypothesis meets the agreed confidence level: Medium (ADR-53).
- Several hypotheses remain, and the available evidence can't separate them.
- The time limit has been reached (15 minutes, A2-d).
- User impact is growing while the investigation continues.
- The next step needs human approval (A1-a), or access the platform doesn't have.
- A required evidence source is unavailable once collection is complete ([Q3.36](conversation.md)). Collection continues past the gap, then the investigation ends inconclusive and hands over (ADR-54).
- There are signs of a security or data risk. Escalate immediately, without waiting for the evidence to run out.

For high-severity incidents (`sev1`, `sev2`), escalate at the start and keep investigating alongside the engineer.

**The handover includes:** impact and a timeline; evidence checked; hypotheses tested and ruled out; missing evidence; the confidence level; suggested next steps. It must state "root cause not established". The platform must never present its best guess as a conclusion.

**When an evidence source disappears** ([Q3.36](conversation.md)):

1. Record the gap: the provider, the time, and the checks that couldn't run. Missing data must never be read as "no errors" or "healthy".
2. Retry within limits: a short, bounded retry (A2-d).
3. Use other sources: logs, traces, deployment records, the error tracker and platform status.
4. Lower confidence: say which conclusions are weaker and why, and keep open any hypotheses that needed the missing data.
5. Hold risky actions: don't recommend actions that depended on the missing evidence.
6. Tell a person: the team may be blind to other alerts too. Escalate if the remaining evidence can't support a diagnosis (A1-c).
7. When it returns during the investigation, collect the missed period and recheck the conclusions. After hand-over nothing resumes automatically: a person may request re-investigation (ADR-46).

Also consider whether the provider's outage is linked to the incident.

**Moving responsibility between people** ([Q3.16](conversation.md)):

1. Escalate on clear triggers: a time limit passes without progress; the severity increases; the cause is in another team's area; the needed access or expertise is missing.
2. Choose the next owner from the escalation path: the owning team's on-call, then a senior engineer, then the incident lead.
3. Hand over explicitly: current impact, the timeline, what was checked, hypotheses tested and ruled out, actions taken, open questions.
4. The new owner confirms they have taken it over. Until they do, the first engineer still owns it.
5. Record the transfer in the incident record.

**Incident lifecycle** ([Q3.15](conversation.md)):

| Stage | Ends when | Timing measured |
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

Each incident has one record, with a timestamped entry for every stage change, the owner, decisions and actions. These nine stages describe how a team tracks an incident. They are not workflow states, which are defined by ADR-46.

**Escalation values:**

| Item | Value |
| --- | --- |
| Confidence level | Medium (ADR-53, provisional) |
| Platform limits | First summary p95 ≤ 5 minutes, investigation 15 minutes, for every severity. They don't vary by severity (layer decision) |
| Work limit | Non-terminal, non-waiting states hand over at 24 hours (ADR-46) |
| Approval requests | Escalate at 15 minutes, expire at 60 minutes (ADR-49) |
| Who is told at the start | The service owner, the on-call engineer and the author of the last change |
| Who receives an escalation | The service's catalogue path: on-call, then a senior engineer, then the incident lead |
| Severity definitions | 3.1 |

**Times for people (provisional, layer decision).** If a limit passes, the next person on the path is called:

| Level | Acknowledge within | No progress for |
| --- | --- | --- |
| `sev1` | 5 minutes | 30 minutes |
| `sev2` | 15 minutes | 60 minutes |
| `sev3` | 60 minutes | 4 hours |
| `sev4` | Next working day | Not applicable |

**Expected in the synthetic reference incident** ([answer-key.json](files/reference-incident/answer-key.json)):

| Item | Run 1: full evidence | Run 2: change history withheld ([Q1.17](../layer-01-problem-discovery/conversation.md)) |
| --- | --- | --- |
| Files withheld | None | `release.json`, `config-diff.txt` |
| Result | Established, High | "Root cause not established" (ADR-54) |
| Workflow | `investigating` → `diagnosed` → `testing`, then `handed_over` with the tested recommendation in Release A, or `awaiting_approval` onward in Release B | `investigating` → `inconclusive` → `handed_over`, no patch |
| Severity | `sev2` proposed; the incident lead confirms. Until then it counts as high-impact (ADR-49) | The same |
| Approval | One approver with approve rights, who may not be `synthetic-engineer-a` acting alone (ADR-49) | None |
| Recovery | The incident lead accepts it after a 10-minute hold (ADR-66) | Stays with people |

**Needs the team:** the real on-call rotas and night coverage behind each escalation path, Owner: Engineering manager, settled when: rotas are confirmed.

## Assumptions

| Assumption | Status | Owner | Validated when |
| --- | --- | --- | --- |
| A load balancer or content delivery layer sits in front of the application (Q3.1) | Provisional | Platform owner | The reference application's request path is documented. |
| Application instances keep no user state in memory, so more can be added (Q3.2) | Provisional | Platform owner | The reference application is reviewed for in-memory state. |
| Requests pass through a proxy, middleware, and then a handler (Q3.3) | Provisional | Platform owner | The reference application's request path is documented. |
| Clients reach one public address, not individual instances (Q3.4) | Provisional | Platform owner | The network layout of the reference application is documented. |
| The data is structured and needs transactions (Q3.5) | Provisional | Platform owner | The reference application's data model is reviewed. |
| An automated build-and-release pipeline exists (Q3.6) | Provisional | Platform owner | The release process is documented. |
| Rollout is gradual, not all at once (Q3.6) | Provisional | Platform owner | The release process is documented (the same review). If false, version-split evidence (Q3.34) is unavailable. |
| Configuration changes are recorded separately from code changes (Q3.7) | Provisional | Platform owner | The release record shows configuration differences per release. |
| The deployment is the likely cause, but this is not yet proven (Q3.8) | Provisional | SRE lead | The evidence in Q3.34 is collected. |
| The team has an on-call process (Q3.9) | Provisional | Engineering manager | The on-call rota is reviewed. |
| The 40% figure is measured over all requests, not a sample (Q3.10) | Provisional | SRE lead | The error-rate metric's definition is checked. |
| 100 ms and 8 seconds describe typical requests (the median), not rare outliers (Q3.11) | Provisional | SRE lead | The metric's percentile is confirmed. |
| The evidence is accurate, and timestamps across sources use the same clock (Q3.12) | Provisional | Platform owner | Clocks across sources are checked against each other. |
| The identified cause is correct (Q3.13) | Provisional | SRE lead | Recovery is verified after the fix (A1-b). |
| The previous version was working before the deployment (Q3.14) | Provisional | SRE lead | Metrics before the deployment show normal behaviour. |
| One record per incident is acceptable to the teams (Q3.15) | Provisional | Engineering manager | The teams agree on the incident record. |
| A second engineer or team can be reached (Q3.16) | Provisional | Engineering manager | The escalation path is documented and tested. |
| Logs can be structured and searched centrally (Q3.17) | Provisional | Platform owner | The logging setup is reviewed. |
| Metrics are kept for at least the reporting period (Q3.18) | Provisional | Platform owner | Metric retention is checked. |
| All five services can be instrumented (Q3.19) | Provisional | Platform owner | Each service's team confirms. |
| Logs record individual events, but there is no total request count to compare against (Q3.20) | Provisional | Platform owner | Available metrics are listed. |
| The slowness is at least partly on the server side (Q3.21) | Provisional | SRE lead | Server-side duration is measured. |
| The 30% is measured correctly, and isn't mostly retries or automated traffic (Q3.22) | Provisional | SRE lead | Errors are broken down by client type. |
| The service runs in containers with CPU limits (Q3.23) | Provisional | Platform owner | The reference application's runtime settings are checked. |
| The crashes are caused by running out of memory (Q3.24) | Provisional | SRE lead | Crash records show out-of-memory kills. |
| The queue exposes its depth and the age of its jobs (Q3.25) | Provisional | Platform owner | The queue's metrics are checked. |
| Deployment events can be captured automatically from the release process (Q3.26) | Provisional | Platform owner | The release process emits a record for each deployment. |
| Deployment and error timestamps are accurate, and use the same clock (Q3.27) | Provisional | Platform owner | Clocks across sources are compared. |
| Applications can expose metrics (Q3.28) | Provisional | Platform owner | The reference application exposes request metrics. |
| The underlying metrics already exist (Q3.29) | Provisional | Platform owner | Metrics are listed per service. |
| All teams can adopt one standard (Q3.30) | Provisional | Engineering manager | The teams agree. |
| The error tracker offers an API or event notifications (Q3.31) | Provisional | Platform owner | The tracker's interface is reviewed. |
| Each tool has a stable public interface (Q3.32) | Provisional | Platform owner | Each tool's interface is reviewed. |
| Alerts can be linked to a service, and timestamps across tools use the same clock (Q3.33) | Provisional | Platform owner | Alerts carry a service label, and clocks are compared. |
| Telemetry is labelled by version, and deployment records are accurate (Q3.34) | Provisional | Platform owner | Metrics and logs carry the version. |
| Log timestamps are accurate (Q3.35) | Provisional | Platform owner | Clocks are compared across sources. |
| Other sources (logs, deployment records, the error tracker) remain reachable (Q3.36) | Provisional | Platform owner | Each source's availability is checked independently. |
| An on-call owner can be reached, and severity levels are defined (Q3.37) | Provisional | Engineering manager | The on-call rota and severity definitions are documented. |
| The `sev1`–`sev4` definitions in 3.1 suit the organisation | Provisional | SRE lead | Past incidents are classified with them, and the incident leads agree with the results. |
| The objectives of 99.9% availability and 95% within 500 ms suit critical journeys | Provisional | SRE lead | 30 days of measurements show each is reachable and meaningful. |
| "Failed" means HTTP 5xx, with 4xx counted separately | Provisional | SRE lead | Each service's metrics are checked against the definition. |
| The acknowledgement and no-progress times in 3.10 are workable | Provisional | Engineering manager | The rotas are confirmed and an escalation drill meets them. |
| Metrics are expected evidence, but not required by ADR-54 | Provisional | SRE lead | The frozen suite shows whether diagnoses without metrics stay within the false-established limit. |
| A recovery predicate of at most 0.2% HTTP 500s per minute suits the reference incident | Provisional | SRE lead | The predicate is run against the baseline period without a false alarm in a recovery drill. |
| Configuration can be reverted on its own, without redeploying code (Q3.14) | Provisional | Platform owner | The release process is documented, and a value is reverted through it. |

## Decisions and conflicts

| Item | Resolution | Decided by |
| --- | --- | --- |
| Evidence strength scale | Q3.34's order is authoritative, and strength attaches to the observation. `validation.STRENGTH` is replaced. | ADR-53 |
| Priority vs strength | Different jobs. `EVIDENCE_PRIORITY` only orders context for reading, and is now `CONTEXT_KIND_ORDER`. | ADR-53 |
| System Design list | All eight tiers stand. Hypotheses: at least two (Q3.12), at most five (System Design). | ADR-53 |
| Decisive evidence in S | Tier 2 and tier 3 are each mechanism evidence. If the rollout isn't gradual, tier 3 still stands alone. | ADR-53 |
| Unavailable source | A missing source never stops collection. A diagnosis can't be established while a required source is missing, which reconciles Q3.36 and Q3.37. | ADR-54 |
| Required evidence per category | Three required sources, in 3.8. Q9.49's longer list becomes required plus optional. | ADR-54 |
| Metrics in the required list | ADR-54 names metrics as neither required nor optional. Treated as expected: a gap lowers confidence, but doesn't block. | Layer decision (proposed) |
| First evidence | Q4.3's alert, service and time window are the intake inputs. Q3.33's order is the reading order, and adapters run in parallel. | Layer decision (proposed) |
| Personal data in logs | Q3.17's rule stands, and the platform removes any it finds (Q3.31). What real logs contain stays with the Security lead in the register. | Layer decision (proposed) |
| Sending evidence to the model | Layer 3 sets no rule on this. Redacted extracts follow Technical specification 5.2 and Layer 5. | Layer decision (proposed) |
| Lifecycle vs workflow states | Q3.15's nine stages describe how a team tracks an incident. Workflow states are those of `states.py`. | ADR-46 |
| Example content | Test values such as the checkout runbook stay test data. 3.1 to 3.7 use the labelled synthetic incident instead. | Layer decision (proposed) |
| Shared-known labels | Checked: A1-a to A1-c and A2-a to A2-d are defined in Artifacts 1 and 2. Labels kept. | Layer decision (proposed) |
| Severity levels and objectives | `sev1`–`sev4` and two provisional objectives, in 3.1. "Failed" means HTTP 5xx, in 3.4. | Layer decision (proposed) |
| Escalation values | Values in 3.10. Platform limits don't vary by severity. Times for people do, and they are provisional. | Layer decision (proposed) |
| Reference incident design | Synthetic `orders-api`, `sev2`, gradual rollout, one changed value read on the failing path. | Layer decision (proposed) |
| Resuming after a source returns | Q3.36's resume applies during the investigation. After hand-over, a person requests re-investigation. | ADR-46, Layer decision (proposed) |
| Who approves and accepts in the demo | Q1.17's "the engineer" can't be the releasing author acting alone, and the incident lead accepts recovery. | ADR-49, ADR-66 |

## Acceptance checks

- [ ] Every required section above is present and complete.
- [x] Every section lists the questions it answers, and every question in this layer is answered by at least one section.
- [x] Every assumption is marked provisional and has a named owner and a condition under which it will be validated.
- [ ] Every disagreement between Claude and ChatGPT that affected a decision is recorded with the team's resolution.
- [ ] The artifact is consistent with every artifact it is built from; any conflict is recorded as an explicit decision.
- [x] The artifact was produced with the team's coding agent and is saved in the team repository.

Reviewed by: TBD
Date: TBD
