"""Artifact 13.5 T0.12: the development catalogue seed, against PostgreSQL as aisre_app."""

from __future__ import annotations

from sqlalchemy import func, select

from aisre.api.dev_seed import seed
from aisre.record import uuid7
from aisre.record.tables import environments, services


def test_seed_adds_once_and_a_rerun_is_harmless(app_engine):
    org = uuid7()
    with app_engine.begin() as conn:
        assert seed(conn, org, "orders-api", "production") == (True, True)
    with app_engine.begin() as conn:
        assert seed(conn, org, "orders-api", "production") == (False, False)
    with app_engine.connect() as conn:
        assert conn.execute(select(func.count()).where(services.c.org_id == org)).scalar() == 1
        n_env = conn.execute(select(func.count()).where(environments.c.org_id == org)).scalar()
        assert n_env == 1
