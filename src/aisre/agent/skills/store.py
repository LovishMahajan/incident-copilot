"""Artifact 7.2, 13.5 T1.15: the skills' SkillStore over PostgreSQL (aisre.record.diagnoses).

Bound to the worker's engine (`aisre_app`, ADR-51). The record is synchronous, so each call runs
in a thread, one transaction each. A database error raises ConnectionError, an outage that the
skill step's retry policy waits out; the gateway's result reuse keeps the retry from calling the
provider again (ADR-61).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Optional, TypeVar
from uuid import UUID

from sqlalchemy import Connection, Engine
from sqlalchemy.exc import DBAPIError

from aisre.agent.agent_contracts import (
    Confidence,
    DiagnosisDraft,
    ReviewResult,
    SkillContext,
)
from aisre.agent.skills.reasoning import Inputs
from aisre.agent.validation import missing_required_sources
from aisre.record import diagnoses as rec

R = TypeVar("R")


class PgSkillStore:
    def __init__(
        self, engine: Engine, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self.engine, self.clock = engine, clock

    async def _tx(self, fn: Callable[[Connection], R]) -> R:
        def run() -> R:
            try:
                with self.engine.begin() as conn:
                    return fn(conn)
            except DBAPIError as e:
                raise ConnectionError(f"database: {type(e.orig).__name__}") from e

        return await asyncio.to_thread(run)

    async def inputs(self, incident_id: str, evidence_ids: Sequence[str]) -> Optional[Inputs]:
        found = await self._tx(lambda c: rec.skill_inputs(c, UUID(incident_id), evidence_ids))
        if found is None:
            return None
        scope, records, facts = found
        context = SkillContext(
            incident_id=scope.incident_id,
            service=scope.service,
            environment=scope.environment,
            started_at=scope.started_at,
        )
        return Inputs(context, records, facts)

    async def save_diagnosis(
        self,
        diagnosis_id: UUID,
        inputs: Inputs,
        draft: DiagnosisDraft,
        confidence: Confidence,
        hypothesis_confidence: list[Confidence],
        produced_by: str,
    ) -> None:
        now = self.clock()
        await self._tx(
            lambda c: rec.store_diagnosis(
                c,
                diagnosis_id=diagnosis_id,
                incident_id=inputs.context.incident_id,
                draft=draft,
                confidence=confidence,
                hypothesis_confidence=hypothesis_confidence,
                produced_by=produced_by,
                now=now,
                missing_sources=missing_required_sources(inputs.evidence),
            )
        )

    async def save_review(
        self, diagnosis_id: UUID, inputs: Inputs, draft: DiagnosisDraft, review: ReviewResult
    ) -> None:
        now = self.clock()
        notes = rec.review_notes(review.verdict) if review.verdict is not None else None
        await self._tx(
            lambda c: rec.record_review(
                c,
                diagnosis_id=diagnosis_id,
                incident_id=inputs.context.incident_id,
                fields=review.stored_fields(draft),
                notes=notes,
                now=now,
            )
        )
