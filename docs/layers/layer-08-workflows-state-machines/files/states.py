"""Artifact 8.4: the incident and action state machines, as data.

One table of allowed transitions, shared by the workflow (which decides) and by the
Incident Record (which refuses any write that isn't in the table). Pure Python, no I/O,
so it is safe to import inside Temporal workflow code.
"""

from __future__ import annotations

from enum import Enum


class IncidentState(str, Enum):
    OPEN = "open"                         # accepted, workflow not yet running
    INVESTIGATING = "investigating"       # evidence, retrieval, diagnosis, review
    DIAGNOSED = "diagnosed"               # reviewed diagnosis, established
    INCONCLUSIVE = "inconclusive"         # root cause not established
    TESTING = "testing"                   # patch being tested in the isolated environment
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"               # trusted dispatcher applying the change
    RECONCILING = "reconciling"           # outcome unknown; establishing the real state
    VERIFYING = "verifying"               # recovery being checked over the hold period
    RECOVERED = "recovered"
    NOT_RECOVERED = "not_recovered"       # fix did not hold; undo in progress
    HANDED_OVER = "handed_over"           # a person owns the next step
    CANCELLED = "cancelled"
    CLOSED = "closed"


class ActionState(str, Enum):
    PROPOSED = "proposed"
    TESTING = "testing"
    TEST_FAILED = "test_failed"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    APPLYING = "applying"
    OUTCOME_UNKNOWN = "outcome_unknown"
    EXECUTED = "executed"                 # the release process confirms it was applied
    NOT_APPLIED = "not_applied"           # confirmed never applied
    VERIFIED = "verified"
    UNDONE = "undone"
    WITHDRAWN = "withdrawn"


I = IncidentState
A = ActionState

TERMINAL_INCIDENT = {I.CLOSED, I.CANCELLED}

# States in which a production change may be in flight. Cancellation never
# interrupts these; it takes effect once the outcome is known.
PROTECTED_INCIDENT = {I.EXECUTING, I.RECONCILING, I.NOT_RECOVERED}

INCIDENT_TRANSITIONS: dict[IncidentState, set[IncidentState]] = {
    I.OPEN: {I.INVESTIGATING, I.CANCELLED},
    I.INVESTIGATING: {I.DIAGNOSED, I.INCONCLUSIVE, I.HANDED_OVER, I.CANCELLED},
    I.DIAGNOSED: {I.TESTING, I.HANDED_OVER, I.CANCELLED},
    I.INCONCLUSIVE: {I.HANDED_OVER, I.CANCELLED},
    I.TESTING: {I.AWAITING_APPROVAL, I.HANDED_OVER, I.CANCELLED},
    I.AWAITING_APPROVAL: {I.EXECUTING, I.HANDED_OVER, I.CANCELLED},
    I.EXECUTING: {I.VERIFYING, I.RECONCILING, I.HANDED_OVER},
    I.RECONCILING: {I.VERIFYING, I.HANDED_OVER},
    I.VERIFYING: {I.RECOVERED, I.NOT_RECOVERED, I.HANDED_OVER},
    I.RECOVERED: {I.CLOSED},
    I.NOT_RECOVERED: {I.HANDED_OVER},
    I.HANDED_OVER: {I.CLOSED},
    I.CANCELLED: set(),
    I.CLOSED: set(),
}

ACTION_TRANSITIONS: dict[ActionState, set[ActionState]] = {
    A.PROPOSED: {A.TESTING, A.WITHDRAWN},
    A.TESTING: {A.AWAITING_APPROVAL, A.TEST_FAILED, A.WITHDRAWN},
    A.TEST_FAILED: set(),
    A.AWAITING_APPROVAL: {A.APPROVED, A.REJECTED, A.EXPIRED, A.WITHDRAWN},
    A.APPROVED: {A.APPLYING, A.EXPIRED, A.WITHDRAWN},
    A.REJECTED: set(),
    A.EXPIRED: set(),
    A.APPLYING: {A.EXECUTED, A.NOT_APPLIED, A.OUTCOME_UNKNOWN},
    A.OUTCOME_UNKNOWN: {A.EXECUTED, A.NOT_APPLIED},  # only reconciliation leaves this state
    A.EXECUTED: {A.VERIFIED, A.UNDONE},
    A.NOT_APPLIED: set(),
    A.VERIFIED: set(),
    A.UNDONE: set(),
    A.WITHDRAWN: set(),
}


class InvalidTransition(Exception):
    """Raised when a transition is not in the table. Never caught to 'try anyway'."""


def check_incident(current: IncidentState, target: IncidentState) -> None:
    if target not in INCIDENT_TRANSITIONS[current]:
        raise InvalidTransition(f"incident: {current.value} -> {target.value} is not allowed")


def check_action(current: ActionState, target: ActionState) -> None:
    if target not in ACTION_TRANSITIONS[current]:
        raise InvalidTransition(f"action: {current.value} -> {target.value} is not allowed")
