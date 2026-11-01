"""Artifact 12.1, 12.2: the platform's own metrics catalog and tracing setup.

Self-observability only: these describe the AI SRE platform, never customers' applications
(which the platform reads, read-only, from their own monitoring). Labels are bounded: no
incident IDs, user IDs or free text in metric labels — those go in traces.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional, Sequence

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

REGISTRY = CollectorRegistry()
SEC = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600, 900)

# ---------------------------------------------------------------------------
# Intake and API
# ---------------------------------------------------------------------------
http_requests = Counter("aisre_http_requests_total", "API requests", ["route", "method", "status_class"], registry=REGISTRY)
http_latency = Histogram("aisre_http_request_seconds", "API request duration", ["route", "method"],
                         buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.3, 0.5, 1, 2.5, 5), registry=REGISTRY)
incidents_accepted = Counter("aisre_incidents_accepted_total", "Incidents accepted", ["source", "deduplicated"], registry=REGISTRY)

# ---------------------------------------------------------------------------
# Queue, workflow and outbox (Q12.4, Q12.5)
# ---------------------------------------------------------------------------
queue_depth = Gauge("aisre_incident_queue_depth", "Incidents waiting for a worker", ["severity"], registry=REGISTRY)
queue_oldest_age = Gauge("aisre_incident_queue_oldest_age_seconds", "Age of the oldest waiting incident", ["severity"], registry=REGISTRY)
queue_wait = Histogram("aisre_incident_queue_wait_seconds", "Acceptance to first worker step", ["severity"], buckets=SEC, registry=REGISTRY)
outbox_pending = Gauge("aisre_outbox_pending", "Undelivered outbox rows", registry=REGISTRY)
outbox_oldest_age = Gauge("aisre_outbox_oldest_age_seconds", "Age of the oldest undelivered outbox row", registry=REGISTRY)
workflows_active = Gauge("aisre_workflows_active", "Running incident workflows", ["state"], registry=REGISTRY)
workflow_last_progress_age = Gauge("aisre_workflow_max_seconds_since_progress",
                                   "Longest time any active workflow has gone without a recorded transition, excluding waits for people",
                                   registry=REGISTRY)
workflows_stalled = Gauge("aisre_workflows_stalled", "Workflows with no progress beyond their state's limit", ["state"], registry=REGISTRY)
step_duration = Histogram("aisre_step_seconds", "Workflow step duration", ["step", "outcome"], buckets=SEC, registry=REGISTRY)
time_to_first_summary = Histogram("aisre_time_to_first_summary_seconds", "Alert time to first summary", buckets=SEC, registry=REGISTRY)

# ---------------------------------------------------------------------------
# Model gateway (Q12.6–Q12.8)
# ---------------------------------------------------------------------------
model_calls = Counter("aisre_model_calls_total", "Model attempts", ["provider", "model", "task", "status"], registry=REGISTRY)
model_latency = Histogram("aisre_model_call_seconds", "Model call latency", ["provider", "model", "task"], buckets=SEC, registry=REGISTRY)
model_tokens = Counter("aisre_model_tokens_total", "Tokens", ["provider", "model", "direction"], registry=REGISTRY)
model_cost = Counter("aisre_model_cost_micros_total", "Settled model cost (millionths of currency)", ["provider", "model", "task"], registry=REGISTRY)
budget_reserved = Gauge("aisre_budget_reserved_micros", "Currently reserved, unsettled model spend (all incidents)", registry=REGISTRY)
budget_daily_spent = Gauge("aisre_budget_daily_spent_micros", "Spend so far today", registry=REGISTRY)
budget_daily_limit = Gauge("aisre_budget_daily_limit_micros", "Daily spend ceiling", registry=REGISTRY)
budget_pending_reconcile = Gauge("aisre_budget_pending_reconcile", "Reservations charged without reported usage", registry=REGISTRY)
circuit_state = Gauge("aisre_circuit_open", "1 if the circuit breaker for a dependency is open", ["dependency"], registry=REGISTRY)

# ---------------------------------------------------------------------------
# Evidence and retrieval
# ---------------------------------------------------------------------------
tool_calls = Counter("aisre_tool_calls_total", "Evidence tool calls", ["tool", "outcome"], registry=REGISTRY)
tool_refusals = Counter("aisre_tool_refusals_total", "ToolGate refusals", ["code"], registry=REGISTRY)
evidence_gaps = Counter("aisre_evidence_gaps_total", "Sources recorded as gaps", ["source"], registry=REGISTRY)
injection_flags = Counter("aisre_injection_flags_total", "Evidence flagged as instruction-like", ["source"], registry=REGISTRY)

# ---------------------------------------------------------------------------
# Remediation (Q12.9–Q12.11)
# ---------------------------------------------------------------------------
approvals_pending = Gauge("aisre_approvals_pending", "Open approval requests", registry=REGISTRY)
approval_oldest_age = Gauge("aisre_approval_oldest_age_seconds", "Age of the oldest open approval request", registry=REGISTRY)
approval_decisions = Counter("aisre_approval_decisions_total", "Approval outcomes", ["decision"], registry=REGISTRY)  # approve|reject|revoke|expired
sandbox_runs = Counter("aisre_sandbox_runs_total", "Sandbox runs", ["outcome"], registry=REGISTRY)
sandboxes_leftover = Gauge("aisre_sandboxes_leftover", "Sandbox containers older than 60 minutes", registry=REGISTRY)
dispatch_outcomes = Counter("aisre_dispatch_outcomes_total", "Dispatcher results", ["outcome"], registry=REGISTRY)    # applied|not_applied|refused|unknown
actions_unknown = Gauge("aisre_actions_outcome_unknown", "Actions whose outcome is still unknown", registry=REGISTRY)
verification_outcomes = Counter("aisre_verification_outcomes_total", "Verification verdicts", ["verdict"], registry=REGISTRY)
rollbacks = Counter("aisre_rollbacks_total", "Rollback actions dispatched", ["outcome"], registry=REGISTRY)
production_writes_fenced = Gauge("aisre_dispatch_fenced", "1 if production writes are currently disabled", ["reason"], registry=REGISTRY)

# ---------------------------------------------------------------------------
# Telemetry itself (Q12.20)
# ---------------------------------------------------------------------------
telemetry_dropped = Counter("aisre_telemetry_dropped_total", "Spans or logs dropped because the exporter was unavailable", ["kind"], registry=REGISTRY)

ALLOWED_LABEL_VALUES = {"status_class": {"2xx", "3xx", "4xx", "5xx"}, "direction": {"input", "output", "cache_read", "cache_write"}}


# ---------------------------------------------------------------------------
# Redaction (Q12.18, Q12.19)
# ---------------------------------------------------------------------------
SECRET_PATTERNS = [
    re.compile(r"(?i)\b(bearer|token|api[_-]?key|secret|password|passwd)\b\s*[:=]\s*\S+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),                       # e-mail addresses
    re.compile(r"\b(?:\d[ -]?){13,19}\b"),                              # card-like digit runs
    re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"),                         # IPv4 addresses
]

# Attributes that may be exported. Everything else is dropped from spans before export.
EXPORTABLE_ATTRIBUTES = {
    "aisre.incident_id", "aisre.workflow_id", "aisre.step", "aisre.task", "aisre.action_id",
    "aisre.service", "aisre.environment", "aisre.outcome", "aisre.attempt", "aisre.tool",
    "aisre.model.provider", "aisre.model.id", "aisre.model.prompt_version",
    "aisre.tokens.input", "aisre.tokens.output", "aisre.cost_micros", "aisre.evidence_count",
    "http.route", "http.request.method", "http.response.status_code", "error.type",
    "db.system", "db.operation.name",
}
NEVER_EXPORT = ("prompt", "completion", "message", "content", "body", "query.text", "log", "evidence.summary")


def redact_text(s: str) -> str:
    for p in SECRET_PATTERNS:
        s = p.sub("[REDACTED]", s)
    return s


def filter_attributes(attrs: dict) -> dict:
    """Allow-list, then redact. Raw prompts, model output and log text are never exported (Q12.19)."""
    out = {}
    for k, v in attrs.items():
        if k not in EXPORTABLE_ATTRIBUTES or any(n in k for n in NEVER_EXPORT):
            continue
        out[k] = redact_text(v) if isinstance(v, str) else v
    return out


# ---------------------------------------------------------------------------
# Tracing setup (Q12.12–Q12.15, Q12.20)
# ---------------------------------------------------------------------------
def build_tracer_provider(service_name: str, exporter=None):
    """OpenTelemetry tracer provider with a redacting processor in front of a *batch* exporter.
    The batch processor has a bounded queue and drops spans when full, so an unavailable
    collector never blocks or fails platform work (Q12.20)."""
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    class RedactingProcessor(SpanProcessor):
        def __init__(self, inner):
            self.inner = inner

        def on_start(self, span, parent_context=None):
            self.inner.on_start(span, parent_context)

        def on_end(self, span):
            try:
                span._attributes = filter_attributes(dict(span.attributes or {}))   # noqa: SLF001 (SDK span is mutable here)
                span._events = []                                                    # events may carry raw text
            except Exception:
                telemetry_dropped.labels("span").inc()
                return
            self.inner.on_end(span)

        def shutdown(self):
            self.inner.shutdown()

        def force_flush(self, timeout_millis=30000):
            return self.inner.force_flush(timeout_millis)

    provider = TracerProvider(resource=Resource.create({"service.name": service_name,
                                                        "service.namespace": "aisre"}))
    if exporter is not None:
        provider.add_span_processor(RedactingProcessor(BatchSpanProcessor(
            exporter, max_queue_size=2048, max_export_batch_size=256, export_timeout_millis=5000)))
    return provider
