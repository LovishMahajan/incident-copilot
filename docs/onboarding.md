# Adding a service

How a team puts its service under the platform for the advisory pilot (Release A, ADR-44): what to configure, where, and the check that ends it. Only what the code supports today is here; each gap is named where it bites. Decisions behind it: ADR-75.

In Release A the platform investigates, diagnoses and recommends; **people change production**. Nothing below gives it a way to write to your service, and the go-live check refuses a configuration that would.

## 1. The catalogue entry

Intake answers `404 unknown_service` (and dead-letters the alert) until the service and its environment are in your organisation's catalogue: the `services` and `environments` tables (Artifact 7.8). Each service row holds:

| Column | Meaning |
| --- | --- |
| `slug` | The service's name, exactly as alerts, evidence sources, runbooks and identity groups spell it (`^[a-z0-9][a-z0-9-]{1,62}$`) |
| `display_name` | For people |
| `owner_group` | The identity-system group that owns it. Runbooks and past incidents are searched for an investigation only where their `allowed_groups` include it |
| `critical` | ADR-49's high-impact rule (two approvers in Release B) |
| `hold_seconds` | ADR-66's hold period, 600 or more |

There is **no catalogue route** yet. Locally, `python -m aisre.api.dev_seed <service> <environment>` adds both rows, and refuses outside development and test. For the pilot, the Platform owner inserts the rows as `aisre_app`, once per service and environment:

```sql
INSERT INTO services (service_id, org_id, slug, display_name, owner_group, critical, hold_seconds)
VALUES ('<uuid7>', '<org-id>', 'payments-api', 'Payments API', 'payments-team', false, 600);
INSERT INTO environments (environment_id, org_id, slug) VALUES ('<uuid7>', '<org-id>', 'production');
```

## 2. Evidence sources

`AISRE_EVIDENCE_SOURCES` (worker) names the read-only adapter that answers each source tool: `get_alert`, `get_metric`, `get_logs`, `get_traces`, `get_changes`, `get_error_groups`, `get_dependency_health` (`SOURCE_TOOLS`). Give your service its own under `"services"`, keyed `service` or `service/environment`; `"*"` stands for every tool not named. The platform-wide `"sources"` map is the fallback for a service with no entry of its own:

```json
{
  "sources": {"*": {"adapter": "<platform-wide adapter>", "...": "..."}},
  "services": {
    "payments-api": {"*": {"adapter": "<adapter>", "...": "its arguments"}},
    "payments-api/production": {"get_logs": {"adapter": "<adapter>", "...": "..."}}
  }
}
```

Lookup is most specific first: `payments-api/production`, then `payments-api`, then `"sources"`. A service no scope covers gets a `gap` for every source, never another service's data, and the diagnosis says what is missing. A misspelt tool or an unknown adapter stops the worker at start.

Each adapter's arguments are its own (its module docstring). Credentials are named by reference, `{"env": "NAME"}` or `{"file": "/path"}`, never written into this JSON; a value given directly is refused at start. Queries are yours: `$service` and `$environment` are put in for each call, quoted for the query language, and `$levels` (logs) is the levels asked for. Each metric is `{"value": query, "requests": query}` (`requests` is optional, and only read for `http_5xx_rate` and `error_rate_by_version`, whose query returns one series per version). `fake_monitoring` is local use only and refused in production. The real adapters, all read-only and **tested against recorded responses only, not yet against a real account** (ADR-75):

| Adapter | Tools | Credentials |
| --- | --- | --- |
| `prometheus` | `get_metric` (HTTP API v1 `query_range`) | optional bearer `token`, or `username` and `token` for basic auth; optional `tenant` |
| `loki` | `get_logs` (`/loki/api/v1/query_range`) | as Prometheus |
| `datadog` | `get_metric` (v1 query), `get_logs` (v2 logs search), `get_changes` (v1 events) | `api_key` and `app_key`, the application key scoped read-only; `site` |
| `cloudwatch` | `get_metric` (GetMetricData), `get_logs` (FilterLogEvents) | the standard AWS chain (optional `profile`); needs the `aws` extra, `uv sync --extra aws` |

No adapter answers `get_alert`, `get_traces`, `get_error_groups` or `get_dependency_health` yet, so the go-live check fails until each has a source or the check is changed (ADR-75). Prometheus and Loki:

