"""Artifact 13.7 (Q13.23): the session guard. Tests never run against production.

Before collection, so before the first test, the run stops with exit code 4 if any of these
holds in the environment:

1. `AISRE_ENV` is set to anything but `test`. Unset counts as `test`, and the guard sets it so
   the code under test sees it. A shell that loaded `.env` (`development`) must unset it.
2. A database URL points at a host that isn't local. Checked: `AISRE_DATABASE_URL`,
   `AISRE_DISPATCHER_DATABASE_URL`, `AISRE_MIGRATION_DATABASE_URL`, any other
   `AISRE_*DATABASE_URL`, `DATABASE_URL` and `PGHOST`. Local means `localhost`, `127.0.0.1`,
   `::1`, a Unix socket (no host), a name ending `.localhost` or `.test`, or the Compose
   service `postgres`.
3. `AISRE_TEMPORAL_ADDRESS` points at a host that isn't local, by the same rule with the
   Compose service `temporal` in place of `postgres`.
4. A production variable is set, even empty: `ANTHROPIC_API_KEY` (tests read only
   `AISRE_TEST_PROVIDER_KEY`), `AISRE_PROVIDER_KEY`, or any `AISRE_` name containing `PROD`.

Developers never hold production credentials; this guard is the backstop if one is ever in a
shell. If it fires, follow the leak procedure. Tested in tests/unit/test_session_guard.py.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from urllib.parse import parse_qs, urlsplit

import pytest

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", ""}
DATABASE_VARS = ("DATABASE_URL", "PGHOST")
PRODUCTION_VARS = ("ANTHROPIC_API_KEY", "AISRE_PROVIDER_KEY")
GUARD_EXIT_CODE = 4


def _is_local(host: str, service: str) -> bool:
    host = host.strip("[]").lower()
    return (
        host in LOCAL_HOSTS
        or host == service
        or host.endswith(".localhost")
        or host.endswith(".test")
    )


def _url_hosts(url: str) -> list[str]:
    if "://" not in url:  # PGHOST: a bare host name or socket directory
        return ["" if url.startswith("/") else url]
    parts = urlsplit(url)
    hosts = [parts.hostname or ""]
    for h in parse_qs(parts.query).get("host", []):  # libpq's ?host= overrides the netloc
        hosts.append("" if h.startswith("/") else h)
    return hosts


def production_findings(environ: Mapping[str, str]) -> list[str]:
    """Every reason the guard stops, by variable name; the values aren't repeated."""
    found = []
    env = environ.get("AISRE_ENV")
    if env is not None and env != "test":
        found.append(f"AISRE_ENV is {env!r}, not 'test'")
    for name, value in environ.items():
        if name in PRODUCTION_VARS or (name.startswith("AISRE_") and "PROD" in name):
            found.append(f"{name} is set")
        is_db = name in DATABASE_VARS or (
            name.startswith("AISRE_") and name.endswith("DATABASE_URL")
        )
        if is_db and value and not all(_is_local(h, "postgres") for h in _url_hosts(value)):
            found.append(f"{name} names a host that isn't local")
    address = environ.get("AISRE_TEMPORAL_ADDRESS", "")
    if address and not _is_local(address.rsplit(":", 1)[0], "temporal"):
        found.append("AISRE_TEMPORAL_ADDRESS names a host that isn't local")
    return found


def pytest_configure(config: pytest.Config) -> None:
    findings = production_findings(os.environ)
    if findings:
        pytest.exit(
            "Session guard (tests/conftest.py): refusing to run tests. "
            + "; ".join(findings)
            + ". See the module docstring for the rule.",
            returncode=GUARD_EXIT_CODE,
        )
    os.environ.setdefault("AISRE_ENV", "test")
