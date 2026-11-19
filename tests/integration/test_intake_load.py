"""T4.10 (NFR-01, NFR-30, NFR-31): intake under load at 10x the expected peak, on the Compose stack.

`load`: only `-m load` runs it. MEASURED ON ONE DEVELOPER MAC, with every service, the fakes and
the test on the same machine: it shows the path holds at this rate, not production capacity.

No Artifact gives an intake rate. The expected peak is taken as NFR-16's burst, 20 alerts in a
minute (ADR-74, an assumption for the Platform owner to confirm), so 10x is 200 alerts a
minute. AISRE_LOAD_RATE_PER_MIN and AISRE_LOAD_SECONDS change it; the default runs one minute,
the long form is `AISRE_LOAD_SECONDS=600`. Alerts go to SERVICES services, so most of them join an
open incident (the 10-minute grouping, FR-43) as alerts do in a real storm, and SERVICES
incidents are opened and investigated.

Checked:
- NFR-01: every request answered 2xx (the target is 99.9%; any 5xx or no answer fails).
- NFR-30: intake p95 <= 300 ms and p99 <= 1 s, timed by the client (nearest rank, 12.9).
- NFR-31: every opened incident's start is delivered from the outbox (0 lost), and acceptance
  to delivery p95 <= 1 s and p99 <= 5 s, from the outbox's own timestamps.
"""

from __future__ import annotations

import math
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
import stack
from sqlalchemy import text
from test_alert_faults import seeded_service

pytestmark = pytest.mark.load

RATE_PER_MIN = float(os.environ.get("AISRE_LOAD_RATE_PER_MIN", "200"))  # 10x 20 a minute
SECONDS = float(os.environ.get("AISRE_LOAD_SECONDS", "60"))
SERVICES = 10


def nearest_rank(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q / 100 * len(ordered)) - 1)]


def test_intake_at_ten_times_the_expected_peak(app_engine):
    why = stack.available()
    if why:
        pytest.skip(why)
    services = [seeded_service() for _ in range(SERVICES)]
    mon = stack.token_for("monitoring", ["aisre:monitoring"])
    n = int(RATE_PER_MIN * SECONDS / 60)
    gap = 60 / RATE_PER_MIN
    results: list[tuple[int, float, str | None]] = []
    lock = threading.Lock()

    def post(i: int) -> None:
        body = {
            "service": services[i % SERVICES],
            "environment": "production",
            "title": f"M4 load alert {i}",
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": "monitoring",
            "fingerprint": f"m4-load-{i % SERVICES}",
        }
        t0 = time.perf_counter()
        code, out = stack.http(
            "POST",
            f"{stack.API}/v1/incidents",
            body,
            {"Authorization": f"Bearer {mon}", "Idempotency-Key": f"m4-load-{uuid.uuid4().hex}"},
        )
        took = time.perf_counter() - t0
        iid = str(out["incident_id"]) if isinstance(out, dict) and "incident_id" in out else None
        with lock:
            results.append((code, took, iid))

    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=32) as pool:
        for i in range(n):
            delay = start + i * gap - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            pool.submit(post, i)
    wall = time.monotonic() - start

    codes = [c for c, _, _ in results]
    ok = sum(200 <= c < 300 for c in codes)
    lat = [t for _, t, _ in results]
    p95, p99 = nearest_rank(lat, 95), nearest_rank(lat, 99)
    incidents = sorted({i for c, _, i in results if c == 201 and i})

    def delivered() -> list[float] | None:
        with app_engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT EXTRACT(EPOCH FROM o.delivered_at - o.created_at) FROM outbox o "
                    "WHERE o.incident_id = ANY(CAST(:i AS uuid[])) AND o.kind = 'start_workflow'"
                ),
                {"i": incidents},
            ).all()
        if len(rows) != len(incidents) or any(r[0] is None for r in rows):
            return None
        return [float(r[0]) for r in rows]

    waits = stack.wait_for(delivered, 120, "every start to be delivered", 2)
    e95, e99 = nearest_rank(waits, 95), nearest_rank(waits, 99)
    print(
        f"load: {len(results)} alerts in {wall:.0f} s ({len(results) / wall * 60:.0f}/min) to "
        f"{SERVICES} services; {ok}/{len(results)} 2xx; intake p95 {p95 * 1000:.0f} ms, "
        f"p99 {p99 * 1000:.0f} ms, max {max(lat) * 1000:.0f} ms; {len(incidents)} incidents "
        f"opened, all delivered: p95 {e95:.2f} s, p99 {e99:.2f} s (this Mac only)"
    )
    assert len(results) == n and ok == n, sorted(set(codes))  # NFR-01
    assert p95 <= 0.3 and p99 <= 1.0  # NFR-30
    assert e95 <= 1.0 and e99 <= 5.0  # NFR-31, and none lost
