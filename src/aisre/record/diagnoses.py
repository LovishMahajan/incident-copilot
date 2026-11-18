"""Artifact 7.2, 9.7, 9.8, 13.5 T1.15 (FR-12, ADR-69): diagnoses and their hypotheses, as the
skills store them, and what a skill reads to start.

`store_diagnosis` writes one diagnosis revision in one transaction: each of its hypotheses as a
new `hypotheses` row (ordinals run on across revisions), each cited record as a
`hypothesis_evidence` link (`supports` from the claims, `contradicts` from the contradicting IDs),
then the `diagnoses` row from DiagnosisDraft.stored_fields(), and the timeline events
(`aisre.hypothesis.created.v1` each, then `aisre.diagnosis.produced.v1` or `.inconclusive.v1`).
Idempotent on the diagnosis ID: a retried step finds its row and writes nothing.

`confidence` is the level code scored from the cited observations (ADR-53), never the model's.
Low is never established (schema.sql), so a draft the model calls established that scores low is
stored as inconclusive, with the reason in `stop_reason`; its review can only confirm that.

`record_review` writes the review's part once (ADR-69): `reviewed`, `final_outcome`,
`final_confidence` from ReviewResult.stored_fields, and the issues as `review_notes`, with
`aisre.diagnosis.reviewed.v1`. Invalid critic output stores nothing.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import Connection, select, text

from aisre.agent.agent_contracts import (
    Confidence,
    CriticVerdict,
    DiagnosisDraft,
    Evidence,
)
from aisre.contracts import Confidence as RecordConfidence
from aisre.contracts import (
    DiagnosisInconclusiveData,
    DiagnosisProducedData,
    DiagnosisReviewedData,
    HypothesisCategory,
    HypothesisCreatedData,
)
from aisre.record.evidence import IncidentScope, evidence, incident_scope, load_evidence
from aisre.record.incidents import append_event
from aisre.record.tables import incidents

ACTOR = "system:diagnosis-engine"
SOURCE = "/aisre/worker/diagnosis-engine"
LOW_NOT_ESTABLISHED = "scored low from the cited observations: not established (ADR-53)"


def skill_inputs(
    conn: Connection, incident_id: UUID, ids: Sequence[str]
) -> tuple[IncidentScope, list[Evidence], dict[UUID, list[str]]] | None:
    """The incident, its records among `ids` and each record's facts (`attributes.facts`)."""
    scope = incident_scope(conn, incident_id)
    if scope is None:
        return None
    records = load_evidence(conn, incident_id, ids)
    e = evidence
    rows = conn.execute(
        select(e.c.evidence_id, e.c.attributes["facts"]).where(
            e.c.incident_id == incident_id, e.c.evidence_id.in_([r.evidence_id for r in records])
        )
    ).all()
    facts = {row[0]: list(row[1] or []) for row in rows}
    return scope, records, facts


def hypothesis_id(diagnosis_id: UUID, index: int) -> UUID:
    return uuid5(diagnosis_id, f"hypothesis:{index}")


def _lock(conn: Connection, incident_id: UUID) -> None:
    conn.execute(
        select(incidents.c.incident_id)
        .where(incidents.c.incident_id == incident_id)
        .with_for_update()
    )


