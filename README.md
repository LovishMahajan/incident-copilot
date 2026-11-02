# Incident Copilot

An AI incident copilot for SRE teams. It investigates an alert through a Temporal workflow, proposes a cited diagnosis and a tested fix, and applies the fix only after human approval, then verifies it.

## Where things live

| Path | Holds |
| --- | --- |
| `src/aisre/` | The `aisre` package: contracts, workflow, agent, gateway, retrieval, remediation, verification, reliability, evaluation |
| `db/` | `schema.sql` and `schema-tests.sql` for PostgreSQL |
| `ops/` | `alerts.yml` |
| `tests/unit/` | Unit tests |
| `tests/security/` | Security tests ST-01 to ST-40. See its [README](tests/security/README.md) |
| `tests/fixtures/reference-incident/` | The reference incident |
| `docs/` | The artifact filing |

`docs/` is the record of the artifacts, including the copy of the code each one was written against (`docs/layers/*/files`). `src/` is the code to change. A change there doesn't update the filed copies. See ADR-67 in [docs/decisions.md](docs/decisions.md).

## Run the tests

Needs [uv](https://docs.astral.sh/uv/). The first run downloads Temporal's test server, unless `TEMPORAL_TEST_SERVER` gives a path to one.

```
uv sync --python 3.12
uv run pytest -q
```

## Load and test the schema

Needs PostgreSQL 16 or later. `schema.sql` loads as the owning role in an empty database, and `schema-tests.sql` runs as a superuser straight after.

```
psql -U postgres -c "CREATE ROLE aisre_owner LOGIN CREATEROLE"
psql -U postgres -c "CREATE DATABASE aisre OWNER aisre_owner"
psql -U aisre_owner -d aisre -v ON_ERROR_STOP=1 -f db/schema.sql
psql -U postgres -d aisre -v ON_ERROR_STOP=1 -f db/schema-tests.sql
```
