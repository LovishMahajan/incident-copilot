#!/bin/sh
# Artifact 13.3 (ADR-51): first start of the Compose PostgreSQL only (an empty volume).
#
# Creates what db/schema.sql's header expects before `alembic upgrade head` runs: the owning
# role and the `aisre` database. The login roles the services use are created here too, with
# their passwords, so schema.sql's role block finds them and leaves them alone (it creates
# roles without passwords, to be set "outside this file"). Temporal gets its own role and
# its own two databases on the same instance (ADR-51).
#
# Passwords come from .env and are FAKE (.env.example). Never put a real one in that file.
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  -v owner_pw="$AISRE_OWNER_PASSWORD" \
  -v app_pw="$AISRE_APP_PASSWORD" \
  -v dispatcher_pw="$AISRE_DISPATCHER_PASSWORD" \
  -v retention_pw="$AISRE_RETENTION_PASSWORD" \
  -v reporting_pw="$AISRE_REPORTING_PASSWORD" \
  -v temporal_pw="$TEMPORAL_DB_PASSWORD" <<'SQL'
CREATE ROLE aisre_owner LOGIN CREATEROLE PASSWORD :'owner_pw';
CREATE ROLE aisre_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'app_pw';
CREATE ROLE aisre_dispatcher LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'dispatcher_pw';
CREATE ROLE aisre_retention LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'retention_pw';
CREATE ROLE aisre_reporting LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'reporting_pw';
CREATE DATABASE aisre OWNER aisre_owner;

CREATE ROLE temporal LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'temporal_pw';
CREATE DATABASE temporal OWNER temporal;
CREATE DATABASE temporal_visibility OWNER temporal;
SQL
