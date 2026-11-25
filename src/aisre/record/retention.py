"""Artifact 12.7, 13.5 T4.8 (NFR-20, NFR-45; ADR-57): the retention job and its audit query.

Runs as aisre_retention, the role that may delete what has expired and nothing else: the
schema's protect_row trigger confirms each row is due, so a bug here can't delete early
(NFR-45's "deleted early: 0"), and purge_incident refuses an incident that isn't due.

One run, in this order:

1. Object-store content past its period (`artifacts_due_for_deletion`: 30 days for evidence
   snapshots, a year when cited, and content removed by a legal deletion). Held items are
   skipped and counted: an incident not yet closed or cancelled holds everything it references
   (Q12.48). Each object goes with every version of it, then an `artifact_deletions` row says
   so; the artifact row stays, because append-only records point at it.
2. Whole incidents closed or cancelled over a year ago, with nothing of theirs still kept
   (`purge_incident`).
3. Dead letters past `retain_until`, idempotency keys past `expires_at` (24 hours).

`overdue` is the audit query: what is past its period and still here, by class. After a run it
is zero for every class but `held` (NFR-20's "evidence older than the period: 0").
Holds other than an open incident (an open review, a legal or security hold) have no table yet.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import Connection, Engine, text

DeleteObject = Callable[[str], object]

HELD = (
    "EXISTS (SELECT 1 FROM incidents i WHERE i.incident_id = d.incident_id "
    "AND i.status NOT IN ('closed', 'cancelled'))"
)
DUE_INCIDENTS = (
    "SELECT incident_id FROM incidents WHERE status IN ('closed', 'cancelled') "
    "AND COALESCE(closed_at, updated_at) < now() - interval '1 year' "
    "AND incident_retention_due(incident_id) ORDER BY incident_id"
)


@dataclass
class RetentionReport:
    objects_deleted: int = 0
    held: int = 0  # due, but an open incident holds it (Q12.48)
    incidents_purged: int = 0
    dead_letters_deleted: int = 0
    idempotency_keys_deleted: int = 0
    failed: list[str] = field(default_factory=list)  # artifact IDs: retried next run


def run(engine: Engine, delete_object: DeleteObject | None) -> RetentionReport:
    """One retention run. Without an object store (`delete_object` None) step 1 is skipped:
    nothing is marked deleted that still exists."""
    rep = RetentionReport()
    if delete_object is not None:
        with engine.connect() as conn:
            due = conn.execute(
                text(
                    f"SELECT d.artifact_id, d.incident_id, d.storage_key, {HELD} AS held "
                    "FROM artifacts_due_for_deletion d ORDER BY d.artifact_id"
                )
            ).all()
        for artifact_id, incident_id, key, held in due:
            if held:
                rep.held += 1
                continue
            try:
                delete_object(str(key))
            except Exception:  # noqa: BLE001 (left for the next run, and reported)
                rep.failed.append(str(artifact_id))
                continue
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO artifact_deletions (artifact_id, incident_id) "
                        "VALUES (:a, :i) ON CONFLICT DO NOTHING"
                    ),
                    {"a": artifact_id, "i": incident_id},
                )
            rep.objects_deleted += 1
    with engine.connect() as conn:
        incidents: list[UUID] = list(conn.execute(text(DUE_INCIDENTS)).scalars())
    for iid in incidents:
        with engine.begin() as conn:
            conn.execute(text("SELECT purge_incident(:i)"), {"i": iid})
        rep.incidents_purged += 1
    with engine.begin() as conn:
        rep.dead_letters_deleted = int(
            conn.execute(text("DELETE FROM dead_letters WHERE retain_until < now()")).rowcount
        )
        rep.idempotency_keys_deleted = int(
            conn.execute(text("DELETE FROM idempotency_keys WHERE expires_at < now()")).rowcount
        )
    return rep


def overdue(conn: Connection) -> dict[str, int]:
    """The audit query (12.7): items past their period that are still kept, by class."""
    row = conn.execute(
        text(
            f"SELECT (SELECT count(*) FROM artifacts_due_for_deletion d WHERE NOT {HELD}), "
            f"(SELECT count(*) FROM artifacts_due_for_deletion d WHERE {HELD}), "
            f"(SELECT count(*) FROM ({DUE_INCIDENTS}) x), "
            "(SELECT count(*) FROM dead_letters WHERE retain_until < now()), "
            "(SELECT count(*) FROM idempotency_keys WHERE expires_at < now())"
        )
    ).one()
    return dict(
        zip(("objects", "held", "incidents", "dead_letters", "idempotency_keys"), row, strict=True)
    )
