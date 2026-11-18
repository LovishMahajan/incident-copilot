"""Artifact 12.1, 12.2 (T1.19, NFR-26): every worker step timed and traced, with the incident ID.

Worker interceptors, registered by aisre.workflow.worker, outermost first:

1. Temporal's own `TracingInterceptor`: a span for each workflow and activity, carried across
   the client, the workflow and its activities by Temporal headers.
2. `StepTiming`: each activity's duration in `aisre_step_seconds{step, outcome}` (outcome ok,
   error or cancelled), and `aisre.incident_id`, `aisre.step` and `aisre.outcome` on the
   activity's span. The incident ID is read from the activity's input, whose dataclasses all
   carry `incident_id` (activities.IncidentIn and the rest); spans keep it, metric labels never
   do (Artifact 12.1: labels stay bounded).

Worker-side only: this module imports temporalio, which aisre.api must never reach.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from opentelemetry import trace
from temporalio import activity
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    Interceptor,
)

from aisre.reliability import telemetry


def incident_of(args: Any) -> str | None:
    for a in args or ():
        found = getattr(a, "incident_id", None)
        if found:
            return str(found)
    return None


class _TimedActivity(ActivityInboundInterceptor):
    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        step = activity.info().activity_type
        span = trace.get_current_span()
        span.set_attribute("aisre.step", step)
        if incident := incident_of(input.args):
            span.set_attribute("aisre.incident_id", incident)
        outcome = "error"
        started = time.monotonic()
        try:
            result = await super().execute_activity(input)
            outcome = "ok"
            return result
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            span.set_attribute("aisre.outcome", outcome)
            telemetry.step_duration.labels(step, outcome).observe(time.monotonic() - started)


class StepTiming(Interceptor):
    def intercept_activity(self, next: ActivityInboundInterceptor) -> ActivityInboundInterceptor:
        return _TimedActivity(next)


def interceptors() -> list[Interceptor]:
    return [TracingInterceptor(), StepTiming()]
