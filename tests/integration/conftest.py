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
    """Every test here is `integration`. A `fault` test (M4: it stops or kills Compose services
    and takes minutes) runs only when `-m` names `fault`, and a `load` test (T4.10) only when it
    names `load`; otherwise they are deselected, so neither the default run nor `-m integration`
    includes them."""
    for item in items:
        if HERE in item.path.parents:
            item.add_marker(pytest.mark.integration)
    named = config.getoption("markexpr", "") or ""
    kept = {m for m in ("fault", "load") if m in named}

    def dropped_here(i: pytest.Item) -> bool:
        return any(i.get_closest_marker(m) and m not in kept for m in ("fault", "load"))

    dropped = [i for i in items if dropped_here(i)]
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = [i for i in items if not dropped_here(i)]


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


REFERENCE_ALERT = HERE.parent / "fixtures/reference-incident/alert.json"


@pytest.fixture
def quiet(app_engine):
    """quiet(org_id, service) -> incident_id: open an incident the Compose worker never runs, so
    the test drives it alone. It goes through intake's own `create_incident`, as the reference
    alert from the monitoring caller, and its start row is marked delivered in the same
    transaction: the relay only claims undelivered rows, so it never sees one. Not a race."""
    import json

    from sqlalchemy import func, update

    from aisre.api.alerts import to_incident_create
    from aisre.record.incidents import create_incident
    from aisre.record.tables import outbox

    body = json.loads(REFERENCE_ALERT.read_text())["incident_create"]

    def open_quietly(org_id, service: str) -> str:
        with app_engine.begin() as conn:
            made = create_incident(
                conn,
                org_id=org_id,
                caller_id="dev:monitoring",
                body=to_incident_create(body | {"service": service}),
            )
            assert not made.deduplicated
            conn.execute(
                update(outbox)
                .where(outbox.c.incident_id == made.incident_id, outbox.c.delivered_at.is_(None))
                .values(delivered_at=func.now())
            )
        return str(made.incident_id)

    return open_quietly


ROOT = HERE.parents[1]


@pytest.fixture(scope="session")
def sandbox_image() -> str:
    """M2 (ADR-71): the harness image from ops/sandbox, built here, by its ID. Skips without
    Docker. The sandboxes these tests start run under runc and prove the flow, not isolation."""
    import subprocess

    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        pytest.skip(f"Docker unavailable for the sandbox: {type(e).__name__}")
    built = subprocess.run(
        [
            "docker",
            "build",
            "--quiet",
            "--label",
            "aisre.harness.image=1",
            str(ROOT / "ops/sandbox"),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=600,
    )
    return built.stdout.strip()


@pytest.fixture(scope="session")
def reference_repo(tmp_path_factory) -> tuple[Path, dict[str, str]]:
    """The runnable orders-api (T2.3) as a git repository: (path, tag -> commit)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "build_repo", ROOT / "ops/reference-app/build_repo.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    dest = tmp_path_factory.mktemp("reference") / "orders-api"
    return dest, module.build(dest)
