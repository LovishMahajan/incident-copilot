"""Artifact 5 (Q5.28, ST-19 to ST-22), Artifact 12.1: the API's security events, as log lines.

Failed sign-ins, permission denials, reads of incident content and rejected alerts are each
one structured line on the `aisre.audit` logger: a JSON object with `event`, `time`, the
request's ID, method and path, and the event's own fields. Never a token, a header or a body.

    aisre.auth.failed           reason (aisre.api.tokens' codes, or missing_token, ...)
    aisre.authz.denied          caller, permission, service, incident_id
    aisre.content.read          caller, incident_id, content
    aisre.intake.dead_lettered  caller, dead_letter_id, reason

They go to standard error with the process's other logs, which the platform keeps 30 days
(ADR-57). The full audit export (`audit:export`, ADR-50) reads them back once the write-once
audit copy exists (T1.20); until then they are log lines only.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from fastapi import Request

logger = logging.getLogger("aisre.audit")

WARN_EVENTS = frozenset({"aisre.auth.failed", "aisre.authz.denied", "aisre.intake.dead_lettered"})


def install_handler() -> None:
    """One line per event on standard error, once per process. Propagates too, so tests
    and any root handler see the same records."""
    logger.setLevel(logging.INFO)
    if not logger.handlers:
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
