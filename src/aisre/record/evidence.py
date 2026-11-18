"""Artifact 7.2, 9.5, 13.5 T1.10: evidence records, their snapshots' artifact rows, and reads.

`store_round` writes one collection round in one transaction, under the incident's tool-call
lock (aisre.record.tool_calls): each tool call is counted and admitted, then the evidence it
produced, its snapshot's `artifacts` row, and one timeline event per record
(`aisre.evidence.collected.v1`, or `aisre.evidence.gap_recorded.v1` for a gap), plus
`aisre.evidence.injection_flagged.v1` for a record the collector flagged (ST-33). A call the
budget refuses at that point is dropped with its evidence, so nothing is stored that the
`tool_calls` log doesn't account for (ADR-69). Evidence is append-only (schema.sql).

The snapshot object itself is written to the object store before this transaction; the row
here points at it by key, which is internal and never returned (Artifact 7). Snapshots are due
for deletion after 30 days (ADR-57).

`list_evidence` is the read the API and approvers use: every column, `flagged_injection`
included. `load_evidence` gives a skill this incident's records as the agent contract.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Connection,
    MetaData,
    RowMapping,
    SmallInteger,
    Table,
    Text,
    insert,
    select,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID as PgUUID

from aisre.agent.agent_contracts import Evidence, EvidenceKind
from aisre.agent.tool_registry import ToolRefused
from aisre.contracts import (
    ArtifactKind,
    EvidenceCollectedData,
    EvidenceRecord,
    GapRecordedData,
    InjectionFlaggedData,
    Strict,
)
from aisre.contracts import EvidenceKind as RecordKind
from aisre.record.incidents import append_event, uuid7
from aisre.record.tables import environments, incident_alerts, incidents, services
from aisre.record.tool_calls import PgToolCalls, lock_incident

COLLECTOR_SOURCE = "/aisre/worker/evidence-collector"
COLLECTOR_ACTOR = "system:evidence-collector"
SNAPSHOT_RETENTION = timedelta(days=30)  # ADR-57: raw snapshots; cited evidence is held longer

Timestamp = TIMESTAMP(timezone=True)
metadata = MetaData()
evidence_kind = ENUM(*(k.value for k in RecordKind), name="evidence_kind", create_type=False)
artifact_kind = ENUM(*(k.value for k in ArtifactKind), name="artifact_kind", create_type=False)

artifacts = Table(
    "artifacts",
    metadata,
    Column("artifact_id", PgUUID, primary_key=True),
    Column("org_id", PgUUID, nullable=False),
    Column("incident_id", PgUUID, nullable=False),
    Column("kind", artifact_kind, nullable=False),
    Column("storage_key", Text, nullable=False),
    Column("content_sha256", Text, nullable=False),
    Column("size_bytes", BigInteger, nullable=False),
    Column("media_type", Text, nullable=False),
    Column("redacted", Boolean, nullable=False),
    Column("retain_until", Timestamp, nullable=False),
)

evidence = Table(
    "evidence",
    metadata,
    Column("evidence_id", PgUUID, primary_key=True),
    Column("incident_id", PgUUID, nullable=False),
    Column("kind", evidence_kind, nullable=False),
    Column("source_system", Text, nullable=False),
    Column("tool", Text, nullable=False),
    Column("collected_by", Text, nullable=False),
    Column("query", Text),
    Column("window_start", Timestamp),
    Column("window_end", Timestamp),
    Column("collected_at", Timestamp, nullable=False),
    Column("source_as_of", Timestamp),
    Column("source_link", Text),
    Column("summary", Text, nullable=False),
    Column("attributes", JSONB, nullable=False),
    Column("artifact_id", PgUUID),
    Column("content_sha256", Text, nullable=False),
    Column("is_partial", Boolean, nullable=False),
    Column("flagged_injection", Boolean, nullable=False),
    Column("gap_reason", Text),
    Column("strength_tier", SmallInteger),
    Column("content_removed_at", Timestamp),
    Column("content_removed_reason", Text),
)


@dataclass(frozen=True)
class IncidentScope:
    """What a skill activity needs about its incident: owner, scope and the opening alert."""

    incident_id: UUID
    org_id: UUID
    service: str
    environment: str
    started_at: datetime
    owner_group: str
    alert_id: str | None


def incident_scope(conn: Connection, incident_id: UUID) -> IncidentScope | None:
    i = incidents
    row = conn.execute(
        select(
            i.c.incident_id,
            i.c.org_id,
            i.c.started_at,
            services.c.slug.label("service"),
            services.c.owner_group,
            environments.c.slug.label("environment"),
        )
        .join(services, services.c.service_id == i.c.service_id)
        .join(environments, environments.c.environment_id == i.c.environment_id)
        .where(i.c.incident_id == incident_id)
    ).first()
    if row is None:
        return None
    alert_id = conn.execute(
        select(incident_alerts.c.source_alert_id)
        .where(incident_alerts.c.incident_id == incident_id)
        .order_by(incident_alerts.c.received_at)
        .limit(1)
    ).scalar()
    return IncidentScope(
        row.incident_id,
        row.org_id,
        row.service,
        row.environment,
        row.started_at,
        row.owner_group,
        alert_id,
    )


@dataclass(frozen=True)
class NewEvidence:
    """One record to store, with its snapshot if one was written to the object store."""

    evidence: Evidence
    attributes: Mapping[str, Any]
    snapshot_key: str | None = None
    snapshot_size: int = 0


def round_evidence(conn: Connection, incident_id: UUID, collection_key: str) -> list[Evidence]:
    """What an earlier attempt of the same round stored: the round is one transaction, so this is
    all of it or nothing. A retried activity answers from it instead of collecting again."""
    e = evidence
    rows = conn.execute(
        select(e)
        .where(
            e.c.incident_id == incident_id,
            e.c.attributes.contains({"collection_key": collection_key}),
        )
        .order_by(e.c.collected_at, e.c.evidence_id)
    ).mappings()
    return [_agent_evidence(r) for r in rows]


def change_history(conn: Connection, incident_id: UUID) -> list[dict[str, Any]]:
    """Every change the incident's earlier rounds stored, as `attributes.change` holds it
    (aisre.evidence.facts.ChangeInfo), for the tier rules of a later round."""
    e = evidence
    found = conn.execute(
        select(e.c.attributes["change"])
        .where(e.c.incident_id == incident_id, e.c.kind == "change")
        .order_by(e.c.collected_at)
    ).scalars()
    return [c for changes in found if changes for c in changes]


def store_round(
    conn: Connection,
    *,
    org_id: UUID,
    incident_id: UUID,
    items: Sequence[NewEvidence],
    calls: Sequence[Mapping[str, Any]],
    tool_calls: PgToolCalls,
    now: datetime,
) -> tuple[list[UUID], list[dict[str, Any]]]:
    """Admits `calls` (rows as ToolGate.record writes them; `fresh` marks a call whose
    `evidence_ids` are new in `items`) and stores the evidence each produced. Returns (stored
    evidence IDs, refused calls with their `hash`)."""
    lock_incident(conn, str(incident_id))
    dropped: set[str] = set()
    refused: list[dict[str, Any]] = []
    for row in calls:
        try:
            tool_calls.admit(conn, str(incident_id), row)
        except ToolRefused as e:
            refused.append({"tool": row["tool"], "hash": row["hash"], "refused": e.code})
            if row.get("fresh"):  # this round's own evidence goes with it; a repeat's stays
                dropped.update(row.get("evidence_ids", []))
    stored: list[UUID] = []
    for item in items:
        ev = item.evidence
        if str(ev.evidence_id) in dropped:
            continue
        artifact_id = _artifact(conn, org_id, incident_id, item, now) if item.snapshot_key else None
        conn.execute(
            insert(evidence).values(
                **ev.model_dump(exclude={"kind"}),
                kind=ev.kind.value,
                collected_by=COLLECTOR_ACTOR,
                attributes=dict(item.attributes),
                artifact_id=artifact_id,
            )
        )
        if ev.kind == EvidenceKind.gap:
            data: Strict = GapRecordedData(
                evidence_id=ev.evidence_id,
                source_system=ev.source_system,
                tool=ev.tool,
                gap_reason=ev.gap_reason or "",
            )
            kind = "aisre.evidence.gap_recorded.v1"
        else:
            data = EvidenceCollectedData(
                evidence_id=ev.evidence_id,
                kind=RecordKind(ev.kind.value),
                source_system=ev.source_system,
                tool=ev.tool,
                content_sha256=ev.content_sha256,
                is_partial=ev.is_partial,
                strength_tier=ev.strength_tier,
            )
            kind = "aisre.evidence.collected.v1"
        written: list[tuple[str, Strict]] = [(kind, data)]
        if ev.flagged_injection:
            flagged = InjectionFlaggedData(
                evidence_id=ev.evidence_id, source_system=ev.source_system, tool=ev.tool
            )
            written.append(("aisre.evidence.injection_flagged.v1", flagged))
        for event_type, event_data in written:
            append_event(
                conn,
                incident_id,
                event_type,
                COLLECTOR_ACTOR,
                event_data,
                occurred_at=ev.collected_at,
                source=COLLECTOR_SOURCE,
            )
        stored.append(ev.evidence_id)
    return stored, refused


def _artifact(
    conn: Connection, org_id: UUID, incident_id: UUID, item: NewEvidence, now: datetime
) -> UUID:
    artifact_id = uuid7()
    conn.execute(
        insert(artifacts).values(
            artifact_id=artifact_id,
            org_id=org_id,
            incident_id=incident_id,
            kind=ArtifactKind.evidence_snapshot.value,
            storage_key=item.snapshot_key,
            content_sha256=item.evidence.content_sha256,
            size_bytes=item.snapshot_size,
            media_type="application/json",
            redacted=True,
            retain_until=now + SNAPSHOT_RETENTION,
        )
    )
    return artifact_id


def _agent_evidence(r: RowMapping) -> Evidence:
    fields = set(Evidence.model_fields)
    return Evidence.model_validate(
        {k: (v.strip() if k == "content_sha256" else v) for k, v in r.items() if k in fields}
    )


def load_evidence(conn: Connection, incident_id: UUID, ids: Sequence[str]) -> list[Evidence]:
    """This incident's records among `ids`, as skills see them (9.5). Another incident's ID is
    simply not found (Q9.29)."""
    if not ids:
        return []
    e = evidence
    rows = conn.execute(
        select(e)
        .where(e.c.incident_id == incident_id, e.c.evidence_id.in_([UUID(i) for i in ids]))
        .order_by(e.c.collected_at, e.c.evidence_id)
    ).mappings()
    return [_agent_evidence(r) for r in rows]


def list_evidence(
    conn: Connection,
    incident_id: UUID,
    org_id: UUID,
    ids: Sequence[str] | None = None,
) -> list[EvidenceRecord]:
    """The incident's evidence as people read it, `flagged_injection` included (FR-39, ST-33),
    oldest first; none for an incident in another organisation."""
    e = evidence
    query = (
        select(e)
        .join(incidents, incidents.c.incident_id == e.c.incident_id)
        .where(e.c.incident_id == incident_id, incidents.c.org_id == org_id)
        .order_by(e.c.collected_at, e.c.evidence_id)
    )
    if ids is not None:
        query = query.where(e.c.evidence_id.in_([UUID(i) for i in ids]))
    return [
        EvidenceRecord.model_validate(
            {k: (v.strip() if k == "content_sha256" else v) for k, v in r.items()}
        )
        for r in conn.execute(query).mappings()
    ]
