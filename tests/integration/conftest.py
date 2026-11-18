"""Artifact 13.7: integration tests, against a real PostgreSQL and Temporal server.

Every test in this folder is marked `integration`. A test asks for what it needs through the
`postgres_url` or `temporal_address` fixture; each fixture connects once per session using the
settings in `aisre.config` (so `AISRE_DATABASE_URL`, `AISRE_TEMPORAL_ADDRESS`, ...) and skips
the test, with the reason, when it can't. So the default `uv run pytest -q` stays offline and
reports these as skips, and `uv run pytest -q -m integration` runs them once the Compose stack
is up.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from aisre.config import Settings

HERE = Path(__file__).parent
CONNECT_TIMEOUT_S = 3


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if HERE in item.path.parents:
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings.from_env()


@pytest.fixture(scope="session")
def postgres_url(settings: Settings) -> str:
    """The app role's URL in libpq form, once a connection with it has succeeded."""
    import psycopg

    url = settings.database_url.get_secret_value().replace("postgresql+psycopg://", "postgresql://")
    try:
        psycopg.connect(url, connect_timeout=CONNECT_TIMEOUT_S).close()
    except psycopg.Error as e:
        pytest.skip(f"PostgreSQL unreachable with AISRE_DATABASE_URL: {type(e).__name__}")
    return url


@pytest.fixture(scope="session")
def temporal_address(settings: Settings) -> str:
    """The Temporal frontend address, once a client has connected to it."""
    from temporalio.client import Client

    async def probe() -> None:
        await asyncio.wait_for(
            Client.connect(settings.temporal_address, namespace=settings.temporal_namespace),
            CONNECT_TIMEOUT_S,
        )

    try:
        asyncio.run(probe())
    except Exception as e:  # any failure to connect is a skip, never an error
        pytest.skip(f"Temporal unreachable at {settings.temporal_address}: {type(e).__name__}")
    return settings.temporal_address


@pytest.fixture(scope="session")
def app_engine(postgres_url: str, settings: Settings):
    """A SQLAlchemy engine as aisre_app (the API's and worker's role)."""
    from aisre.record import make_engine

    engine = make_engine(settings.database_url)
    yield engine
    engine.dispose()


def add_catalogue(engine, org_id, service: str, environment: str = "production") -> None:
    """Put one service and one environment in an organisation's catalogue."""
    from sqlalchemy import insert

    from aisre.record import uuid7
    from aisre.record.tables import environments, services

    with engine.begin() as conn:
        conn.execute(
            insert(services).values(
                service_id=uuid7(),
                org_id=org_id,
                slug=service,
                display_name=service,
                owner_group="test-owners",
                critical=False,
                hold_seconds=600,
            )
        )
        conn.execute(
            insert(environments).values(environment_id=uuid7(), org_id=org_id, slug=environment)
        )


@pytest.fixture
def catalogue(app_engine):
    """A fresh organisation with one service and one environment, so tests never share rows.

    Returns (org_id, service_slug, environment_slug)."""
    from aisre.record import uuid7

    org_id = uuid7()
    slug = f"svc-{org_id.hex[-8:]}"
    add_catalogue(app_engine, org_id, slug)
    return org_id, slug, "production"


@pytest.fixture
def add_service(app_engine):
    """add_catalogue, bound to the app engine: add_service(org_id, slug)."""
    return lambda org_id, slug, environment="production": add_catalogue(
        app_engine, org_id, slug, environment
    )


@pytest.fixture
def quiet(app_engine):
    """quiet(incident_id): mark the incident's start row delivered right after intake, so the
    Compose worker's relay never runs its workflow and the test drives the incident alone.
    The relay polls every 0.5 s; if it claimed the row first, the test fails here, not later."""
    from sqlalchemy import text

    def mark(incident_id) -> None:
        with app_engine.begin() as conn:
            won = conn.execute(
                text(
                    "UPDATE outbox SET delivered_at = now() WHERE incident_id = :i"
                    " AND kind = 'start_workflow' AND delivered_at IS NULL"
                ),
                {"i": str(incident_id)},
            ).rowcount
        assert won == 1, f"the Compose relay started incident {incident_id} first; rerun"

    return mark
