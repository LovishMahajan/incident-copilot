"""Artifact 10.9, 10.13, 13.5 T1.17: ingestion, evaluation and activation of the runbook corpus.

`read_folder` reads Markdown documents, each with a front-matter block (the synthetic corpus in
tests/fixtures/runbooks/ shows the fields), and redacts them (10.9 step 2). `build` makes an
immutable IndexRevision from them (retrieval.py), scores it on the labelled query set
(`load_queries`, 10.13), and decides activation with `activation_gate`: every target must hold,
exact-identifier recall must not fall below the active revision's, and a deletion activates at
once. aisre.record.corpus stores the documents, chunks and revision, with its evaluation, in one
transaction; investigations search the newest activated revision (ADR-62).

`passages` turns search hits into evidence records for the Knowledge Retriever: guidance, never
proof, so they carry no strength tier and support no claim (Artifact 10).

    python -m aisre.retrieval.corpus <folder> --org <org-id> [--queries <file>]

ingests a folder as `aisre_app` (AISRE_DATABASE_URL) and prints the evaluation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from aisre.agent.agent_contracts import Evidence, EvidenceKind
from aisre.agent.validation import flag_injection
from aisre.record.incidents import uuid7
from aisre.reliability.telemetry import redact_text
from aisre.retrieval.retrieval import (
    Document,
    IndexRevision,
    LabelledQuery,
    SearchHit,
    activation_gate,
    evaluate_revision,
)

SUMMARY_MAX = 4000
QUERIES_FILE = "queries-v1.json"


class CorpusError(ValueError):
    """A document or query file that can't be read as the format says."""


