"""Artifact 13.7: the services the integration tests need answer, as the app role."""

from __future__ import annotations

import asyncio

from temporalio.client import Client


def test_postgres_answers(postgres_url):
    import psycopg

    with psycopg.connect(postgres_url) as conn:
        assert conn.execute("SELECT 1").fetchone() == (1,)


def test_temporal_answers(temporal_address, settings):
    async def namespace_known() -> bool:
        client = await Client.connect(temporal_address, namespace=settings.temporal_namespace)
        return client.namespace == settings.temporal_namespace

    assert asyncio.run(namespace_known())
