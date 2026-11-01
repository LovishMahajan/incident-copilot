"""Artifact 10.9, 10.10, 10.13: runbook retrieval — ingestion, heading-aware chunking, BM25 search,
immutable index revisions, permissions, staleness, deletion, and retrieval evaluation.

Separate from inference: nothing here calls a model.
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


@dataclass(frozen=True)
class Chunk:
    chunk_id: str                  # doc_id@version#ordinal
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


HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
FENCE = re.compile(r"^```")


def chunk_document(doc: Document, max_words: int = 220, stale_after: timedelta = timedelta(days=180),
                   now: Optional[datetime] = None) -> list[Chunk]:
    """Q10.43, Q10.44: split at headings; never split a code block; keep the heading path with
    every chunk; split long sections at paragraph breaks, not mid-sentence."""
    now = now or datetime.utcnow()
    stale = doc.deprecated or (now - doc.reviewed_at) > stale_after
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
                                doc.allowed_groups, stale))
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


@dataclass
class IndexRevision:
    """Built once from a set of document versions; never modified. A runbook edit or deletion
    produces a new revision. Investigations record the revision ID they searched (Q10.64)."""

    revision_id: str
    built_at: datetime
    chunks: list[Chunk]
    k1: float = 1.2
    b: float = 0.75
    _tf: list[Counter] = field(default_factory=list, repr=False)
    _df: Counter = field(default_factory=Counter, repr=False)
    _avgdl: float = 0.0

    @staticmethod
    def build(docs: list[Document], now: Optional[datetime] = None, deleted: Iterable[str] = ()) -> IndexRevision:
        now = now or datetime.utcnow()
        gone = set(deleted)
        live = sorted((d for d in docs if d.doc_id not in gone), key=lambda d: d.doc_id)  # Q10.55
        rev = hashlib.sha256("|".join(f"{d.doc_id}@{d.version}" for d in live).encode()).hexdigest()[:16]
        chunks = [c for d in live for c in chunk_document(d, now=now)]
        idx = IndexRevision(rev, now, chunks)
        for c in chunks:
            tf = Counter(tokenize(c.text))
            idx._tf.append(tf)
            idx._df.update(tf.keys())
        idx._avgdl = (sum(sum(tf.values()) for tf in idx._tf) / len(chunks)) if chunks else 0.0
        return idx

    def idf(self, term: str) -> float:
        n, df = len(self.chunks), self._df.get(term, 0)
        return math.log(1 + (n - df + 0.5) / (df + 0.5))       # rare terms weigh more (Q10.46)

    def search(self, query: str, *, service: str, groups: set[str], k: int = 5,
               include_stale: bool = False) -> list[SearchHit]:
        """Permissions and service scope are filters applied BEFORE ranking (Q10.56)."""
        q = tokenize(query)
        hits = []
        for i, c in enumerate(self.chunks):
            if not (c.allowed_groups & groups):
                continue
            if "*" not in c.services and service not in c.services:
                continue
            if c.stale and not include_stale:
                continue
            tf, dl = self._tf[i], sum(self._tf[i].values())
            score = 0.0
            for t in q:
                f = tf.get(t, 0)
                if f:
                    score += self.idf(t) * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self._avgdl))
            if score > 0:
                hits.append(SearchHit(c.chunk_id, c.doc_id, c.doc_version, c.heading_path, round(score, 4),
                                      c.start, c.end, c.sha256, c.stale, c.text))
        hits.sort(key=lambda h: (-h.score, h.chunk_id))
        return hits[:k]


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