def _front_matter(text: str, path: Path) -> tuple[dict[str, str], str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise CorpusError(f"{path.name}: no front matter")
    try:
        end = next(i for i, ln in enumerate(lines[1:], 1) if ln.strip() == "---")
    except StopIteration:
        raise CorpusError(f"{path.name}: front matter not closed") from None
    meta = {}
    for ln in lines[1:end]:
        key, sep, value = ln.partition(":")
        if not sep:
            raise CorpusError(f"{path.name}: front matter line without a colon")
        meta[key.strip()] = value.strip()
    return meta, "".join(lines[end + 1 :]).lstrip("\n")


def _list(value: str) -> frozenset[str]:
    return frozenset(v.strip() for v in value.split(",") if v.strip())


def read_document(path: Path) -> Document:
    meta, body = _front_matter(path.read_text(encoding="utf-8"), path)
    try:
        return Document(
            doc_id=meta["doc_id"],
            version=meta["version"],
            title=meta["title"],
            text=redact_text(body),
            services=_list(meta["services"]),
            allowed_groups=_list(meta["allowed_groups"]),
            reviewed_at=datetime.fromisoformat(meta["reviewed_at"]),
            owner=meta["owner"],
            deprecated=meta.get("deprecated", "false").lower() == "true",
            doc_type=meta.get("doc_type", "runbook"),
        )
    except KeyError as e:
        raise CorpusError(f"{path.name}: missing {e.args[0]}") from None


def read_folder(folder: Path) -> list[Document]:
    """Every `*.md` with front matter; others (a README) are skipped."""
    docs = [
        read_document(p)
        for p in sorted(folder.glob("*.md"))
        if p.read_text(encoding="utf-8").startswith("---")
    ]
    seen: set[tuple[str, str]] = set()
    for d in docs:
        if (d.doc_id, d.version) in seen:
            raise CorpusError(f"{d.doc_id}@{d.version} appears twice")
        seen.add((d.doc_id, d.version))
    return docs


@dataclass(frozen=True)
class QuerySet:
    version: str
    queries: list[LabelledQuery]


def load_queries(path: Path) -> QuerySet:
    data = json.loads(path.read_text(encoding="utf-8"))
    return QuerySet(
        data["version"],
        [
            LabelledQuery(
                query_id=q["query_id"],
                query=q["query"],
                kind=q["kind"],
                service=q["service"],
                groups=frozenset(q["groups"]),
                relevant=tuple((doc, tuple(path)) for doc, path in q["relevant"]),
                labelled_by=q["labelled_by"],
                labelled_at=datetime.fromisoformat(q["labelled_at"]),
            )
            for q in data["queries"]
        ],
    )


@dataclass(frozen=True)
class Build:
    revision: IndexRevision
    documents: list[Document]
    query_set_version: str | None
    report: dict[str, Any]
    activate: bool
    misses: list[str]


def build(
    docs: Sequence[Document],
    queries: QuerySet | None,
    *,
    now: datetime,
    active_exact_recall: float | None = None,
    active_doc_ids: Iterable[str] = (),
) -> Build:
    """A new revision, scored and judged. A document of the active revision that is missing here
    is a deletion, which activates whatever the score (10.9)."""
    idx = IndexRevision.build(list(docs), now=now)
    report = evaluate_revision(idx, queries.queries) if queries else {}
    deletion = bool(set(active_doc_ids) - {d.doc_id for d in docs})
    activate, misses = activation_gate(report, active_exact_recall, deletion)
    return Build(idx, list(docs), queries.version if queries else None, report, activate, misses)


# ---------------------------------------------------------------------------
# Hits as evidence (the Knowledge Retriever)
# ---------------------------------------------------------------------------
def passages(
    incident_id: UUID,
    tool: str,
    params: dict[str, Any],
    hits: Sequence[SearchHit],
    revision: str,
    collection_key: str,
    now: datetime,
) -> list[tuple[Evidence, dict[str, Any]]]:
    """One record per hit, pinned to its document version, chunk, span and fingerprint (10.9).
    Flagged like any other extract (ST-33)."""
    out = []
    query = json.dumps({"tool": tool, "params": params}, sort_keys=True, separators=(",", ":"))
    for h in hits:
        text = redact_text(h.text)
        ev = Evidence(
            evidence_id=uuid7(),
            incident_id=incident_id,
            kind=EvidenceKind(h.doc_type),
            source_system="runbook-corpus",
            tool=tool,
            query=query,
            window_start=None,
            window_end=None,
            collected_at=now,
            source_as_of=None,
            source_link=f"corpus:{h.chunk_id}",
            summary=text if len(text) <= SUMMARY_MAX else text[: SUMMARY_MAX - 1] + "…",
            content_sha256=h.sha256,
        )
        attributes = {
            "collection_key": collection_key,
            "params": params,
            "doc_id": h.doc_id,
            "doc_version": h.doc_version,
            "chunk_id": h.chunk_id,
            "heading_path": list(h.heading_path),
            "span_start": h.start,
            "span_end": h.end,
            "score": h.score,
            "stale": h.stale,
            "index_revision": revision,
        }
        out.append((flag_injection(ev), attributes))
    return out


def knowledge_gap(
    incident_id: UUID, tool: str, reason: str, collection_key: str, now: datetime
) -> tuple[Evidence, dict[str, Any]]:
    """Runbooks are optional evidence (ADR-54): no revision or a failed search is a gap."""
    summary = f"{tool} returned nothing usable: {reason}. Not collected; not a sign of health."
    ev = Evidence(
        evidence_id=uuid7(),
        incident_id=incident_id,
        kind=EvidenceKind.gap,
        source_system="runbook-corpus",
        tool=tool,
        query=None,
        window_start=None,
        window_end=None,
        collected_at=now,
        source_as_of=None,
        source_link=None,
        summary=summary,
        content_sha256=hashlib.sha256(summary.encode()).hexdigest(),
        gap_reason=reason,
    )
    return ev, {"collection_key": collection_key, "missing_kind": "runbook"}


def main(argv: Sequence[str] | None = None) -> int:
    from aisre.config import Settings
    from aisre.record import corpus as store
    from aisre.record import make_engine

    parser = argparse.ArgumentParser(prog="python -m aisre.retrieval.corpus")
    parser.add_argument("folder", type=Path)
    parser.add_argument("--org", type=UUID, required=True)
    parser.add_argument("--queries", type=Path, help=f"default: <folder>/{QUERIES_FILE}")
    args = parser.parse_args(argv)
    qfile = args.queries or args.folder / QUERIES_FILE
    queries = load_queries(qfile) if qfile.exists() else None
    engine = make_engine(Settings.from_env().database_url)
    try:
        with engine.begin() as conn:
            result = store.ingest(
                conn, args.org, read_folder(args.folder), queries, datetime.now(UTC)
            )
    finally:
        engine.dispose()
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
