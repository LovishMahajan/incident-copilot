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

Needs Docker with Compose. `compose.yaml` runs PostgreSQL 17.12, Temporal 1.32.2 (schema set up by `temporal-sql-tool`, in its own databases on the same PostgreSQL), the Temporal UI 2.58.1, the migration job, the API and the worker with its outbox relay. From M1 it adds an object store (MinIO, with a one-shot job that creates the bucket), an OpenTelemetry collector and Prometheus 3.16 (which loads `ops/alerts.yml`), and three fakes from `ops/fakes/` for local use only: a token issuer, a model provider answering in the Anthropic Messages API shape from scripted fixtures, and a monitoring source serving `tests/fixtures`. None needs a key. Ports are bound to 127.0.0.1; change them in `.env` if one is taken.

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
| Object store (MinIO S3 API, console) | http://127.0.0.1:59000, http://127.0.0.1:59001; bucket `aisre-artifacts` |
| Prometheus | http://127.0.0.1:59090 |
| OpenTelemetry collector (OTLP gRPC, HTTP) | 127.0.0.1:54317, 127.0.0.1:54318 |
| Fake token issuer | http://127.0.0.1:59100 (`/jwks`, `POST /token`) |
| Fake model provider | http://127.0.0.1:59200 (`POST /v1/messages`, any `x-api-key`) |
| Fake monitoring source | http://127.0.0.1:59300 (`/v1/scenarios`) |

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

**The worker** runs the workflow with the real `load_incident`, `record_transition` and `hand_over`. M0 has no model, so with `AISRE_SCRIPTED_SKILLS=true` (as in `.env.example`) the other activities answer with fixed results from `src/aisre/workflow/scripted.py`, and an incident goes `open` → `investigating` → `diagnosed` → `handed_over`, with every step in its timeline. Scripted results are refused when `AISRE_ENV` is `production`.

**Evidence** (`src/aisre/evidence/`, T1.10). With `AISRE_EVIDENCE_SOURCES` set on the worker, `collect_evidence` is the real Evidence Collector: the baseline plan (alert, changes, 5xx rate, error rate by version, logs, error groups, traces, dependency health) or a targeted round's requests, each through the ToolGate, then the source configured for that tool. Results are scoped to the incident's service, environment and window, redacted, capped, fingerprinted and stored as `evidence` rows with a timeline event each; a failed source is a `gap` row, never an empty one. Tool calls are counted and inserted in one transaction per incident (`tool_calls`, ADR-69). The variable is JSON naming an adapter per tool (`"*"` for the rest); against the fake monitoring source, replaying the reference incident up to its evidence cut-off:

```
AISRE_EVIDENCE_SOURCES='{"sources": {"*": {"adapter": "fake_monitoring", "url": "http://fake-monitoring:8080/v1/scenarios/reference-incident"}}, "replay_as_of": "2026-10-20T14:20:00Z"}'
```

Snapshots go to the object store when `AISRE_OBJECT_STORE_URL`, `_BUCKET`, `_ACCESS_KEY` and `_SECRET_KEY` are set (MinIO in the stack: `http://minio:9000`, `aisre-artifacts` and the `MINIO_ROOT_*` values); without them a record keeps its fingerprint and no snapshot. `compose.yaml` doesn't pass these to the worker yet, so the stack still runs the scripted collector. The fake adapter and `replay_as_of` are refused when `AISRE_ENV` is production.

**Runbook retrieval** (`src/aisre/retrieval/corpus.py`, `src/aisre/record/corpus.py`, T1.17). The corpus lives in PostgreSQL (`documents`, `chunks`, `index_revisions`, ADR-62) and BM25 runs in the worker. Ingesting a folder builds a new immutable revision, scores it on the labelled query set (Artifact 10.13) and activates it only if every target holds; the evaluation is stored on the revision (migration `0005_m1_evidence`). `retrieve_knowledge` searches the newest activated revision for the identifiers in the baseline evidence, filtered to the incident's service and its owner group before ranking, and stores each passage as `runbook` or `past_incident` evidence pinned to its revision, document version, chunk and span. No activated revision is a gap. No real runbooks exist yet; `tests/fixtures/runbooks/` is a synthetic corpus of 15 documents with a 50-query labelled set, which on 2026-11-18 scored Recall@5 1.00, MRR 0.96, nDCG@5 0.97 (exact identifiers 1.00 / 1.00 / 1.00, paraphrases 1.00 / 0.91 / 0.93), with 0 wrong-service and 0 wrong-permission hits. The corpus and set were written together, so this shows the pipeline works, not how BM25 does on real runbooks. To ingest it into the stack's database for the development organisation (the image doesn't hold `tests/`, so run it from the host):

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

**Integration tests** run from the host against the stack, with test settings:

```
AISRE_ENV=test \
AISRE_DATABASE_URL=postgresql+psycopg://aisre_app:fake-app-password@127.0.0.1:55432/aisre \
AISRE_TEMPORAL_ADDRESS=127.0.0.1:7233 \
uv run pytest -q -m integration
```

The `integration` tests expect the Compose worker to run with scripted skills.

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

Post the reference alert, keep the incident ID, and read the timeline a few seconds later. It ends in `aisre.incident.handed_over.v1`:

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

The migrations built the schema from empty (37 tables), and `docker compose down -v` deletes everything:

```
docker compose exec postgres psql -U postgres -d aisre -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
docker compose down -v
```