```json
{"services": {"payments-api/production": {
  "get_metric": {"adapter": "prometheus", "url": "https://prometheus.internal.example",
    "token": {"file": "/run/secrets/prometheus-token"},
    "metrics": {
      "http_5xx_rate": {
        "value": "sum(rate(http_requests_total{service=\"$service\",env=\"$environment\",code=~\"5..\"}[1m])) / sum(rate(http_requests_total{service=\"$service\",env=\"$environment\"}[1m]))",
        "requests": "sum(increase(http_requests_total{service=\"$service\",env=\"$environment\"}[1m]))"},
      "error_rate_by_version": {
        "value": "sum by (version) (rate(http_requests_total{service=\"$service\",code=~\"5..\"}[1m])) / sum by (version) (rate(http_requests_total{service=\"$service\"}[1m]))",
        "requests": "sum by (version) (increase(http_requests_total{service=\"$service\"}[1m]))"}},
    "environment_label": "env"},
  "get_logs": {"adapter": "loki", "url": "https://loki.internal.example", "tenant": "payments",
    "token": {"env": "LOKI_READ_TOKEN"},
    "query": "{service=\"$service\", env=\"$environment\"} | json | level=~\"$levels\"",
    "environment_label": "env"}}}}
```

Datadog (`$levels` becomes `(error OR warn)`, or the one level asked for):

```json
{"services": {"payments-api/production": {"*": {"adapter": "datadog", "site": "datadoghq.eu",
  "api_key": {"env": "DD_API_KEY"}, "app_key": {"file": "/run/secrets/dd-app-key-readonly"},
  "metrics": {
    "http_5xx_rate": {"value": "sum:trace.http.request.errors{service:$service,env:$environment}.as_rate() / sum:trace.http.request.hits{service:$service,env:$environment}.as_rate()",
      "requests": "sum:trace.http.request.hits{service:$service,env:$environment}.as_count()"},
    "error_rate_by_version": {"value": "sum:trace.http.request.errors{service:$service,env:$environment} by {version}.as_rate() / sum:trace.http.request.hits{service:$service,env:$environment} by {version}.as_rate()",
      "requests": "sum:trace.http.request.hits{service:$service,env:$environment} by {version}.as_count()"}},
  "logs_query": "service:$service env:$environment status:$levels",
  "events_tags": "service:$service,env:$environment", "events_sources": "github"}}}}
```

Deployment events need `service:`, `env:` and `version:` tags (and `previous_version:` if you have it); Datadog doesn't hold the files or configuration keys a release changed, so the fact rules have less to go on than with a full change history. CloudWatch (the worker's role needs only `cloudwatch:GetMetricData` and `logs:FilterLogEvents` on the service's log groups):

```json
{"services": {"payments-api/production": {"*": {"adapter": "cloudwatch", "region": "eu-west-1",
  "metrics": {
    "http_5xx_rate": {"value": "SUM(SEARCH('{App,Service,Env} Service=\"$service\" Env=\"$environment\" MetricName=\"5xxRate\"', 'Average', 60))",
      "requests": "SUM(SEARCH('{App,Service,Env} Service=\"$service\" Env=\"$environment\" MetricName=\"Requests\"', 'Sum', 60))"},
    "error_rate_by_version": {"value": "SEARCH('{App,Service,Env,version} Service=\"$service\" Env=\"$environment\" MetricName=\"5xxRate\"', 'Average', 60)"}},
  "log_group": "/ecs/$service-$environment",
  "filter_pattern": "{ $.level = \"error\" || $.level = \"warn\" }"}}}}
```

## 3. Runbooks and past incidents

