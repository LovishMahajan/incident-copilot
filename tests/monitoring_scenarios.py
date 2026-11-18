"""Test helpers: the fake monitoring source (ops/fakes/monitoring.py) in-process, serving the
reference incident or a copy of it with files withheld or changed (T1.10, T1.11)."""

from __future__ import annotations

import importlib.util
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

from aisre.evidence.sources import SourceSet

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests/fixtures"
REFERENCE = "reference-incident"
STARTED = datetime(2026, 10, 20, 14, 7, tzinfo=UTC)  # alert.json incident_create.started_at
CUT_OFF = datetime(2026, 10, 20, 14, 20, tzinfo=UTC)  # the evidence cut-off (README)
ALERT_ID = "alrt-20261020-0417"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "fake_monitoring", ROOT / "ops/fakes/monitoring.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


monitoring = _load()


def copy_reference(tmp: Path, without: tuple[str, ...] = (), name: str = REFERENCE) -> Path:
    """A copy of the reference incident under `tmp`, minus `without`. Returns the scenarios root."""
    target = tmp / name
    shutil.copytree(FIXTURES / REFERENCE, target)
    for f in without:
        (target / f).unlink()
    return tmp


@contextmanager
def serving(root: Path = FIXTURES, scenario: str = REFERENCE) -> Iterator[str]:
    """Serves `root` on a free port; yields the scenario's URL."""
    server = monitoring.serve(root)
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1/scenarios/{scenario}"
    finally:
        server.shutdown()
        server.server_close()


def sources(url: str, **extra: object) -> SourceSet:
    return SourceSet.from_config(
        {"sources": {"*": {"adapter": "fake_monitoring", "url": url}}, **extra}
    )
