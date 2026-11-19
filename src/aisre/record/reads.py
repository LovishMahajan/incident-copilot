"""Artifact 7.4 (T1.5, FR-36): evidence, the current diagnosis and actions, as the API shows them.

Plain SQL over the schema's own column names (ADR-04), turned into the contracts: EvidenceRecord,
Diagnosis and ActionView. Every query is scoped to one incident, and the caller has already
checked that the incident belongs to its organisation and service (aisre.api).

Evidence is paged in `evidence_id` order (UUIDv7, so collection order); the cursor is the last
ID returned. The current diagnosis is the highest revision. A diagnosis row names no hypotheses
of its own, so it shows the incident's hypotheses recorded up to it, the latest five by
ordinal (Diagnosis allows 2 to 5), always keeping the leading one. Actions come in creation
order, each with its patch and its latest test run.

Cited runbooks and past incident reviews are shown only to a reader in one of the document's
permitted groups (Artifact 10.9, T1.24): to anyone else `restrict_documents` replaces the
passage, its heading path and span with "restricted runbook (owner: …)", keeping only which
document version it was. A document no longer in the organisation's corpus is shown restricted.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from aisre.contracts import (
    ActionView,
    Diagnosis,
    EvidenceLink,
    EvidenceRecord,
    Hypothesis,
    PatchView,
    TestRunView,
)
from aisre.retrieval.retrieval import may_read, restricted_notice

EVIDENCE_COLUMNS = (
    "evidence_id, incident_id, kind, source_system, tool, collected_by, query, window_start, "
    "window_end, collected_at, source_as_of, source_link, summary, attributes, artifact_id, "
    "content_sha256, is_partial, flagged_injection, gap_reason, strength_tier, "
    "content_removed_at, content_removed_reason"
)
HYPOTHESES_SHOWN = 5
DOCUMENT_KINDS = ("runbook", "past_incident")
KEPT_WHEN_RESTRICTED = ("collection_key", "doc_id", "doc_version", "index_revision")


def evidence_page(
    conn: Connection,
    incident_id: UUID,
    *,
    kind: str | None = None,
    after: UUID | None = None,
    limit: int = 100,
) -> list[EvidenceRecord]:
    rows = conn.execute(
        text(
            f"SELECT {EVIDENCE_COLUMNS} FROM evidence WHERE incident_id = :i "
            "AND (CAST(:kind AS evidence_kind) IS NULL OR kind = CAST(:kind AS evidence_kind)) "
            "AND (CAST(:after AS uuid) IS NULL OR evidence_id > CAST(:after AS uuid)) "
            "ORDER BY evidence_id LIMIT :limit"
        ),
        {"i": incident_id, "kind": kind, "after": after, "limit": limit},
    ).mappings()
    return [EvidenceRecord.model_validate(dict(r)) for r in rows]


def restrict_documents(
    conn: Connection, org_id: UUID, items: list[EvidenceRecord], groups: frozenset[str]
) -> list[EvidenceRecord]:
    """`items` as a reader with `groups` may see them (10.9)."""
    cited = {
        (str(e.attributes.get("doc_id")), str(e.attributes.get("doc_version")))
        for e in items
        if e.kind.value in DOCUMENT_KINDS
    }
    if not cited:
        return items
    rows = conn.execute(
        text(
            "SELECT doc_ref, doc_version, doc_type, allowed_groups, owner FROM documents "
            "WHERE org_id = :o AND doc_ref = ANY(:refs)"
        ),
        {"o": org_id, "refs": sorted({ref for ref, _ in cited})},
    ).mappings()
    docs = {(r["doc_ref"], r["doc_version"]): r for r in rows}
    out = []
    for e in items:
        if e.kind.value not in DOCUMENT_KINDS:
            out.append(e)
            continue
        key = (str(e.attributes.get("doc_id")), str(e.attributes.get("doc_version")))
        doc = docs.get(key)
        if doc is not None and may_read(doc["allowed_groups"], groups):
            out.append(e)
            continue
        owner = doc["owner"] if doc is not None else "unknown"
        kept = {k: e.attributes[k] for k in KEPT_WHEN_RESTRICTED if k in e.attributes}
        out.append(
            e.model_copy(
                update={
                    "summary": restricted_notice(e.kind.value, owner),
                    "attributes": kept | {"restricted": True, "owner": owner},
                    "source_link": None,
                    "query": None,
                }
            )
        )
    return out


def current_diagnosis(conn: Connection, incident_id: UUID) -> Diagnosis | None:
    d = (
        conn.execute(
            text(
                "SELECT diagnosis_id, incident_id, revision, outcome, leading_hypothesis_id, "
                "category, confidence, summary, claims, ruled_out, missing_evidence, stop_reason, "
                "reviewed, review_notes, produced_by, final_outcome, final_confidence, created_at "
                "FROM diagnoses WHERE incident_id = :i ORDER BY revision DESC LIMIT 1"
            ),
            {"i": incident_id},
        )
        .mappings()
        .first()
    )
    if d is None:
        return None
    rows = conn.execute(
        text(
            "SELECT hypothesis_id, ordinal, statement, category, claims, confirm_test, "
            "refute_test, status, confidence, status_reason FROM hypotheses "
            "WHERE incident_id = :i AND created_at <= :at ORDER BY ordinal"
        ),
        {"i": incident_id, "at": d["created_at"]},
    ).mappings()
    hyps = [dict(r) for r in rows]
    shown = hyps[-HYPOTHESES_SHOWN:]
    lead = d["leading_hypothesis_id"]
    if lead is not None and lead not in {h["hypothesis_id"] for h in shown}:
        shown = [h for h in hyps if h["hypothesis_id"] == lead] + shown[1:]
    links = _evidence_links(conn, [h["hypothesis_id"] for h in shown])
    fields = {k: v for k, v in d.items() if k != "created_at"}
    return Diagnosis.model_validate(
        fields
        | {
            "hypotheses": [
                Hypothesis.model_validate(h | {"evidence": links.get(h["hypothesis_id"], [])})
                for h in sorted(shown, key=lambda h: int(h["ordinal"]))
            ]
        }
    )


def actions(conn: Connection, incident_id: UUID, action_id: UUID | None = None) -> list[ActionView]:
    """The incident's actions, oldest first; or just `action_id`, if given and the incident's."""
    rows = conn.execute(
        text(
            "SELECT a.action_id, a.incident_id, a.diagnosis_id, a.parent_action_id, a.kind, "
            "s.slug AS target_service, e.slug AS target_environment, a.target_deployment, "
            "a.base_revision, a.expected_after_revision, a.description, a.expected_effect, "
            "a.risk, a.undo_plan, a.recovery_criteria, a.hold_seconds, a.action_digest, "
            "a.status, a.version FROM actions a "
            "JOIN services s ON s.service_id = a.target_service_id "
            "JOIN environments e ON e.environment_id = a.target_environment_id "
            "WHERE a.incident_id = :i AND (CAST(:a AS uuid) IS NULL OR a.action_id = CAST(:a AS uuid)) "
            "ORDER BY a.created_at, a.action_id"
        ),
        {"i": incident_id, "a": action_id},
    ).mappings()
    out = []
    for r in rows:
        patch = (
            conn.execute(
                text(
                    "SELECT patch_id, base_revision, patch_sha256, files_changed, lines_added, "
                    "lines_removed, diff_artifact_id, review_ref "
                    "FROM patches WHERE action_id = :a"
                ),
                {"a": r["action_id"]},
            )
            .mappings()
            .first()
        )
        test = (
            conn.execute(
                text(
                    "SELECT test_run_id, patch_sha256, reproduced, outcome, isolation, output_artifact_id, "
                    "started_at, finished_at FROM test_runs WHERE action_id = :a "
                    "ORDER BY started_at DESC, test_run_id DESC LIMIT 1"
                ),
                {"a": r["action_id"]},
            )
            .mappings()
            .first()
        )
        out.append(
            ActionView.model_validate(
                dict(r)
                | {
                    "patch": PatchView.model_validate(dict(patch)) if patch else None,
                    "latest_test": TestRunView.model_validate(dict(test)) if test else None,
                }
            )
        )
    return out


def incident_of_action(conn: Connection, action_id: UUID, org_id: UUID) -> UUID | None:
    found = conn.execute(
        text(
            "SELECT a.incident_id FROM actions a JOIN incidents i USING (incident_id) "
            "WHERE a.action_id = :a AND i.org_id = :o"
        ),
        {"a": action_id, "o": org_id},
    ).scalar()
    return found if isinstance(found, UUID) else None


@dataclass(frozen=True)
class StoredArtifact:
    """An artifact's row, for the content read. `storage_key` is internal and never returned."""

    incident_id: UUID
    kind: str
    storage_key: str
    content_sha256: str
    media_type: str


def artifact(conn: Connection, artifact_id: UUID, org_id: UUID) -> StoredArtifact | None:
    row = (
        conn.execute(
            text(
                "SELECT incident_id, kind, storage_key, content_sha256, media_type FROM artifacts "
                "WHERE artifact_id = :a AND org_id = :o"
            ),
            {"a": artifact_id, "o": org_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    return StoredArtifact(
        row["incident_id"],
        str(row["kind"]),
        str(row["storage_key"]),
        str(row["content_sha256"]).strip(),
        str(row["media_type"]),
    )


def _evidence_links(conn: Connection, ids: list[Any]) -> dict[Any, list[EvidenceLink]]:
    if not ids:
        return {}
    rows = conn.execute(
        text(
            "SELECT hypothesis_id, evidence_id, relation, note FROM hypothesis_evidence "
            "WHERE hypothesis_id = ANY(:ids) ORDER BY evidence_id, relation"
        ),
        {"ids": ids},
    ).mappings()
    links: dict[Any, list[EvidenceLink]] = {}
    for r in rows:
        link: Mapping[str, Any] = {k: r[k] for k in ("evidence_id", "relation", "note")}
        links.setdefault(r["hypothesis_id"], []).append(EvidenceLink.model_validate(link))
    return links
