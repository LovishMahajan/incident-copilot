"""Artifact 7.2, 11.1, 11.8, 13.5 T2.8 (FR-19, ADR-69, ADR-72): a drafted patch, stored as its
own action.

`store_patch` writes one candidate in one transaction: the `actions` row (status `proposed`,
kind, exact target, base revision, description, expected effect, risk, undo plan, recovery
criteria and their digest, hold, policy versions), two `patch_diff` artifacts, the `patches` row
and `aisre.action.proposed.v1`. The two artifacts are the patch's canonical bytes
(Patch.canonical_bytes(): what the Test Runner reads, hashes and applies, `patches.diff_artifact_id`)
and the reviewable form (the whole Patch, with its summary and each hunk's rationale and evidence,
under the same key with `.review.json`), which the draft pull request is built from. Their objects
are written to the object store before this transaction, as evidence snapshots are. Idempotent on
the action ID: a retried step finds its row and writes nothing.

The action's digests stay unset: they are written once, when testing ends (ADR-64), and in
Release A no action reaches them (ADR-44).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from aisre.contracts import ActionKind, ActionProposedData
from aisre.record.incidents import append_event, uuid7

ACTOR = "system:patch-proposer"
SOURCE = "/aisre/worker/patch-proposer"
RETAIN = timedelta(days=365)  # provisional: a patch is kept with its incident's actions


def patch_keys(incident_id: UUID, action_id: UUID) -> tuple[str, str]:
    """The canonical bytes' key, and the reviewable form's. Internal, never returned (Artifact 7)."""
    base = f"patches/{incident_id}/{action_id}"
    return f"{base}.json", f"{base}.review.json"


@dataclass(frozen=True)
class NewPatch:
    action_id: UUID
    incident_id: UUID
    diagnosis_id: UUID
    kind: str
    target_deployment: str
    base_revision: str
    expected_after_revision: str
    description: str
    expected_effect: str
    risk: str
    undo_plan: str
    recovery_criteria: list[dict[str, Any]]
    policy_version: str
    patch_sha256: str
    canonical: bytes
    review: bytes
    files_changed: int
    lines_added: int
    lines_removed: int


@dataclass(frozen=True)
class Stored:
    action_id: UUID
    patch_sha256: str
    kind: str
    hold_seconds: int


def drafted(conn: Connection, action_id: UUID) -> Stored | None:
    row = conn.execute(
        text(
            "SELECT a.action_id, a.kind, a.hold_seconds, p.patch_sha256 FROM actions a "
            "JOIN patches p ON p.action_id = a.action_id WHERE a.action_id = :a"
        ),
        {"a": action_id},
    ).first()
    if row is None:
        return None
    return Stored(row[0], str(row[3]).strip(), str(row[1]), int(row[2]))


def store_patch(conn: Connection, p: NewPatch, now: datetime) -> Stored:
    conn.execute(
        text("SELECT 1 FROM incidents WHERE incident_id = :i FOR UPDATE"), {"i": p.incident_id}
    )
    found = drafted(conn, p.action_id)
    if found is not None:
        return found
    target = (
        conn.execute(
            text(
                "SELECT i.org_id, i.service_id, i.environment_id, s.slug AS service, "
                "e.slug AS environment, s.hold_seconds FROM incidents i "
                "JOIN services s ON s.service_id = i.service_id "
                "JOIN environments e ON e.environment_id = i.environment_id "
                "WHERE i.incident_id = :i"
            ),
            {"i": p.incident_id},
        )
        .mappings()
        .one()
    )
    criteria = json.dumps(p.recovery_criteria, sort_keys=True, separators=(",", ":"))
    hold = int(target["hold_seconds"])  # never below the service's (ADR-69)
    conn.execute(
        text(
            "INSERT INTO actions (action_id, incident_id, diagnosis_id, kind, target_service_id, "
            "target_environment_id, target_deployment, base_revision, expected_after_revision, "
            "description, expected_effect, risk, undo_plan, recovery_criteria, "
            "recovery_predicates_digest, hold_seconds, high_impact, policy_version) VALUES "
            "(:a, :i, :d, CAST(:k AS action_kind), :s, :e, :dep, :base, :after, :desc, :eff, "
            ":risk, :undo, CAST(:crit AS jsonb), :crit_sha, :hold, false, :pol)"
        ),
        {
            "a": p.action_id,
            "i": p.incident_id,
            "d": p.diagnosis_id,
            "k": p.kind,
            "s": target["service_id"],
            "e": target["environment_id"],
            "dep": p.target_deployment,
            "base": p.base_revision,
            "after": p.expected_after_revision,
            "desc": p.description,
            "eff": p.expected_effect,
            "risk": p.risk,
            "undo": p.undo_plan,
            "crit": criteria,
            "crit_sha": hashlib.sha256(criteria.encode()).hexdigest(),
            "hold": hold,
            "pol": p.policy_version,
        },
    )
    canonical_key, review_key = patch_keys(p.incident_id, p.action_id)
    diff_artifact = uuid7()
    for artifact_id, key, body, media in (
        (diff_artifact, canonical_key, p.canonical, "application/json"),
        (uuid7(), review_key, p.review, "application/vnd.aisre.patch+json"),
    ):
        conn.execute(
            text(
                "INSERT INTO artifacts (artifact_id, org_id, incident_id, kind, storage_key, "
                "content_sha256, size_bytes, media_type, redacted, retain_until) VALUES "
                "(:id, :org, :i, 'patch_diff', :key, :sha, :size, :media, false, :until)"
            ),
            {
                "id": artifact_id,
                "org": target["org_id"],
                "i": p.incident_id,
                "key": key,
                "sha": hashlib.sha256(body).hexdigest(),
                "size": len(body),
                "media": media,
                "until": now + RETAIN,
            },
        )
    conn.execute(
        text(
            "INSERT INTO patches (patch_id, action_id, base_revision, diff_artifact_id, "
            "patch_sha256, files_changed, lines_added, lines_removed) VALUES "
            "(:id, :a, :base, :art, :sha, :files, :added, :removed)"
        ),
        {
            "id": uuid7(),
            "a": p.action_id,
            "base": p.base_revision,
            "art": diff_artifact,
            "sha": p.patch_sha256,
            "files": p.files_changed,
            "added": p.lines_added,
            "removed": p.lines_removed,
        },
    )
    append_event(
        conn,
        p.incident_id,
        "aisre.action.proposed.v1",
        ACTOR,
        ActionProposedData(
            action_id=p.action_id,
            diagnosis_id=p.diagnosis_id,
            parent_action_id=None,
            kind=ActionKind(p.kind),
            target_service=str(target["service"]),
            target_environment=str(target["environment"]),
            target_deployment=p.target_deployment,
        ),
        occurred_at=now,
        source=SOURCE,
    )
    return Stored(p.action_id, p.patch_sha256, p.kind, hold)
