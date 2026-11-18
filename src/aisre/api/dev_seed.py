"""DEVELOPMENT ONLY (M0, T0.12): put one service and one environment in an organisation's catalogue.

Intake answers `404 unknown_service` until the alert's service and environment are in the
caller's organisation's catalogue (Artifact 7.8), and M0 has no catalogue route. This fills
the gap for the local stack:

    docker compose exec api python -m aisre.api.dev_seed                 # orders-api/production
    docker compose exec api python -m aisre.api.dev_seed checkout staging --org <uuid>

The organisation defaults to the development caller's (aisre.api.auth.DEV_ORG_ID); the
defaults match tests/fixtures/reference-incident/service-catalog.json. Rerunning is harmless:
an existing service or environment is left as it is. Writes as aisre_app, from
`AISRE_DATABASE_URL`, and refuses unless `AISRE_ENV` is development or test.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import Connection
from sqlalchemy.dialects.postgresql import insert

from aisre.api.auth import DEV_ORG_ID, require_development
from aisre.config import Settings
from aisre.record import make_engine, uuid7
from aisre.record.tables import environments, services

REFERENCE_SERVICE = "orders-api"
REFERENCE_ENVIRONMENT = "production"
REFERENCE_OWNER_GROUP = "orders-team"
REFERENCE_HOLD_SECONDS = 600


def seed(
    conn: Connection,
    org_id: UUID,
    service: str,
    environment: str,
    owner_group: str = REFERENCE_OWNER_GROUP,
    hold_seconds: int = REFERENCE_HOLD_SECONDS,
) -> tuple[bool, bool]:
    """Insert what is missing. Returns (service added, environment added)."""
    added_service = conn.execute(
        insert(services)
        .values(
            service_id=uuid7(),
            org_id=org_id,
            slug=service,
            display_name=service,
            owner_group=owner_group,
            critical=False,
            hold_seconds=hold_seconds,
        )
        .on_conflict_do_nothing(index_elements=["org_id", "slug"])
        .returning(services.c.service_id)
    ).first()
    added_environment = conn.execute(
        insert(environments)
        .values(environment_id=uuid7(), org_id=org_id, slug=environment)
        .on_conflict_do_nothing(index_elements=["org_id", "slug"])
        .returning(environments.c.environment_id)
    ).first()
    return added_service is not None, added_environment is not None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m aisre.api.dev_seed", description=__doc__)
    parser.add_argument("service", nargs="?", default=REFERENCE_SERVICE)
    parser.add_argument("environment", nargs="?", default=REFERENCE_ENVIRONMENT)
    parser.add_argument("--org", type=UUID, default=DEV_ORG_ID)
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    require_development(settings.env)
    engine = make_engine(settings.database_url)
    try:
        with engine.begin() as conn:
            added = seed(conn, args.org, args.service, args.environment)
    finally:
        engine.dispose()
    state = {(True, True): "added", (False, False): "already present"}.get(added, "completed")
    print(f"catalogue for org {args.org}: {args.service}/{args.environment} {state}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
