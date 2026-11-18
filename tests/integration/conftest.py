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
