"""Artifact 10.9, 10.10, 10.13: runbook retrieval — ingestion, heading-aware chunking, BM25 search,
immutable index revisions, permissions, staleness, deletion, and retrieval evaluation.

PostgreSQL stores the corpus (documents, chunks, index_revisions in schema.sql) and the worker
ranks it (ADR-62): IndexRevision.load rebuilds a revision from those rows. Separate from
inference: nothing here calls a model.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, Optional


# ---------------------------------------------------------------------------
# Documents and chunks
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Document:
    doc_id: str
    version: str                   # source version, e.g. git commit or wiki revision
    title: str
    text: str                      # markdown
    services: frozenset[str]       # services it applies to ("*" = all)
    allowed_groups: frozenset[str] # identity groups that may read it
    reviewed_at: datetime          # last time an owner confirmed it is correct
    owner: str
    deprecated: bool = False
    doc_type: str = "runbook"      # runbook | past_incident: what each search tool filters on


@dataclass(frozen=True)
class Chunk:
    chunk_id: str                  # doc_id@version#ordinal, the citation label; stored key is document_id + ordinal
    doc_id: str
    doc_version: str
    ordinal: int
    heading_path: tuple[str, ...]  # ("Checkout runbook", "HTTP 500 after deploy", "Revert config")
    text: str                      # the chunk text, with its heading path prefixed for search
    start: int                     # character span in the source document (Q10.53)
    end: int
    sha256: str
    services: frozenset[str]
    allowed_groups: frozenset[str]
    stale: bool
    doc_type: str = "runbook"
    document_id: Optional[str] = None   # documents.document_id, once loaded from storage


HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
FENCE = re.compile(r"^```")


def is_stale(doc_id: str, deprecated: bool, reviewed_at: datetime, now: datetime, stale_after: timedelta,
             changed_since_review: Optional[dict[str, datetime]] = None) -> bool:
    """Q10.54: deprecated, past review age, or the service changed after the last review."""
    changed = (changed_since_review or {}).get(doc_id, reviewed_at) > reviewed_at
    return deprecated or (now - reviewed_at) > stale_after or changed


def chunk_document(doc: Document, max_words: int = 220, stale_after: timedelta = timedelta(days=180),
                   now: Optional[datetime] = None,
                   changed_since_review: Optional[dict[str, datetime]] = None) -> list[Chunk]:
    """Q10.43, Q10.44: split at headings; never split a code block; keep the heading path with
    every chunk; split long sections at paragraph breaks, not mid-sentence."""
    now = now or datetime.utcnow()
    stale = is_stale(doc.doc_id, doc.deprecated, doc.reviewed_at, now, stale_after, changed_since_review)
    lines = doc.text.splitlines(keepends=True)
    sections: list[tuple[tuple[str, ...], int, int]] = []   # (heading path, start, end)
    path: list[tuple[int, str]] = []
    pos, sec_start, in_code = 0, 0, False
    current_path: tuple[str, ...] = (doc.title,)
    for line in lines:
        if FENCE.match(line.strip()):
            in_code = not in_code
        m = None if in_code else HEADING.match(line.rstrip("\n"))
        if m:
            if pos > sec_start:
                sections.append((current_path, sec_start, pos))
            level, title = len(m.group(1)), m.group(2).strip()
            path = [(l, t) for (l, t) in path if l < level] + [(level, title)]
            heads = tuple(t for _, t in path)
            current_path = heads if heads and heads[0] == doc.title else (doc.title,) + heads
            sec_start = pos
        pos += len(line)
    if pos > sec_start:
        sections.append((current_path, sec_start, pos))

    chunks: list[Chunk] = []
    for hp, s, e in sections:
        for ps, pe in _split_paragraphs(doc.text, s, e, max_words):
            body = doc.text[ps:pe].strip()
            if not body or HEADING.match(body) and "\n" not in body:
                continue
            text = " > ".join(hp) + "\n" + body
            n = len(chunks)
            chunks.append(Chunk(f"{doc.doc_id}@{doc.version}#{n}", doc.doc_id, doc.version, n, hp, text, ps, pe,
                                hashlib.sha256(doc.text[ps:pe].encode()).hexdigest(), doc.services,
                                doc.allowed_groups, stale, doc.doc_type))
    return chunks


def _split_paragraphs(text: str, s: int, e: int, max_words: int) -> Iterable[tuple[int, int]]:
    """Group paragraphs (and whole code blocks) up to max_words; spans index the original text."""
    blocks, i, in_code, block_start = [], s, False, s
    for m in re.finditer(r"\n", text[s:e] + "\n"):
        line_end = s + m.end()
        line = text[i:line_end]
        if FENCE.match(line.strip()):
            in_code = not in_code
        if not in_code and line.strip() == "" and i > block_start:
            blocks.append((block_start, i))
            block_start = line_end
        i = line_end
    if min(i, e) > block_start:
        blocks.append((block_start, min(i, e)))
    cur_s, cur_e, words = None, None, 0
    for bs, be in blocks:
        w = len(text[bs:be].split())
        if cur_s is not None and words + w > max_words:
            yield cur_s, cur_e
            cur_s, words = None, 0
        if cur_s is None:
            cur_s = bs
        cur_e, words = be, words + w
    if cur_s is not None:
        yield cur_s, cur_e


# ---------------------------------------------------------------------------
# Tokenizer that keeps technical identifiers (Q10.45, Q10.46)
# ---------------------------------------------------------------------------
IDENT = re.compile(r"[A-Za-z0-9]+(?:[._:/-][A-Za-z0-9]+)*")
STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "is", "for", "on", "with", "this", "that", "it", "be", "if"}


def tokenize(text: str) -> list[str]:
    """Keeps whole identifiers (ERR_CONN_RESET, payments.timeout_ms, HTTP-500) AND their parts,
    so an exact identifier matches strongly and a partial one still matches."""
    out = []
    for m in IDENT.finditer(text):
        tok = m.group(0).lower()
        if tok in STOP:
            continue
        out.append(tok)
        segments = [p for p in re.split(r"[.:/]", m.group(0)) if p]          # payments | timeout_ms
        parts = [p for p in re.split(r"[._:/-]|(?<=[a-z])(?=[A-Z])", m.group(0)) if p]   # payments | timeout | ms
        for p in (segments if len(segments) > 1 else []) + (parts if len(parts) > 1 else []):
            if p.lower() not in STOP and p.lower() != tok:
                out.append(p.lower())
    return out


# ---------------------------------------------------------------------------
# BM25 over an immutable index revision (Q10.50, Q10.52)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SearchHit:
    chunk_id: str
    doc_id: str
    doc_version: str
    heading_path: tuple[str, ...]
    score: float
    start: int
    end: int
    sha256: str
    stale: bool
    text: str
    services: frozenset[str] = frozenset()
    allowed_groups: frozenset[str] = frozenset()
    doc_type: str = "runbook"


@dataclass
class IndexRevision:
    """Built once from a set of document versions; never modified, except by purge_document for a
    legal or safety deletion. A runbook edit or deletion produces a new revision. Investigations
    record the revision ID they searched (Q10.64)."""

    revision_id: str               # index_revisions.revision_hash, a full SHA-256 (ADR-62)
    built_at: datetime
    chunks: list[Chunk]
    k1: float = 1.2
    b: float = 0.75
    _tf: list[Counter] = field(default_factory=list, repr=False)
    _df: Counter = field(default_factory=Counter, repr=False)
    _avgdl: float = 0.0

    @staticmethod
    def build(docs: list[Document], now: Optional[datetime] = None, deleted: Iterable[str] = (),
              changed_since_review: Optional[dict[str, datetime]] = None) -> IndexRevision:
        now = now or datetime.utcnow()
        gone = set(deleted)
        live = sorted((d for d in docs if d.doc_id not in gone), key=lambda d: d.doc_id)  # Q10.55
        # Document versions, permitted groups and the full build timestamp (ADR-62).
        parts = [f'{d.doc_id}@{d.version}:{",".join(sorted(d.allowed_groups))}' for d in live] + [now.isoformat()]
        rev = hashlib.sha256("|".join(parts).encode()).hexdigest()
        chunks = [c for d in live for c in chunk_document(d, now=now, changed_since_review=changed_since_review)]
        idx = IndexRevision(rev, now, chunks)
        idx._rank_stats()
        return idx

    @staticmethod
    def load(revision_hash: str, built_at: datetime, document_rows: list[dict], chunk_rows: list[dict],
             stale_after: timedelta = timedelta(days=180),
             changed_since_review: Optional[dict[str, datetime]] = None) -> IndexRevision:
        """Rebuilds a revision from its `documents` and `chunks` rows (ADR-62); the caller runs the
        query joining them through index_revision_documents. Staleness is judged as of built_at."""
        docs = {d["document_id"]: d for d in document_rows}
        chunks = []
        for r in sorted(chunk_rows, key=lambda r: (docs[r["document_id"]]["doc_ref"], r["ordinal"])):
            d = docs[r["document_id"]]
            chunks.append(Chunk(f'{d["doc_ref"]}@{d["doc_version"]}#{r["ordinal"]}', d["doc_ref"], d["doc_version"],
                                r["ordinal"], tuple(r["heading_path"]), r["body"], r["span_start"], r["span_end"],
                                r["content_sha256"], frozenset(d["services"]), frozenset(d["allowed_groups"]),
                                is_stale(d["doc_ref"], d["deprecated"], d["reviewed_at"], built_at, stale_after,
                                         changed_since_review),
                                d.get("doc_type", "runbook"), r["document_id"]))
        idx = IndexRevision(revision_hash, built_at, chunks)
        idx._rank_stats()
        return idx

    def _rank_stats(self) -> None:
        self._tf, self._df = [], Counter()
        for c in self.chunks:
            tf = Counter(tokenize(c.text))
            self._tf.append(tf)
            self._df.update(tf.keys())
        self._avgdl = (sum(sum(tf.values()) for tf in self._tf) / len(self.chunks)) if self.chunks else 0.0

    def idf(self, term: str) -> float:
        n, df = len(self.chunks), self._df.get(term, 0)
        return math.log(1 + (n - df + 0.5) / (df + 0.5))       # rare terms weigh more (Q10.46)

    def search(self, query: str, *, service: str, groups: set[str], k: int = 5, include_stale: bool = False,
               doc_type: Optional[str] = None) -> list[SearchHit]:
        """Permissions, service scope and document type are filters applied BEFORE ranking (Q10.56)."""
        q = tokenize(query)
        hits = []
        for i, c in enumerate(self.chunks):
            if not (c.allowed_groups & groups):
                continue
            if "*" not in c.services and service not in c.services:
                continue
            if c.stale and not include_stale:
                continue
            if doc_type is not None and c.doc_type != doc_type:
                continue
            tf, dl = self._tf[i], sum(self._tf[i].values())
            score = 0.0
            for t in q:
                f = tf.get(t, 0)
                if f:
                    score += self.idf(t) * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self._avgdl))
            if score > 0:
                hits.append(SearchHit(c.chunk_id, c.doc_id, c.doc_version, c.heading_path, round(score, 4),
                                      c.start, c.end, c.sha256, c.stale, c.text, c.services, c.allowed_groups,
                                      c.doc_type))
        hits.sort(key=lambda h: (-h.score, h.chunk_id))
        return hits[:k]


def purge_document(doc_id: str, revisions: Iterable[IndexRevision], evidence: Iterable[dict], reason: str,
                   now: datetime) -> list[dict]:
    """Q10.55: a legal or safety deletion, the one change to an existing revision. Removes the
    document's chunks from every revision, marks the evidence citing it "content removed", and
    returns one timeline event per affected incident for the caller to write."""
    for idx in revisions:
        idx.chunks = [c for c in idx.chunks if c.doc_id != doc_id]
        idx._rank_stats()
    incidents = []
    for e in evidence:
        if e.get("attributes", {}).get("doc_id") == doc_id:
            e["summary"] = "content removed"
            e.pop("text", None)
            if e["incident_id"] not in incidents:
                incidents.append(e["incident_id"])
    return [{"incident_id": i, "type": "aisre.evidence.content_removed.v1", "doc_id": doc_id, "reason": reason,
             "occurred_at": now} for i in incidents]


def stale_report(docs: list[Document], now: datetime, max_age: timedelta = timedelta(days=180),
                 changed_since_review: Optional[dict[str, datetime]] = None) -> list[tuple[str, str]]:
    """Q10.54: documents to send back to their owners."""
    out = []
    for d in docs:
        if d.deprecated:
            out.append((d.doc_id, "marked deprecated"))
        elif now - d.reviewed_at > max_age:
            out.append((d.doc_id, f"not reviewed for {(now - d.reviewed_at).days} days"))
        elif changed_since_review and changed_since_review.get(d.doc_id, d.reviewed_at) > d.reviewed_at:
            out.append((d.doc_id, "the service it describes changed after its last review"))
    return out


# ---------------------------------------------------------------------------
# Retrieval evaluation (10.13, Q10.51)
# ---------------------------------------------------------------------------
def recall_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    return len(set(ranked[:k]) & relevant) / len(relevant) if relevant else 0.0


def mrr(ranked: list[str], relevant: set[str]) -> float:
    for i, d in enumerate(ranked, 1):
        if d in relevant:
            return 1.0 / i
    return 0.0


def ndcg_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    dcg = sum(1.0 / math.log2(i + 1) for i, d in enumerate(ranked[:k], 1) if d in relevant)
    ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(k, len(relevant)) + 1))
    return dcg / ideal if ideal else 0.0


def wrong_service_hits(hits: list[SearchHit], service: str) -> int:
    return sum(1 for h in hits if service not in h.services and "*" not in h.services)


def wrong_permission_hits(hits: list[SearchHit], groups: set[str]) -> int:
    return sum(1 for h in hits if not (h.allowed_groups & groups))


@dataclass(frozen=True)
class LabelledQuery:
    """One row of the labelled set (10.13). `relevant` is (doc_id, heading path) pairs, because
    chunk IDs change with each document version."""
    query_id: str
    query: str
    kind: str                      # exact_identifier | paraphrase
    service: str
    groups: frozenset[str]
    relevant: tuple[tuple[str, tuple[str, ...]], ...]
    labelled_by: str
    labelled_at: datetime


def resolve_labels(idx: IndexRevision, q: LabelledQuery) -> set[str]:
    """A label that no longer resolves fails the run until its owner relabels it."""
    out: set[str] = set()
    for doc_id, path in q.relevant:
        found = {c.chunk_id for c in idx.chunks if c.doc_id == doc_id and c.heading_path == tuple(path)}
        if not found:
            raise ValueError(f"{q.query_id}: label {doc_id} {path} no longer resolves")
        out |= found
    return out


def evaluate_revision(idx: IndexRevision, queries: list[LabelledQuery], k: int = 5) -> dict:
    """Scores the whole set, then each kind. An empty report means there is no query set yet."""
    rows = []
    for q in queries:
        relevant = resolve_labels(idx, q)
        hits = idx.search(q.query, service=q.service, groups=set(q.groups), k=k)
        ranked = [h.chunk_id for h in hits]
        rows.append({"kind": q.kind, "recall": recall_at_k(ranked, relevant, k), "mrr": mrr(ranked, relevant),
                     "ndcg": ndcg_at_k(ranked, relevant, k), "wrong_service": wrong_service_hits(hits, q.service),
                     "wrong_permission": wrong_permission_hits(hits, set(q.groups))})

    def mean(subset: list[dict]) -> dict[str, float]:
        return {m: sum(r[m] for r in subset) / len(subset) for m in ("recall", "mrr", "ndcg")} if subset else {}

    if not rows:
        return {}
    return {"all": mean(rows), "wrong_service_hits": sum(r["wrong_service"] for r in rows),
            "wrong_permission_hits": sum(r["wrong_permission"] for r in rows),
            **{kind: mean([r for r in rows if r["kind"] == kind]) for kind in ("exact_identifier", "paraphrase")}}


RETRIEVAL_TARGETS = {"recall": 0.9, "mrr": 0.7, "ndcg": 0.8}   # 10.13, provisional


def activation_gate(report: dict, active_exact_recall: Optional[float] = None,
                    deletion: bool = False) -> tuple[bool, list[str]]:
    """10.13. Returns (activate, misses). No query set, no activation. A deletion activates at once
    and keeps its misses so they can alert the SRE lead."""
    misses = []
    if not report:
        misses.append("no query set")
    else:
        misses += [m for m, target in RETRIEVAL_TARGETS.items() if report["all"][m] < target]
        misses += [m for m in ("wrong_service_hits", "wrong_permission_hits") if report[m] > 0]
        exact = report["exact_identifier"].get("recall")
        if active_exact_recall is not None and (exact is None or exact < active_exact_recall):
            misses.append("exact_identifier_recall")
    return deletion or not misses, misses
