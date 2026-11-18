"""Artifact 5 (Q5.28, ST-19 to ST-22), Artifact 12.1: the API's security events, as log lines.

Failed sign-ins, permission denials, reads of incident content and rejected alerts are each
one structured line on the `aisre.audit` logger: a JSON object with `event`, `time`, the
request's ID, method and path, and the event's own fields. Never a token, a header or a body.

    aisre.auth.failed           reason (aisre.api.tokens' codes, or missing_token, ...)
    aisre.authz.denied          caller, permission, service, incident_id
    aisre.content.read          caller, incident_id, content
    aisre.intake.dead_lettered  caller, dead_letter_id, reason

They go to standard error with the process's other logs, which the platform keeps 30 days
(ADR-57), and, when a write-once store is configured, to the audit copy (T1.20): `install_copy`
adds a handler that batches the lines and writes them every few seconds, or at 100 lines
(aisre.record.audit_copy). A write that fails keeps its lines for the next try, up to
MAX_PENDING, then drops the oldest and counts them in `aisre_telemetry_dropped_total{kind=
"audit"}`. The full audit export (`audit:export`, ADR-50) reads them back from the copy.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from datetime import UTC, datetime
from typing import Any

from fastapi import Request

from aisre.record.audit_copy import WriteOnceStore, write_log_lines
from aisre.reliability import telemetry

logger = logging.getLogger("aisre.audit")

FLUSH_S = 5.0
FLUSH_LINES = 100
MAX_PENDING = 10_000
WARN_EVENTS = frozenset({"aisre.auth.failed", "aisre.authz.denied", "aisre.intake.dead_lettered"})


def install_handler() -> None:
    """One line per event on standard error, once per process. Propagates too, so tests
    and any root handler see the same records."""
    logger.setLevel(logging.INFO)
    if not any(type(h) is logging.StreamHandler for h in logger.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)


def emit(event: str, request: Request | None, **fields: Any) -> dict[str, Any]:
    """Log one security event and return it."""
    record: dict[str, Any] = {"event": event, "time": datetime.now(UTC).isoformat()}
    if request is not None:
        record |= {
            "request_id": getattr(request.state, "request_id", None),
            "method": request.method,
            "path": request.url.path,
        }
    record |= fields
    level = logging.WARNING if event in WARN_EVENTS else logging.INFO
    logger.log(level, json.dumps(record, default=str, sort_keys=True), extra={"aisre": record})
    return record


class CopyHandler(logging.Handler):
    """Batches each event's fields (`extra={"aisre": ...}`) into the write-once copy."""

    def __init__(self, store: WriteOnceStore, flush_s: float = FLUSH_S) -> None:
        super().__init__(logging.INFO)
        self.store = store
        self.pending: list[dict[str, Any]] = []
        self.guard = threading.Lock()
        self.stopped = threading.Event()
        self.flush_s = flush_s
        threading.Thread(target=self._every, daemon=True, name="audit-copy").start()

    def emit(self, record: logging.LogRecord) -> None:
        fields = getattr(record, "aisre", None)
        if not isinstance(fields, dict):
            return
        with self.guard:
            self.pending.append(fields)
            full = len(self.pending) >= FLUSH_LINES
        if full:
            self.flush()

    def flush(self) -> None:
        with self.guard:
            batch, self.pending = self.pending, []
        if not batch:
            return
        try:
            write_log_lines(self.store, batch)
        except Exception:
            with self.guard:
                self.pending = batch + self.pending
                over = len(self.pending) - MAX_PENDING
                if over > 0:
                    del self.pending[:over]
                    telemetry.telemetry_dropped.labels("audit").inc(over)

    def close(self) -> None:
        self.stopped.set()
        self.flush()
        super().close()

    def _every(self) -> None:
        while not self.stopped.wait(self.flush_s):
            self.flush()


_copy: CopyHandler | None = None


def install_copy(store: WriteOnceStore | None) -> CopyHandler | None:
    """Send audit lines to `store` too, replacing any earlier copy handler; None turns it off."""
    global _copy
    if _copy is not None:
        logger.removeHandler(_copy)
        _copy.close()
        _copy = None
    if store is not None:
        _copy = CopyHandler(store)
        logger.addHandler(_copy)
    return _copy
