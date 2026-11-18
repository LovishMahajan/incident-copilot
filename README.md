# Incident Copilot

An AI incident copilot for SRE teams. It investigates an alert through a Temporal workflow, proposes a cited diagnosis and a tested fix, and applies the fix only after human approval, then verifies it.

## Where things live

| Path | Holds |
| --- | --- |
| `src/aisre/` | The `aisre` package: contracts, workflow, agent, gateway, retrieval, remediation, verification, reliability, evaluation |
| `db/` | `schema.sql` and `schema-tests.sql` for PostgreSQL; `migrations/` (Alembic) |
| `ops/` | `alerts.yml`; `compose/` (Compose configuration); `fakes/` (local-only stand-ins: token issuer, model provider, monitoring source) |
| `tests/unit/` | Unit tests |
| `tests/integration/` | Tests against PostgreSQL and Temporal; skipped when those aren't reachable |
| `tests/security/` | Security tests ST-01 to ST-40. See its [README](tests/security/README.md) |
| `tests/fixtures/reference-incident/` | The reference incident |
| `docs/` | The artifact filing |

`docs/` is the record of the artifacts, including the copy of the code each one was written against (`docs/layers/*/files`). `src/` is the code to change. A change there doesn't update the filed copies. See ADR-67 in [docs/decisions.md](docs/decisions.md).

## Run the tests

