"""Artifact 7.7 (T1.6): a signal to a run that has ended is delivered, not retried for ever."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from temporalio.service import RPCError, RPCStatusCode

from aisre.record import SIGNAL_WORKFLOW
from aisre.workflow.relay import Message, Relay


class Handle:
    def __init__(self, status: RPCStatusCode) -> None:
        self.status = status

    async def signal(self, name: str, arg: Any) -> None:
        raise RPCError("workflow execution already completed", self.status, b"")


class Client:
    def __init__(self, status: RPCStatusCode) -> None:
        self.status = status

    def get_workflow_handle(self, workflow_id: str) -> Handle:
        return Handle(self.status)


def message() -> Message:
    arg = {"request_id": "r-1", "kind": "close", "by": "user:lena"}
    return Message(
        1, uuid4(), SIGNAL_WORKFLOW, "incident-x", {"signal": "lead_decision", "arg": arg}, 0
    )


@pytest.mark.asyncio
async def test_a_signal_to_an_ended_run_counts_as_delivered():
    relay = Relay(engine=None, client=Client(RPCStatusCode.NOT_FOUND))  # type: ignore[arg-type]
    await relay.deliver(message())


@pytest.mark.asyncio
async def test_any_other_signal_failure_is_retried():
    relay = Relay(engine=None, client=Client(RPCStatusCode.UNAVAILABLE))  # type: ignore[arg-type]
    with pytest.raises(RPCError):
        await relay.deliver(message())
