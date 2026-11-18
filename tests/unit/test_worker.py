"""Artifact 13.5 T0.10: the worker entry point, offline."""

from __future__ import annotations

import os
import time

from aisre.workflow import activities as act
from aisre.workflow import worker


def test_worker_registers_the_investigation_queue_only():
    names = {a.__name__ for a in act.ALL_INVESTIGATION_ACTIVITIES}
    assert not names & {a.__name__ for a in act.ALL_DISPATCHER_ACTIVITIES}
    assert worker.act.INVESTIGATION_QUEUE == "aisre-investigation"


def test_check_alive_follows_the_file(tmp_path, monkeypatch):
    alive = tmp_path / "alive"
    monkeypatch.setattr(worker, "ALIVE_FILE", alive)
    assert worker.main(["--check-alive"]) == 1  # never touched
    alive.touch()
    assert worker.main(["--check-alive"]) == 0
    old = time.time() - worker.ALIVE_WITHIN.total_seconds() - 5
    os.utime(alive, (old, old))
    assert worker.main(["--check-alive"]) == 1


def test_unknown_arguments_are_refused():
    assert worker.main(["--nope"]) == 2
