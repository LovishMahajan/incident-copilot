# Incident Copilot

An AI incident copilot for SRE teams. It investigates an alert through a Temporal workflow, proposes a cited diagnosis and a tested fix, and applies the fix only after human approval, then verifies it.

## Where things live

| Path | Holds |
| --- | --- |
| `src/aisre/` | The `aisre` package: contracts, workflow, agent, gateway, retrieval, remediation, verification, reliability, evaluation |
| `db/` | `schema.sql` and `schema-tests.sql` for PostgreSQL; `migrations/` (Alembic) |
| `ops/` | `alerts.yml` |
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

Needs Docker with Compose. `compose.yaml` runs PostgreSQL 17.12, Temporal 1.32.2 (schema set up by `temporal-sql-tool`, in its own databases on the same PostgreSQL), the Temporal UI 2.58.1, the migration job, the API and the worker with its outbox relay. Ports are bound to 127.0.0.1; change them in `.env` if one is taken.

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

**Development-only sign-in.** M0 has no identity system yet, so the API takes the caller's name from `X-Aisre-Dev-Caller` (and an optional organisation UUID from `X-Aisre-Dev-Org`). Anyone can claim any name, so the API refuses to start unless `AISRE_ENV` is `development` or `test`. Real tokens replace it at M1 (`src/aisre/api/auth.py`). Writes also need an `Idempotency-Key` of 8 to 128 characters:

```
curl -X POST http://127.0.0.1:8000/v1/incidents -H 'Content-Type: application/json' \
  -H 'X-Aisre-Dev-Caller: alice' -H 'Idempotency-Key: demo-0000001' \
  --data @tests/fixtures/reference-incident/alert.json
```

The service and environment must be in the organisation's catalogue (`services`, `environments`); otherwise the answer is `404 unknown_service`.

**Integration tests** run from the host against the stack, with test settings:

```
AISRE_ENV=test \
AISRE_DATABASE_URL=postgresql+psycopg://aisre_app:fake-app-password@127.0.0.1:55432/aisre \
AISRE_TEMPORAL_ADDRESS=127.0.0.1:7233 \
uv run pytest -q -m integration
```