def store_diagnosis(
    conn: Connection,
    *,
    diagnosis_id: UUID,
    incident_id: UUID,
    draft: DiagnosisDraft,
    confidence: Confidence,
    hypothesis_confidence: Sequence[Confidence],
    produced_by: str,
    now: datetime,
) -> int:
    """Stores one revision; returns its number (the existing one on a repeat)."""
    _lock(conn, incident_id)
    found = conn.execute(
        text("SELECT revision FROM diagnoses WHERE diagnosis_id = :d"), {"d": diagnosis_id}
    ).scalar()
    if found is not None:
        return int(found)
    revision = 1 + int(
        conn.execute(
            text("SELECT coalesce(max(revision), 0) FROM diagnoses WHERE incident_id = :i"),
            {"i": incident_id},
        ).scalar_one()
    )
    first = int(
        conn.execute(
            text("SELECT coalesce(max(ordinal), 0) FROM hypotheses WHERE incident_id = :i"),
            {"i": incident_id},
        ).scalar_one()
    )
    established = draft.outcome == "established" and confidence != Confidence.low
    lead = draft.leading_index if established else None
    for i, h in enumerate(draft.hypotheses):
        hid = hypothesis_id(diagnosis_id, i)
        status = "open" if lead is None else ("supported" if i == lead else "ruled_out")
        conn.execute(
            text(
                "INSERT INTO hypotheses (hypothesis_id, incident_id, ordinal, statement, category, "
                "claims, confirm_test, refute_test, status, confidence, status_reason) VALUES "
                "(:h, :i, :o, :s, CAST(:c AS hypothesis_category), CAST(:claims AS jsonb), :ct, "
                ":rt, CAST(:st AS hypothesis_status), CAST(:conf AS confidence), :why)"
            ),
            {
                "h": hid,
                "i": incident_id,
                "o": first + i + 1,
                "s": h.statement,
                "c": h.category,
                "claims": json.dumps([c.model_dump(mode="json") for c in h.claims]),
                "ct": h.confirm_test,
                "rt": h.refute_test,
                "st": status,
                "conf": hypothesis_confidence[i].value,
                "why": f"diagnosis revision {revision}",
            },
        )
        links = {(e, "supports") for c in h.claims for e in c.evidence_ids}
        links |= {(e, "contradicts") for e in h.contradicting_evidence_ids}
        for evidence_id, relation in sorted(links, key=str):
            conn.execute(
                text(
                    "INSERT INTO hypothesis_evidence (hypothesis_id, evidence_id, relation) "
                    "VALUES (:h, :e, CAST(:r AS evidence_relation))"
                ),
                {"h": hid, "e": evidence_id, "r": relation},
            )
        _event(
            conn,
            incident_id,
            "aisre.hypothesis.created.v1",
            HypothesisCreatedData(
                hypothesis_id=hid,
                ordinal=first + i + 1,
                statement=h.statement,
                category=HypothesisCategory(h.category),
                confidence=RecordConfidence(hypothesis_confidence[i].value),
            ),
            now,
        )
    fields: dict[str, Any] = draft.stored_fields()
    stop_reason = None
    if draft.outcome == "established" and not established:
        fields["outcome"], stop_reason = "inconclusive", LOW_NOT_ESTABLISHED
    lead_id = hypothesis_id(diagnosis_id, lead) if lead is not None else None
    conn.execute(
        text(
            "INSERT INTO diagnoses (diagnosis_id, incident_id, revision, outcome, "
            "leading_hypothesis_id, category, confidence, summary, claims, ruled_out, "
            "missing_evidence, stop_reason, produced_by) VALUES (:d, :i, :rev, "
            "CAST(:outcome AS diagnosis_outcome), :lead, CAST(:category AS hypothesis_category), "
            "CAST(:conf AS confidence), :summary, CAST(:claims AS jsonb), :ruled_out, :missing, "
            ":stop, :by)"
        ),
        {
            "d": diagnosis_id,
            "i": incident_id,
            "rev": revision,
            "outcome": fields["outcome"],
            "lead": lead_id,
            "category": fields["category"],
            "conf": confidence.value,
            "summary": fields["summary"],
            "claims": json.dumps(fields["claims"]),
            "ruled_out": fields["ruled_out"],
            "missing": fields["missing_evidence"],
            "stop": stop_reason,
            "by": produced_by,
        },
    )
    if lead_id is not None:
        _event(
            conn,
            incident_id,
            "aisre.diagnosis.produced.v1",
            DiagnosisProducedData(
                diagnosis_id=diagnosis_id,
                revision=revision,
                leading_hypothesis_id=lead_id,
                category=HypothesisCategory(fields["category"]),
                confidence=RecordConfidence(confidence.value),
                produced_by=produced_by,
            ),
            now,
        )
    else:
        _event(
            conn,
            incident_id,
            "aisre.diagnosis.inconclusive.v1",
            DiagnosisInconclusiveData(
                diagnosis_id=diagnosis_id,
                revision=revision,
                stop_reason=stop_reason,
                missing_evidence=list(fields["missing_evidence"]),
                produced_by=produced_by,
            ),
            now,
        )
    return revision


def review_notes(verdict: CriticVerdict) -> str:
    issues = [
        f"{i.kind.value}{' (blocking)' if i.blocking else ''}: {i.detail}" for i in verdict.issues
    ]
    return f"{verdict.verdict}, cap {verdict.confidence_cap.value}" + (
        "; " + "; ".join(issues) if issues else ""
    )


def record_review(
    conn: Connection,
    *,
    diagnosis_id: UUID,
    incident_id: UUID,
    fields: dict[str, Any],
    notes: str | None,
    now: datetime,
) -> bool:
    """Writes the review once. False when there is nothing to write or it was written already."""
    if not fields:
        return False
    _lock(conn, incident_id)
    row = conn.execute(
        text(
            "SELECT revision, reviewed FROM diagnoses WHERE diagnosis_id = :d AND incident_id = :i"
        ),
        {"d": diagnosis_id, "i": incident_id},
    ).first()
    if row is None or row.reviewed:
        return False
    conn.execute(
        text(
            "UPDATE diagnoses SET reviewed = true, review_notes = :n, "
            "final_outcome = CAST(:o AS diagnosis_outcome), final_confidence = CAST(:c AS confidence) "
            "WHERE diagnosis_id = :d"
        ),
        {
            "n": notes,
            "o": fields["final_outcome"],
            "c": fields["final_confidence"],
            "d": diagnosis_id,
        },
    )
    _event(
        conn,
        incident_id,
        "aisre.diagnosis.reviewed.v1",
        DiagnosisReviewedData(
            diagnosis_id=diagnosis_id, revision=int(row.revision), reviewed=True, review_notes=notes
        ),
        now,
    )
    return True


def _event(
    conn: Connection,
    incident_id: UUID,
    event_type: str,
    data: HypothesisCreatedData
    | DiagnosisProducedData
    | DiagnosisInconclusiveData
    | DiagnosisReviewedData,
    now: datetime,
) -> None:
    append_event(conn, incident_id, event_type, ACTOR, data, occurred_at=now, source=SOURCE)
