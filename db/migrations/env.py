"""Artifact 13.1 (ADR-05, ADR-64): Alembic environment for the Incident Record.

Runs online only, as the owning role: the URL is `AISRE_MIGRATION_DATABASE_URL`, read through
`aisre.config`. The version table lives in its own schema, `aisre_migrations`, so `public`
holds exactly what the migrations create and the application roles get no rights on it.
Migrations are raw SQL (ADR-04: explicit queries), so there is no `target_metadata` and no
autogenerate.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool, text

from aisre.config import Settings

VERSION_SCHEMA = "aisre_migrations"

if context.config.config_file_name is not None:
    fileConfig(context.config.config_file_name)


def run_migrations_online() -> None:
    url = Settings.from_env().migration_database_url.get_secret_value()
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {VERSION_SCHEMA}"))
        connection.commit()
        context.configure(
            connection=connection,
            version_table_schema=VERSION_SCHEMA,
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise SystemExit("Offline (--sql) mode is not supported: run against a database.")
run_migrations_online()
