"""Baseline: db/schema.sql as it stood when M0 froze it (ADR-64, Artifact 13.1).

Upgrade runs db/schema.sql unchanged, as the owning role, in the migration's transaction:
schema.sql's own `BEGIN;` and `COMMIT;` lines are the only thing left out, so the schema and
the version row commit together or not at all. Every later change is a new migration;
schema.sql stays the frozen input to this one.

Downgrade drops every object in `public` (views, tables with their triggers and indexes,
functions, types, the domain) and leaves the cluster-wide roles schema.sql created, which
other databases may share. It exists for development and CI. Never run it on a database
whose data matters: it deletes everything, and there is no undo.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-11-18
"""

from __future__ import annotations

import re
from pathlib import Path

from alembic import op

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None

SCHEMA_SQL = Path(__file__).parents[2] / "schema.sql"


def schema_body() -> str:
    """schema.sql without its one transaction wrapper; refuses a file it doesn't recognise."""
    sql = SCHEMA_SQL.read_text(encoding="utf-8")
    begins = re.findall(r"^BEGIN;$", sql, flags=re.M)
    commits = re.findall(r"^COMMIT;$", sql, flags=re.M)
    if len(begins) != 1 or len(commits) != 1:
        raise RuntimeError("schema.sql must have exactly one BEGIN; and one COMMIT; line")
    return re.sub(r"^(BEGIN|COMMIT);$", "", sql, flags=re.M)


def run_script(sql: str) -> None:
    """Hand a whole script to psycopg with no parameters, so nothing parses its `:` or `%`.
    It runs on the migration's connection, inside the migration's transaction."""
    with op.get_bind().connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    run_script(schema_body())


DROP_PUBLIC = """
DO $$
DECLARE
  r record;
BEGIN
  FOR r IN SELECT c.oid::regclass AS name, c.relkind FROM pg_class c
           WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('v', 'm') LOOP
    EXECUTE format('DROP %s IF EXISTS %s CASCADE',
                   CASE r.relkind WHEN 'v' THEN 'VIEW' ELSE 'MATERIALIZED VIEW' END, r.name);
  END LOOP;
  FOR r IN SELECT c.oid::regclass AS name FROM pg_class c
           WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p') LOOP
    EXECUTE format('DROP TABLE IF EXISTS %s CASCADE', r.name);
  END LOOP;
  FOR r IN SELECT p.oid::regprocedure AS name FROM pg_proc p
           WHERE p.pronamespace = 'public'::regnamespace LOOP
    EXECUTE format('DROP ROUTINE IF EXISTS %s CASCADE', r.name);
  END LOOP;
  FOR r IN SELECT t.oid::regtype AS name, t.typtype FROM pg_type t
           WHERE t.typnamespace = 'public'::regnamespace AND t.typtype IN ('e', 'd', 'c')
             AND NOT EXISTS (SELECT 1 FROM pg_class c WHERE c.reltype = t.oid) LOOP
    EXECUTE format('DROP %s IF EXISTS %s CASCADE',
                   CASE r.typtype WHEN 'd' THEN 'DOMAIN' ELSE 'TYPE' END, r.name);
  END LOOP;
  FOR r IN SELECT c.oid::regclass AS name FROM pg_class c
           WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'S' LOOP
    EXECUTE format('DROP SEQUENCE IF EXISTS %s CASCADE', r.name);
  END LOOP;
END $$;
"""


def downgrade() -> None:
    run_script(DROP_PUBLIC)
