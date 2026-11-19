"""Artifact 8.9, 13.5 T3.10 (WR-29, RB-04): the workflow reconciliation job's activity.

`reconcile_workflows` runs every 5 minutes (provisional) from the `aisre-reconciliation`
schedule, as ReconciliationWorkflow's one activity on the worker. It compares the record with
Temporal: the incidents not yet closed or cancelled, and any that are but still have a live run;
each running IncidentWorkflow's `current_state` query; the investigation queue; and the starts
still waiting in the outbox. activities.reconciliation_findings applies the 8.9 table. The
mismatches per state set `aisre_workflows_stalled{state}`, which raises AisreWorkflowStalled
(ops/alerts.yml, RB-04): alerts only, the record wins. Its one write removes queue rows whose
run has ended (ADR-69).

A run whose query doesn't answer (a worker gone, or a run from older code) is left out of the
comparison rather than guessed: the record's status alone isn't a mismatch for it.

An incident whose start was withheld on purpose (every start row delivered with no attempt,
which the relay never writes) and that has no live run is told apart from a stall and left out:
see activities.reconciliation_findings. One the relay really started is always judged.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text
from temporalio import activity
from temporalio.client import Client

from aisre.reliability import telemetry
from aisre.workflow import activities as act
from aisre.workflow.states import IncidentState

RUNNING = 'WorkflowType="IncidentWorkflow" AND ExecutionStatus="Running"'
QUERIES_AT_ONCE = 10


def incident_of(workflow_id: str) -> str:
    """`incident-<id>` or `incident-<id>-r<N>` (ADR-46) -> `<id>`."""
    return workflow_id.removeprefix("incident-")[:36]


class ReconciliationActivities:
    def __init__(self, engine: Engine, client: Client) -> None:
        self.engine, self.client = engine, client

    def all(self) -> list[Callable[..., Any]]:
        return [self.reconcile_workflows]

    async def _running(self) -> dict[str, str | None]:
        """Running workflow ID -> its current incident state, or None if it didn't answer."""
        ids = [w.id async for w in self.client.list_workflows(RUNNING)]
        gate = asyncio.Semaphore(QUERIES_AT_ONCE)

        async def ask(wid: str) -> str | None:
            async with gate:
                try:
                    state = await self.client.get_workflow_handle(wid).query("current_state")
                except Exception:  # noqa: BLE001 (any failure: not judged)
                    return None
                activity.heartbeat(wid)
                return str(state["incident"]) if isinstance(state, dict) else None

        answers = await asyncio.gather(*(ask(w) for w in ids))
        return dict(zip(ids, answers, strict=True))

    def _read(self, live: list[str]) -> tuple[dict[str, str], dict[str, str], set[str], set[str]]:
        with self.engine.connect() as conn:
            incidents = {
                str(i): str(s)
                for i, s in conn.execute(
                    text(
                        "SELECT incident_id, status FROM incidents "
                        "WHERE status NOT IN ('closed', 'cancelled') "
                        "OR incident_id = ANY(CAST(:live AS uuid[]))"
                    ),
                    {"live": live},
                )
            }
            queue = {
                str(i): str(r)
                for i, r in conn.execute(
                    text("SELECT incident_id, run_id FROM investigation_queue")
                )
            }
            pending = {
                str(i)
                for (i,) in conn.execute(
                    text(
                        "SELECT DISTINCT incident_id FROM outbox "
                        "WHERE kind = 'start_workflow' AND delivered_at IS NULL"
                    )
                )
            }
            unstarted = {
                str(i)
                for (i,) in conn.execute(
                    text(
                        "SELECT o.incident_id FROM outbox o JOIN incidents i USING (incident_id) "
                        "WHERE o.kind = 'start_workflow' "
                        "AND i.status NOT IN ('closed', 'cancelled') "
                        "GROUP BY o.incident_id "
                        "HAVING bool_and(delivered_at IS NOT NULL AND attempts = 0)"
                    )
                )
            }
        return incidents, queue, pending, unstarted

    def _remove(self, orphans: list[str], queue: dict[str, str]) -> None:
        with self.engine.begin() as conn:
            for iid in orphans:
                conn.execute(
                    text("DELETE FROM investigation_queue WHERE incident_id = :i AND run_id = :r"),
                    {"i": UUID(iid), "r": queue[iid]},
                )

    @activity.defn(name="reconcile_workflows")
    async def reconcile_workflows(self, inp: act.ReconciliationIn) -> act.ReconciliationOut:
        running = await self._running()
        live: dict[str, str] = {}
        for wid in sorted(running):  # a later run (-rN) sorts after its first
            state = running[wid]
            if state is not None:
                live[incident_of(wid)] = state
        incidents, queue, pending, unstarted = await asyncio.to_thread(self._read, sorted(live))
        judged = {i: s for i, s in incidents.items() if i in live or i not in _unanswered(running)}
        stalled, orphans = act.reconciliation_findings(
            judged, live, queue, pending, set(running), unstarted & set(judged)
        )
        for state in IncidentState:
            telemetry.workflows_stalled.labels(state=state.value).set(stalled.get(state.value, 0))
        if orphans:
            await asyncio.to_thread(self._remove, orphans, queue)
        return act.ReconciliationOut(sum(stalled.values()))


def _unanswered(running: dict[str, str | None]) -> set[str]:
    return {incident_of(w) for w, s in running.items() if s is None}