Write each as Markdown with a front-matter block (`tests/fixtures/runbooks/` shows every field): `doc_id`, `version`, `title`, `doc_type` (`runbook` or `past_incident`), `services` (yours, or `*`), `allowed_groups` (must include the service's `owner_group`), `owner`, `reviewed_at`, `deprecated`. Ingest a folder as `aisre_app`:

```
AISRE_DATABASE_URL=... uv run python -m aisre.retrieval.corpus <folder> --org <org-id> [--queries <file>]
```

It builds a new index revision, scores it on the labelled queries if given (Artifact 10.13), and activates it only if every target holds (ADR-62). Text is redacted on the way in; removing a document's text later needs the legal deletion decision (README, T4.11).

## 4. Owners, responders and approvers

Rights come only from identity-system groups, one role per service: `aisre:<role>:<service>`, or `aisre:<role>` for every service (ADR-49, ADR-50, ADR-73). Roles: `viewer`, `responder`, `approver`, `incident_lead`, `monitoring` (the alerting system's identity: it can only open incidents), `platform_admin`, `security_lead`. For the pilot each service needs at least:

| Group | Why |
| --- | --- |
| `aisre:monitoring:<service>` or `aisre:monitoring` | The alert source that posts to `POST /v1/incidents` |
| `aisre:responder:<service>` | On-call engineers: read, note, cancel, request re-investigation |
| `aisre:incident_lead:<service>` | Confirms severity, closes, and records the incident review (`aisre incident review`), which the pilot's accuracy is measured from (T5.3) |
| `aisre:approver:<service>` | Not used in Release A (no fix is sent for approval); name them for Release B and for the approval review on a test target (T5.4) |

The API verifies tokens against `AISRE_OIDC_ISSUER`, `_AUDIENCE` and `_JWKS_URL`. **No identity system is chosen** (ADR-73): the stack's is the fake issuer, refused in production.

## 5. Repository and the review system

The Patch Proposer reads, and opens draft pull requests in, the repository **named after the service** (FR-48: never one the model names), through the review system (`AISRE_REVIEW_ADAPTER`, `_URL`, `_TOKEN`, ADR-72). The Test Runner tests a patch against a local git mirror of that repository, from `AISRE_SANDBOX_REPOS` (JSON, service to mirror path), in the `runsc` sandbox (`AISRE_SANDBOX_IMAGE` by digest).

For the pilot that is GitHub (or GitHub Enterprise), `AISRE_REVIEW_ADAPTER=github`, allowed in production because Release A drafts pull requests (ADR-44, M2):

```
AISRE_REVIEW_ADAPTER=github
AISRE_REVIEW_URL=https://api.github.com            # or https://github.example.com/api/v3
AISRE_REVIEW_TOKEN=<from the secrets manager>      # worker only
AISRE_REVIEW_REPOS={"payments-api": "example-org/payments-api"}
```

The token is a fine-grained token (or a GitHub App's) on the pilot's repositories only, with Contents read and write (to create the `aisre/<action>` branch) and Pull requests read and write. The client opens draft pull requests only and refuses any merge in code; branch protection on the default branch stays the barrier. It is tested against recorded responses only, not yet against a real repository (ADR-75): open one draft on the first day. Without it, the go-live check fails: every `draft_patch` would hand over with nothing drafted.

## 6. Release process

In Release A there is none to configure: people change production by hand, and **no release adapter or credential may be set** (ADR-44). Every `AISRE_RELEASE_*` variable fails the go-live check. Production releases run from GitHub Actions; the adapter for it (apply, undo, look up by key) is the next pass's work and stays unusable until HG-10's recorded decision provisions a write credential.

## 7. Paging

The platform's own alerts page by role through Alertmanager (`ops/compose/alertmanager.yml`, ADR-74): Platform owner, Incident lead, Security lead. Replace the three `fake-pager` webhook URLs with your paging service's receivers (a `*_file` form keeps the secret out of the file). Nothing else in that file changes.

## 8. The go-live check

Run it with the production settings in the environment, naming every pilot service:

```
uv run python -m aisre.cli pilot check --service payments-api --service orders-api \
  --alertmanager /path/to/alertmanager.yml
```

It prints one PASS or FAIL line per check and exits 1 on any FAIL; it names variables, never their values. It passes only when Release A holds: production, no release credential, remediation off, nothing fake or development-only (scripted skills, `runc`, fake adapters, development signing keys, the fake pager), a real model key with every task's alias pinned, the GitHub review system with a repository for each named service, evidence sources answering every source tool for each named service in `production` with every credential they reference present, and a paging receiver for each role. What it can't tell, check by hand on the first day: that each credential works, each source answers (post a test alert and read the evidence; a `gap` names the failing source), and a page reaches a person.

Once incidents run, the incident lead records each review (`aisre incident review <id> review.json`), and the SRE lead runs `aisre pilot report`, `pilot baseline` and `pilot approvals` (README, Pilot readiness).