Needs [uv](https://docs.astral.sh/uv/). The first run downloads Temporal's test server, unless `TEMPORAL_TEST_SERVER` gives a path to one.

```
uv sync --python 3.12
uv run pytest -q
uv run ruff format --check . && uv run ruff check .
uv run mypy src
```

Markers: `integration` (PostgreSQL and Temporal), `system` (Compose stack, gVisor host), `paid` (a real provider). The default run selects everything; marked tests skip, with the reason, when what they need is missing. `uv run pytest -q -m integration` runs only the integration tier. `tests/conftest.py` stops any run whose environment looks like production; its docstring gives the rule.

The live contract test for each pinned Claude model (`tests/integration/test_claude_contract.py`) costs money and runs only with both `AISRE_PAID_TESTS=1` and a test key: `AISRE_PAID_TESTS=1 AISRE_TEST_PROVIDER_KEY=... uv run pytest -q -m paid`. Every other model test uses the fake Messages API in `tests/fake_anthropic.py`. Prompts live in `prompts/<task>/v<N>.md` and are never edited once merged (ADR-63): `uv run python scripts/check_prompts.py --base origin/main` is CI's check.

Settings come from `AISRE_*` environment variables (`src/aisre/config.py`). Copy `.env.example` to `.env` for local values; every value in it is fake.

## Build and test the schema

`db/schema.sql` is frozen: Alembic's `0001_baseline` (in `db/migrations/`, configured by `alembic.ini` at the root) applies it unchanged, and every later change is a new migration. Migrations run as the owning role, from `AISRE_MIGRATION_DATABASE_URL`, and record their version in the `aisre_migrations` schema. `schema-tests.sql` runs as a superuser straight after. 223 of 223 checks pass on PostgreSQL 16.16 and 17.12 (2026-11-18).

```
psql -U postgres -c "CREATE ROLE aisre_owner LOGIN CREATEROLE"
psql -U postgres -c "CREATE DATABASE aisre OWNER aisre_owner"
AISRE_MIGRATION_DATABASE_URL=postgresql+psycopg://aisre_owner@localhost:5432/aisre uv run alembic upgrade head
psql -U postgres -d aisre -v ON_ERROR_STOP=1 -f db/schema-tests.sql
```

`uv run alembic downgrade base` drops everything in `public` and keeps the roles. It is for development and CI only: it deletes all data.

## Run the local stack

Needs Docker with Compose. `compose.yaml` runs PostgreSQL 17.12, Temporal 1.32.2 (schema set up by `temporal-sql-tool`, in its own databases on the same PostgreSQL), the Temporal UI 2.58.1, the migration job, the API and the worker with its outbox relay. From M1 it adds an object store (MinIO, with a one-shot job that creates the artifact bucket and the audit bucket with Object Lock), an OpenTelemetry collector and Prometheus 3.16 (which loads `ops/alerts.yml` and scrapes the API's and worker's `/metrics`), a one-shot job that ingests the synthetic runbook corpus (`corpus-seed`), and three fakes from `ops/fakes/` for local use only: a token issuer, a model provider answering in the Anthropic Messages API shape from scripted fixtures, and a monitoring source serving `tests/fixtures`. None needs a key. Ports are bound to 127.0.0.1; change them in `.env` if one is taken.

```
cp .env.example .env
docker compose up -d --build     # healthy in about a minute; `docker compose ps` to check
docker compose down -v           # stop and delete the data
```

| Service | Address |
| --- | --- |
| API | http://127.0.0.1:8000 (`/livez`, `/readyz`, `/v1/incidents`) |
| Temporal UI | http://127.0.0.1:8233 |
| Temporal frontend | 127.0.0.1:7233 |
| PostgreSQL | 127.0.0.1:55432, database `aisre` |
| Object store (MinIO S3 API, console) | http://127.0.0.1:59000, http://127.0.0.1:59001; buckets `aisre-artifacts` (evidence snapshots), `aisre-audit` (write-once audit copy, Object Lock) |
| Prometheus | http://127.0.0.1:59090 (targets `api`, `worker`, `otel-collector`, `prometheus`) |
| OpenTelemetry collector (OTLP gRPC, HTTP) | 127.0.0.1:54317, 127.0.0.1:54318 |
| Fake token issuer | http://127.0.0.1:59100 (`/jwks`, `POST /token`) |
| Fake model provider | http://127.0.0.1:59200 (`POST /v1/messages`, any `x-api-key`) |
| Fake monitoring source | http://127.0.0.1:59300 (`/v1/scenarios`) |
| Demo viewer (ADR-70) | http://127.0.0.1:58080 (read-only page; see Demo viewer below) |

**Sign-in** (`src/aisre/api/auth.py`, T1.3). Every `/v1` call sends `Authorization: Bearer <token>`. The API verifies the token against the issuer's keys (`AISRE_OIDC_ISSUER`, `AISRE_OIDC_AUDIENCE`, `AISRE_OIDC_JWKS_URL`); in the stack that is the fake issuer, which mints any identity for local use. Roles come from the token's `groups`: `aisre:<role>` on every service, or `aisre:<role>:<service>` on one, with ADR-50's roles (`viewer`, `responder`, `approver`, `incident_lead`, `monitoring`, `platform_admin`). Writes also need an `Idempotency-Key` of 8 to 128 characters:

```
token() { curl -s -X POST http://127.0.0.1:59100/token -d "{\"sub\": \"$1\", \"groups\": [\"$2\"]}" \
  | uv run python -c 'import json, sys; print(json.load(sys.stdin)["access_token"])'; }
MON=$(token monitoring aisre:monitoring)     # creates incidents only
READ=$(token alice aisre:responder)          # reads, and creates
curl -X POST http://127.0.0.1:8000/v1/incidents -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $MON" -H 'Idempotency-Key: demo-0000001' \
  --data @tests/fixtures/reference-incident/alert.json
```

Each route checks ADR-50's matrix on the incident's service (T1.4): Monitoring may create but not read, and a role on another service sees the same 404 as a missing incident. Failed sign-ins, denials and every read of incident content are logged as JSON lines on the API's standard error (`aisre.auth.failed`, `aisre.authz.denied`, `aisre.content.read`). Tokens last 5 minutes; mint new ones when they expire. Without the three `AISRE_OIDC_*` settings the API falls back to the development-only `X-Aisre-Dev-Caller` header (with optional `X-Aisre-Dev-Org` and `X-Aisre-Dev-Groups`, default `aisre:responder`), and then refuses to start unless `AISRE_ENV` is `development` or `test`. With them set, that header is refused. The in-process integration tests use the header.

**Intake** (`src/aisre/api/intake.py`, T1.2). The service and environment must be in the organisation's catalogue (`services`, `environments`), and the caller must hold `incident:create` on that service. Otherwise the alert is rejected: stored raw in `dead_letters` with the reason, logged as `aisre.intake.dead_lettered` for the incident lead, and answered `422 unknown_service` to a monitoring caller or `404 unknown_service` to a person, the same for an unknown and a forbidden service (ADR-58). A body missing a mandatory field is `422 validation_failed`, naming the field, and is dead-lettered too. A repeat of an open incident's alert (same fingerprint, or service plus alert name without one), or any alert for the same service and environment within 10 minutes of the incident opening, joins that incident: `200`, `"deduplicated": true`. M0 has no catalogue route; `python -m aisre.api.dev_seed` adds one service and environment (default `orders-api`/`production`, the reference incident's) to the development organisation, and a rerun changes nothing:

```
docker compose exec api python -m aisre.api.dev_seed
```

**The worker** runs the workflow with the real `load_incident`, `record_transition` and `hand_over`. With `AISRE_SCRIPTED_SKILLS=true` (as in `.env.example`) the activities not built yet answer with fixed results from `src/aisre/workflow/scripted.py`: the slot, the spend state and `draft_patch`, which answers "no allowed fix" until M2, and the model skills too when no provider is configured. Scripted results are refused when `AISRE_ENV` is `production`.

**The model skills** (`src/aisre/agent/skills/`, T1.14 to T1.18). With `AISRE_PROVIDER_API_KEY` set on the worker, `hypothesise`, `diagnose` and `review_diagnosis` are real: each assembles its context from the incident's evidence and the facts behind each record's strength tier, takes its prompt from `prompts/` (`AISRE_PROMPTS_DIR`, v1 active), and asks the Model Gateway, which picks the model per task (ADR-69), reserves budget in PostgreSQL and calls the provider through the Anthropic SDK transport, at `AISRE_PROVIDER_URL` when set. Answers are validated against `agent_contracts`, their citations checked against the incident's records, and output holding a secret, link or command is stopped without retry, its audit copy masked (`aisre.model_output.unsafe` on the audit log, `aisre_model_output_unsafe_total`); a secret takes the security stop. Code scores confidence from the cited observations (ADR-53). Each diagnosis is stored with its hypotheses and their evidence links, and the review writes `final_outcome` and `final_confidence` once (ADR-69). `.env.example` points the worker at Compose's fake provider, whose scripts (`ops/fakes/provider-scripts/`) answer the reference incident with a cited diagnosis naming the `ORDER_TAX_RATE_BPS` change, and the honesty variant with "Root cause not established"; they fill in evidence IDs from the request (`{{ref:<regex>}}`, see `ops/fakes/model_provider.py`). So an incident goes `open` → `investigating` → `diagnosed` → `handed_over`, with the hypotheses, diagnosis and review in its timeline. No real key exists here; a real one belongs in a secrets manager.

**Evidence** (`src/aisre/evidence/`, T1.10). With `AISRE_EVIDENCE_SOURCES` set on the worker, `collect_evidence` is the real Evidence Collector: the baseline plan (alert, changes, 5xx rate, error rate by version, logs, error groups, traces, dependency health) or a targeted round's requests, each through the ToolGate, then the source configured for that tool. Results are scoped to the incident's service, environment and window, redacted, capped, fingerprinted and stored as `evidence` rows with a timeline event each; a failed source is a `gap` row, never an empty one. Tool calls are counted and inserted in one transaction per incident (`tool_calls`, ADR-69). The variable is JSON naming an adapter per tool (`"*"` for the rest); against the fake monitoring source, replaying the reference incident up to its evidence cut-off:

```
AISRE_EVIDENCE_SOURCES='{"sources": {"*": {"adapter": "fake_monitoring", "url": "http://fake-monitoring:8080/v1/scenarios/reference-incident"}}, "replay_as_of": "2026-10-20T14:20:00Z"}'
```

Snapshots go to the object store when `AISRE_OBJECT_STORE_URL`, `_BUCKET`, `_ACCESS_KEY` and `_SECRET_KEY` are set (MinIO in the stack: `http://minio:9000`, `aisre-artifacts` and the `MINIO_ROOT_*` values); without them a record keeps its fingerprint and no snapshot. `.env.example` sets all of these, so the stack's worker runs the real collector against the fake monitoring source: the reference incident gets `alert`, `change`, `metric`, `log`, `error_group`, `trace` and `dependency` rows with their strength tiers, and with `fake-monitoring` stopped each of those is a `gap` row instead. Leave `AISRE_EVIDENCE_SOURCES` empty to go back to the scripted collector. The fake adapter and `replay_as_of` are refused when `AISRE_ENV` is production.

**Runbook retrieval** (`src/aisre/retrieval/corpus.py`, `src/aisre/record/corpus.py`, T1.17). The corpus lives in PostgreSQL (`documents`, `chunks`, `index_revisions`, ADR-62) and BM25 runs in the worker. Ingesting a folder builds a new immutable revision, scores it on the labelled query set (Artifact 10.13) and activates it only if every target holds; the evaluation is stored on the revision (migration `0005_m1_evidence`). `retrieve_knowledge` searches the newest activated revision for the identifiers in the baseline evidence, filtered to the incident's service and its owner group before ranking, and stores each passage as `runbook` or `past_incident` evidence pinned to its revision, document version, chunk and span. No activated revision is a gap. On the API's evidence read, a cited runbook or past incident review shows its passage only to a reader in one of the document's permitted groups (the token's `groups`, or `X-Aisre-Dev-Groups`); anyone else sees "restricted runbook (owner: …)" and which document version it was, never its text (T1.24, Artifact 10.9). No real runbooks exist yet; `tests/fixtures/runbooks/` is a synthetic corpus of 15 documents with a 50-query labelled set, which on 2026-11-18 scored Recall@5 1.00, MRR 0.96, nDCG@5 0.97 (exact identifiers 1.00 / 1.00 / 1.00, paraphrases 1.00 / 0.91 / 0.93), with 0 wrong-service and 0 wrong-permission hits. The corpus and set were written together, so this shows the pipeline works, not how BM25 does on real runbooks. The stack's `corpus-seed` job ingests it for the development organisation on every `docker compose up` (each run is a new revision, activated if the query set still passes), so the reference incident gets `runbook` and `past_incident` evidence. To ingest it by hand from the host:

```
AISRE_DATABASE_URL=postgresql+psycopg://aisre_app:fake-app-password@127.0.0.1:55432/aisre \
  uv run python -m aisre.retrieval.corpus tests/fixtures/runbooks --org 00000000-0000-7000-8000-000000000001
```

**The CLI** (`src/aisre/cli.py`, T1.5) is a thin client of the API, so it can do only what the caller's roles allow. It reads `AISRE_API_URL` (default `http://127.0.0.1:8000`) and a bearer token from `AISRE_TOKEN`:

```
export AISRE_TOKEN=$(token lena aisre:incident_lead)
uv run python -m aisre.cli incident submit tests/fixtures/reference-incident/alert.json
uv run python -m aisre.cli incident show $ID
uv run python -m aisre.cli incident timeline $ID [--follow]   # --follow streams it (SSE) until closed or cancelled
uv run python -m aisre.cli incident cancel $ID --reason "false alarm"
uv run python -m aisre.cli incident close $ID --note "fixed by hand"   # sends the incident's ETag as If-Match
```

Writes send a fresh `Idempotency-Key` unless `--key` gives one. A refusal prints the API's problem document on standard error and exits 1. The fence and restore commands ADR-69 gives the CLI aren't there yet.

**Telemetry and the audit copy** (T1.19, T1.20). The API and the worker send spans to the collector (`OTEL_EXPORTER_OTLP_ENDPOINT`; `docker compose logs otel-collector` shows each batch) and serve `/metrics` on `AISRE_METRICS_PORT` (9464) inside the Compose network, which Prometheus scrapes as jobs `api` and `worker`. Both write the write-once audit copy (`AISRE_AUDIT_STORE_*`) to the `aisre-audit` bucket: timeline events every minute, audit log lines as they happen, each object under COMPLIANCE retention for `AISRE_AUDIT_STORE_LOCK_DAYS` (1 locally), so not even MinIO's root user can delete it until then. `docker compose down -v` still removes it with the volume. Without the `AISRE_AUDIT_STORE_*` settings there is no copy, and the weekly check verifies the chain only. Every one of these settings is in `src/aisre/config.py`.

**Integration tests** run from the host against the stack, with test settings. The owner URL lets the audit copy's tamper tests (ST-31, ST-32) run; without it they skip:

```
AISRE_ENV=test \
AISRE_DATABASE_URL=postgresql+psycopg://aisre_app:fake-app-password@127.0.0.1:55432/aisre \
AISRE_MIGRATION_DATABASE_URL=postgresql+psycopg://aisre_owner:fake-owner-password@127.0.0.1:55432/aisre \
AISRE_TEMPORAL_ADDRESS=127.0.0.1:7233 \
uv run pytest -q -m integration
```

The `integration` tests expect the Compose worker as `.env.example` configures it: scripted skills for what isn't built, real evidence from the fake monitoring source, and the real model skills against the fake provider. Tests that drive an incident themselves open it with no undelivered start row, so the Compose relay never starts it (the `quiet` fixture in `tests/integration/conftest.py`). On 2026-11-18: 134 passed, 2 skipped (`paid`).

**The honesty variant** (Run 2 of the reference incident: `release.json` and `config-diff.txt` withheld). The fake monitoring source serves a scenario with files withheld when its name says so, `reference-incident:without=release.json,config-diff.txt`, and the change history then answers 404, a gap. Restart the worker pointed at it, post the alert, and the incident goes `open` → `investigating` → `inconclusive` → `handed_over`, its diagnosis starting "Root cause not established"; restart without the override to go back:

```
AISRE_EVIDENCE_SOURCES='{"sources": {"*": {"adapter": "fake_monitoring", "url": "http://fake-monitoring:8080/v1/scenarios/reference-incident:without=release.json,config-diff.txt"}}, "replay_as_of": "2026-10-20T14:20:00Z"}' \
  docker compose up -d --wait worker
```

An alert for `orders-api` joins an incident still open within 10 minutes, so close the first run's incident before posting the second.

**Evaluation** (`src/aisre/evaluation/`, T1.22). `tests/fixtures/eval/dev-seed-1/` is a case store and `tests/fixtures/eval/keys-dev-seed-1/` its key store, in Artifact 12.8's layout. It is a **development seed, not the frozen suite**: four development cases, written with the code and never labelled by two SREs, so 12.8's freeze rules refuse it and its manifest says so. `seed-0001` and `seed-0002` are 12.8's reference-incident seeds (the second withholds the change history); `dev-3f9c2a71` is the reference incident on `basket-api` with an instruction planted in a log line, and `dev-8b14e6d0` is it on `cart-api` with the database failing over at the same time and the old version failing too. The fake provider's `eval-*` scripts answer the last two. `python -m aisre.evaluation.seed tests/fixtures/eval` rebuilds both stores from `tests/fixtures/reference-incident/`, and a unit test holds the committed copy to it. The fake monitoring source serves each case by its ID. The runner drives each case through the running stack: it restarts the worker pointed at the case's fixture, posts the case's alert, reads the incident, timeline, evidence, diagnosis and actions through the API, and closes the incident; only after every case has run does it read the keys and score with `eval_harness.score`, writing `scorecard.json`:

```
uv run python -m aisre.evaluation.runner tests/fixtures/eval/dev-seed-1 tests/fixtures/eval/keys-dev-seed-1 --out eval-run
```

A run stops before it could pass `--max-spend-usd` (default $10, ADR-56): a case starts only while the spend so far plus one incident's $2 cap fits, and cases left unrun count as wrong. Against the fake provider nothing is billed; the spend shown is list price on the fake's token counts. `.github/workflows/eval-nightly.yml` runs it every night on the development split. On 2026-11-18, with the fake provider: FR-13 leading cause 2/2, FR-14 1/1, FR-15 5/5 contradicted hypotheses not left supported, FR-16 honesty 1/1, FR-39 injection followed 0/1 and flagged 1/1, accuracy 4/4, evidence coverage 0/2 (the scripted answers never cite the alert, which the keys require). These show the evaluation path works end to end; the answers come from scripts written with the cases, so they say nothing about a model's accuracy.

## Demo viewer

A read-only page for showing an investigation to people who won't run the CLI (ADR-70). It is a showcase tool, not the product dashboard, and it is never deployed. The `demo-viewer` Compose service (nginx 1.30.6, `demo/nginx.conf`) serves the static files in `demo/` (no build step) on http://127.0.0.1:58080 (`AISRE_DEMO_VIEWER_PORT`) and proxies to the API and the fake token issuer on the same origin. With the stack up and the dev seed applied, open the page, pick a persona and press **Start reference incident**, or open an incident by ID (also as `http://127.0.0.1:58080/#<incident-id>`).

What it can do: sign in as a demo persona through the **fake** token issuer (so it only works against a local or test stack), refreshing the 5-minute token a minute early; post `demo/reference-alert.json` (a copy of `tests/fixtures/reference-incident/alert.json`, timestamps unchanged so the evidence replay lines up) with a fresh `Idempotency-Key`; and follow the incident live: the status, the timeline (the SSE stream read with `fetch`, so it can send the bearer token, resumed with `Last-Event-ID` after a drop or a token refresh), the evidence with its strength tiers and gaps, the hypotheses, the diagnosis with each claim's citations linked to its evidence, and the model spend. Runbooks and past incident reviews show what the API returns for the persona: "Responder outside the orders team" sees them as restricted.

What it can't do: anything else. The proxy allows `GET` and `HEAD` on `/v1/...`, `POST` on exactly `/v1/incidents` and on `/token`, and answers every other method `405`, so it can never approve, cancel, close or execute; the API's own ADR-50 checks still apply to each persona. Evidence and model output are rendered as text under a strict Content-Security-Policy (`default-src 'self'`, no inline script or style, no external hosts). An orders-api alert joins an incident until that incident is closed, so a second **Start** reopens the same one; close it with the CLI (`incident close`) for a fresh run.

The scenario is the worker's, not the page's: there is no per-incident choice. For the honesty variant, restart the worker with the override shown under "The honesty variant" above, close the previous run's incident, and start again; the viewer shows the inconclusive diagnosis ("Root cause not established") and the change-history gap. `docker compose up -d --wait worker` without the override goes back. Against the fake provider an incident runs from `open` to `handed_over` in a few seconds.

## M0 demo

From a fresh clone, with Docker running and nothing else on the ports in `.env`. Each block is copied from the run recorded in [the M0 evidence record](docs/layers/layer-13-implementation-planning/milestones/m0.md), except sign-in: M0 named the caller with the development header, and from M1 the stack takes tokens (Sign-in, above), so the calls send `$MON` and `$READ`.

```
uv sync --python 3.12
cp .env.example .env
docker compose up -d --build --wait     # exits 0 once every service is healthy
docker compose exec api python -m aisre.api.dev_seed
token() { curl -s -X POST http://127.0.0.1:59100/token -d "{\"sub\": \"$1\", \"groups\": [\"$2\"]}" \
  | uv run python -c 'import json, sys; print(json.load(sys.stdin)["access_token"])'; }
MON=$(token monitoring aisre:monitoring); READ=$(token alice aisre:responder)
```

Post the reference alert, keep the incident ID, and read the timeline a few seconds later. From M1 the evidence events (`aisre.evidence.collected.v1`) come between `investigating` and `diagnosed`, and it still ends in `aisre.incident.handed_over.v1`:

```
ID=$(curl -s -X POST http://127.0.0.1:8000/v1/incidents -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $MON" -H 'Idempotency-Key: demo-run-0001' \
  --data @tests/fixtures/reference-incident/alert.json \
  | uv run python -c 'import json, sys; print(json.load(sys.stdin)["incident_id"])')
curl -s "http://127.0.0.1:8000/v1/incidents/$ID/timeline" -H "Authorization: Bearer $READ"
```

The workflow is `incident-$ID`: open it in the Temporal UI (http://127.0.0.1:8233, namespace `default`), or ask Temporal from the command line (the admin-tools image has the `temporal` CLI) or through the UI's HTTP API. After the hand-over it stays running, waiting for a person to close the incident:

```
docker compose run --rm --no-deps --entrypoint temporal temporal-namespace workflow describe -w "incident-$ID"
curl -s "http://127.0.0.1:8233/api/v1/namespaces/default/workflows/incident-$ID"
```

Post the same alert again, under a new key. It is attached to the open incident (`200`, `"deduplicated": true`, the same ID): no second incident and no second workflow:

```
curl -s -X POST http://127.0.0.1:8000/v1/incidents -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $MON" -H 'Idempotency-Key: demo-run-0002' \
  --data @tests/fixtures/reference-incident/alert.json
docker compose exec postgres psql -U postgres -d aisre -c 'SELECT count(*) FROM incidents'
docker compose run --rm --no-deps --entrypoint temporal temporal-namespace workflow count
```

Kill the worker during a second run. Restart the worker with each scripted step taking 3 seconds, post an alert for another service (a new incident: from M1, another alert for `orders-api` within 10 minutes joins the first), kill the worker with `SIGKILL` once the incident is `investigating`, start it again, and the incident still reaches `handed_over`. Activities the dead worker held are retried once their heartbeat or start-to-close timeout passes, so allow about a minute:

```
AISRE_SCRIPTED_STEP_SECONDS=3 docker compose up -d --wait worker
docker compose exec api python -m aisre.api.dev_seed checkout production
ID2=$(sed 's/orders-api/checkout/g' tests/fixtures/reference-incident/alert.json \
  | curl -s -X POST http://127.0.0.1:8000/v1/incidents -H 'Content-Type: application/json' \
    -H "Authorization: Bearer $MON" -H 'Idempotency-Key: demo-run-0003' --data @- \
  | uv run python -c 'import json, sys; print(json.load(sys.stdin)["incident_id"])')
until curl -s "http://127.0.0.1:8000/v1/incidents/$ID2" -H "Authorization: Bearer $READ" | grep -q '"status":"investigating"'; do sleep 0.2; done
docker compose kill -s SIGKILL worker
docker compose start worker
until curl -s "http://127.0.0.1:8000/v1/incidents/$ID2" -H "Authorization: Bearer $READ" | grep -q '"status":"handed_over"'; do sleep 2; done
curl -s "http://127.0.0.1:8000/v1/incidents/$ID2/timeline" -H "Authorization: Bearer $READ"
docker compose run --rm --no-deps --entrypoint temporal temporal-namespace workflow show -w "incident-$ID2"
```

The migrations built the schema from empty (37 tables at M0, 39 from migration `0006_m1_ops`), and `docker compose down -v` deletes everything:

```
docker compose exec postgres psql -U postgres -d aisre -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
docker compose down -v
```

