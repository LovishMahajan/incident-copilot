"""T4.7 (NFR-27, Artifact 4.4): the dashboard covers the six measurements by percentile, and every
histogram it reads is one the platform publishes."""

from __future__ import annotations

import json
import re
from pathlib import Path

from aisre.reliability import telemetry

DASHBOARD = Path(__file__).parents[2] / "ops/grafana/dashboards/nfr27-percentiles.json"


def test_six_measurements_each_at_p50_p95_p99():
    panels = json.loads(DASHBOARD.read_text())["panels"]
    titles = " | ".join(p["title"] for p in panels).lower()
    for m in (
        "queue wait",
        "each step",
        "evidence source",
        "model call",
        "human wait",
        "end to end",
    ):
        assert m in titles, m
    for p in panels:
        text = json.dumps(p["targets"])
        assert all(q in text for q in ("0.5", "0.95", "0.99")), p["title"]


def test_every_histogram_is_published():
    published = {m.name for m in telemetry.REGISTRY.collect()}
    text = DASHBOARD.read_text()
    for metric in re.findall(r"(aisre_[a-z_]+)_bucket", text):
        assert metric in published, metric
