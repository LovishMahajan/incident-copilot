"""Artifact 12.1, 12.2: the platform's own metrics catalog and tracing setup.

Self-observability only: these describe the AI SRE platform, never customers' applications
(which the platform reads, read-only, from their own monitoring). Labels are bounded: no
incident IDs, user IDs or free text in metric labels — those go in traces.
"""

from __future__ import annotations

import re
import threading
from typing import Iterable, Optional, Sequence

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

REGISTRY = CollectorRegistry()
SEC = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 45, 60, 120, 300, 600, 900)
MONTHLY_CEILING_MICROS = 500_000_000                # ADR-56, provisional: the same values as gateway.py (tested)
DAILY_LIMIT_MICROS = MONTHLY_CEILING_MICROS // 30

# ---------------------------------------------------------------------------
# Intake and API
# ---------------------------------------------------------------------------
http_requests = Counter("aisre_http_requests_total", "API requests", ["route", "method", "status_class"], registry=REGISTRY)
http_latency = Histogram("aisre_http_request_seconds", "API request duration", ["route", "method"],
                         buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.3, 0.5, 1, 2.5, 5), registry=REGISTRY)
incidents_accepted = Counter("aisre_incidents_accepted_total", "Incidents accepted", ["source", "deduplicated"], registry=REGISTRY)
process_ready = Gauge("aisre_process_ready", "1 if the process's /readyz checks pass", ["process"], registry=REGISTRY)
dependency_up = Gauge("aisre_dependency_up", "1 if the last 15 s probe succeeded (postgresql, temporal, object_store, release_process)",
                      ["dependency"], registry=REGISTRY)

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
event_available = Histogram("aisre_event_available_seconds", "Outbox row written to event available to workers", buckets=SEC, registry=REGISTRY)
time_to_first_notification = Histogram("aisre_time_to_first_notification_seconds", "Alert time to first message from the Notifier",
                                       buckets=SEC, registry=REGISTRY)
worker_resume = Histogram("aisre_worker_resume_seconds", "Worker lost to work resumed on another worker", buckets=SEC, registry=REGISTRY)
cancellation = Histogram("aisre_cancellation_seconds", "Cancel request to all work stopped", buckets=SEC, registry=REGISTRY)
incident_outcomes = Counter("aisre_incident_outcomes_total", "Incidents ended", ["outcome"], registry=REGISTRY)  # established|inconclusive|handed_over|cancelled

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
budget_monthly_spent = Gauge("aisre_budget_monthly_spent_micros", "Spend so far this month", registry=REGISTRY)
budget_monthly_limit = Gauge("aisre_budget_monthly_limit_micros", "Monthly spend ceiling", registry=REGISTRY)
budget_daily_limit.set(DAILY_LIMIT_MICROS)
budget_monthly_limit.set(MONTHLY_CEILING_MICROS)
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
approval_decisions = Counter("aisre_approval_decisions_total", "Approval outcomes", ["decision"], registry=REGISTRY)  # approved|rejected|revoked|expired
approval_refused = Counter("aisre_approval_refused_total", "Failed approval attempts (ADR-49)", ["reason"], registry=REGISTRY)
sandbox_runs = Counter("aisre_sandbox_runs_total", "Sandbox runs", ["outcome"], registry=REGISTRY)
sandboxes_leftover = Gauge("aisre_sandboxes_leftover", "Sandbox containers older than 60 minutes", registry=REGISTRY)
dispatch_outcomes = Counter("aisre_dispatch_outcomes_total", "Dispatcher results", ["outcome"], registry=REGISTRY)    # applied|not_applied|refused|unknown
actions_unknown = Gauge("aisre_actions_outcome_unknown", "Actions whose outcome is still unknown", registry=REGISTRY)
verification_outcomes = Counter("aisre_verification_outcomes_total", "Verification verdicts", ["verdict", "action_kind", "category"], registry=REGISTRY)
rollbacks = Counter("aisre_rollbacks_total", "Rollback actions dispatched", ["outcome"], registry=REGISTRY)
production_writes_fenced = Gauge("aisre_dispatch_fenced", "1 if production writes are currently disabled", ["reason"], registry=REGISTRY)
clock_drift = Gauge("aisre_clock_drift_seconds", "Absolute clock drift on the dispatcher host", registry=REGISTRY)
audit_chain_faults = Gauge("aisre_audit_chain_faults", "Incidents whose timeline failed verify_timeline_chain at the last check", registry=REGISTRY)

# ---------------------------------------------------------------------------
# Telemetry itself (Q12.20)
# ---------------------------------------------------------------------------
telemetry_dropped = Counter("aisre_telemetry_dropped_total", "Spans or logs dropped because the exporter failed or the export queue was full", ["kind"], registry=REGISTRY)

ALLOWED_LABEL_VALUES = {"status_class": {"2xx", "3xx", "4xx", "5xx"}, "direction": {"input", "output", "cache_read", "cache_write"}}


