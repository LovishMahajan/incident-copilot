"""T4.7 (NFR-27): the provisioned Grafana dashboard answers every one of its queries on the stack.

Each panel's query goes through Grafana's own query API (`/api/ds/query`), so the data source
wiring, the PromQL and the SQL are all exercised. A query may return no data where the stack has
none yet (a human wait needs an approval); it must not return an error.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import stack

GRAFANA = "http://127.0.0.1:59030"
DASHBOARD = Path(__file__).parents[2] / "ops/grafana/dashboards/nfr27-percentiles.json"


def test_every_panel_query_runs_through_grafana():
    code, found = stack.http("GET", f"{GRAFANA}/api/search?query=NFR-27")
    if code != 200:
        pytest.skip(f"Grafana isn't answering on {GRAFANA}")
    assert [d["uid"] for d in found] == ["aisre-nfr27"]  # provisioned from the file
    panels = json.loads(DASHBOARD.read_text())["panels"]
    for panel in panels:
        for target in panel["targets"]:
            code, out = stack.http(
                "POST",
                f"{GRAFANA}/api/ds/query",
                {"from": "now-24h", "to": "now", "queries": [target | {"intervalMs": 60000}]},
            )
            assert code == 200, (panel["title"], out)
            result = out["results"][target["refId"]]
            assert "error" not in result, (panel["title"], result.get("error"))
