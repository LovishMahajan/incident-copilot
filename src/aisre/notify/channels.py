"""Artifact 6 component 15 (T1.19): how the Notifier reaches people. Pluggable.

A channel takes one Notification and delivers it, or raises. M1 has only `LogChannel`: one
JSON line on the `aisre.notify` logger, for the incident lead's on-call tooling to pick up
from the worker's log. Paging and chat channels come with alert delivery (T4.6, M4). A
notification carries its key, so a receiver can drop a repeat (Artifact 7.8).
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Protocol

log = logging.getLogger("aisre.notify")


@dataclass(frozen=True)
class Notification:
    key: str  # idempotency key: the receiver drops a repeat
    kind: str  # opened, summary, handed_over, dead_letter, ...
    audience: list[str]  # roles: incident_lead, on_call, approver, security
    incident_id: str | None = None
    dead_letter_id: str | None = None
    detail: str | None = None
    fields: dict[str, str] = field(default_factory=dict)


class Channel(Protocol):
    name: str

    def send(self, n: Notification) -> None: ...


class LogChannel:
    name = "log"

    def send(self, n: Notification) -> None:
        log.warning(json.dumps({"event": "aisre.notification"} | asdict(n), sort_keys=True))