# ---------------------------------------------------------------------------
# Redaction (Q12.18, Q12.19)
# ---------------------------------------------------------------------------
SECRET_PATTERNS = [
    re.compile(r"""(?i)(?:bearer|token|api[_-]?key|access[_-]?key|private[_-]?key|secret|password|passwd|pwd)"""
               r"""(?:[_-](?:access[_-])?(?:key|value))?["']?\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s"',;]+)"""),
    re.compile(r"""(?i)\bauthorization["']?\s*[:=]\s*["']?(?:bearer|basic|digest|token)\s+[^\s"',;]+"""),
    re.compile(r"(?i)\bbearer\s+(?=[\w.~+/-]*\d)[\w.~+/-]{16,}=*"),      # needs a digit: "bearer authentication" is prose
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+"),                          # a JWT
    re.compile(r"(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)"),   # with its body
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),                       # e-mail addresses
    re.compile(r"\b(?:\d[ -]?){13,19}\b"),                              # card-like digit runs
    re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"),                         # IPv4 addresses
    re.compile(r"(?<![\w:.])(?:(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}"     # IPv6 addresses: in full, or with ::
               r"|(?:[0-9A-Fa-f]{1,4}:){1,7}:"
               r"|(?:[0-9A-Fa-f]{1,4}:){1,6}:[0-9A-Fa-f]{1,4}"
               r"|(?:[0-9A-Fa-f]{1,4}:){1,5}(?::[0-9A-Fa-f]{1,4}){2}"
               r"|(?:[0-9A-Fa-f]{1,4}:){1,4}(?::[0-9A-Fa-f]{1,4}){3}"
               r"|(?:[0-9A-Fa-f]{1,4}:){1,3}(?::[0-9A-Fa-f]{1,4}){4}"
               r"|(?:[0-9A-Fa-f]{1,4}:){1,2}(?::[0-9A-Fa-f]{1,4}){5}"
               r"|[0-9A-Fa-f]{1,4}:(?::[0-9A-Fa-f]{1,4}){6}"
               r"|:(?::[0-9A-Fa-f]{1,4}){1,7})(?![\w:])"),
]

# Attributes that may be exported. Everything else is dropped from spans before export.
EXPORTABLE_ATTRIBUTES = {
    "aisre.incident_id", "aisre.workflow_id", "aisre.step", "aisre.task", "aisre.action_id",
    "aisre.service", "aisre.environment", "aisre.outcome", "aisre.attempt", "aisre.tool",
    "aisre.model.provider", "aisre.model.id", "aisre.model.prompt_version",
    "aisre.tokens.input", "aisre.tokens.output", "aisre.cost_micros", "aisre.evidence_count",
    "http.route", "http.request.method", "http.response.status_code", "error.type",
    "db.system", "db.operation.name", "aisre.provider_request_id", "aisre.request_id",
}
NEVER_EXPORT = ("prompt", "completion", "message", "content", "body", "query.text", "log", "evidence.summary")
EXACT_ALLOWED = {"aisre.model.prompt_version"}      # a version label, not prompt text: skips the NEVER_EXPORT check


def redact_text(s: str) -> str:
    for p in SECRET_PATTERNS:
        s = p.sub("[REDACTED]", s)
    return s


def filter_attributes(attrs: dict) -> dict:
    """Allow-list, then redact. Raw prompts, model output and log text are never exported (Q12.19)."""
    out = {}
    for k, v in attrs.items():
        if k not in EXPORTABLE_ATTRIBUTES or (k not in EXACT_ALLOWED and any(n in k for n in NEVER_EXPORT)):
            continue
        out[k] = redact_text(v) if isinstance(v, str) else v
    return out


# ---------------------------------------------------------------------------
# Tracing setup (Q12.12–Q12.15, Q12.20)
# ---------------------------------------------------------------------------
MAX_QUEUED_SPANS = 2048


def build_tracer_provider(service_name: str, exporter=None):
    """OpenTelemetry tracer provider with a redacting processor in front of a *batch* exporter.
    The queue is bounded and refuses spans when full, so an unavailable collector never blocks
    or fails platform work. Refused spans and failed exports both count in telemetry_dropped
    (Q12.20)."""
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult

    slots = threading.Semaphore(MAX_QUEUED_SPANS)      # one per span queued or being exported

    class CountingExporter(SpanExporter):
        def __init__(self, inner):
            self.inner = inner

        def export(self, spans):
            try:
                result = self.inner.export(spans)
            except Exception:
                result = SpanExportResult.FAILURE
            finally:
                slots.release(len(spans))
            if result != SpanExportResult.SUCCESS:
                telemetry_dropped.labels("span").inc(len(spans))
            return result

        def shutdown(self):
            self.inner.shutdown()

        def force_flush(self, timeout_millis=30000):
            return self.inner.force_flush(timeout_millis)

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
            if span.context.trace_flags.sampled and not slots.acquire(blocking=False):
                telemetry_dropped.labels("span").inc()         # queue full: refused, never waited for
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
            CountingExporter(exporter), max_queue_size=MAX_QUEUED_SPANS, max_export_batch_size=256, export_timeout_millis=5000)))
    return provider
