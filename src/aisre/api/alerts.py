"""Artifact 13.8 (M0 criteria): what `POST /v1/incidents` accepts.

The body is an `IncidentCreate`. The reference incident's `alert.json` wraps one: it holds the
monitoring system's own alert beside the `incident_create` the intake reads. The M0 demo
posts that file as it is, so an envelope of exactly those keys is unwrapped here. The whole
document is still stored as the alert's raw payload. Mapping other monitoring formats is
full intake (FR-01), at M1.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aisre.contracts import IncidentCreate

ENVELOPE_KEYS = frozenset({"synthetic", "monitoring_alert", "incident_create"})


def to_incident_create(payload: Mapping[str, Any]) -> IncidentCreate:
    """`IncidentCreate` from the request body. Raises pydantic.ValidationError."""
    if "incident_create" in payload and set(payload) <= ENVELOPE_KEYS:
        return IncidentCreate.model_validate(payload["incident_create"])
    return IncidentCreate.model_validate(payload)
