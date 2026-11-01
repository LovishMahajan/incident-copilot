"""Artifact 10.11, 10.12: context assembly and prompt versioning.

Builds the context list given to the Model Gateway from canonical records (evidence, runbook
hits, computed facts), under a token budget. Every item keeps its source reference; nothing is
summarised without its references; contradictions are always kept, both sides; and the result
comes with a manifest that is stored so the call can be reproduced (Q10.64).
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Optional


def tokens(text: str) -> int:
    return math.ceil(len(text) / 3) + 4          # same conservative estimate as the gateway


@dataclass(frozen=True)
class Item:
    ref: str                     # evidence:<uuid> | runbook:<chunk_id> | fact:<name> | gap:<uuid>
    kind: str                    # fact | contradiction | gap | evidence | runbook
    text: str
    priority: int                # lower = more important
    sha256: str = ""
    refs: tuple[str, ...] = ()   # for merged items: every source they stand for


# Evidence kinds by decisiveness for change-caused failures (Artifact 3 / validation.STRENGTH)
EVIDENCE_PRIORITY = {"test_result": 10, "trace": 11, "error_group": 11, "metric": 12, "change": 13,
                     "log": 14, "dependency": 13, "alert": 15, "past_incident": 30}


@dataclass
class Manifest:
    prompt_id: str
    prompt_version: str
    index_revision: Optional[str]
    budget_tokens: int
    used_tokens: int = 0
    included: list[dict] = field(default_factory=list)
    excluded: list[dict] = field(default_factory=list)

    def sha256(self) -> str:
        return hashlib.sha256(json.dumps(self.__dict__, sort_keys=True, default=str).encode()).hexdigest()


def merge_repeated_logs(evidence: list[dict]) -> list[dict]:
    """Q10.60: collapse log items with the same message template into one item that lists every
    source reference and the count. Compression never drops a reference (Q10.62)."""
    groups: dict[str, list[dict]] = {}
    other = []
    for e in evidence:
        if e["kind"] == "log" and e.get("template"):
            groups.setdefault(e["template"], []).append(e)
        else:
            other.append(e)
    merged = []
    for tpl, items in groups.items():
        first = items[0]
        refs = tuple(f'evidence:{i["evidence_id"]}' for i in items)
        merged.append({**first, "summary": f'{first["summary"]} (seen {len(items)} times; sources: {", ".join(refs)})',
                       "refs": refs})
    return other + merged


def assemble(*, prompt_id: str, prompt_version: str, budget_tokens: int, facts: dict[str, str],
             evidence: list[dict], contradictions: list[tuple[str, str, str]], runbook_hits: list,
             index_revision: Optional[str], max_item_tokens: int = 600,
             must_include: set[str] = frozenset()) -> tuple[list[dict], Manifest]:
    """Returns (context items for ModelRequest.context, manifest).

    Order of priority (Q10.57, Q10.61):
      0  computed facts (timeline, version split, changed values)       always
      1  contradictions, both sides with references                     always
      2  gaps (missing sources)                                         always
      3  evidence named in must_include (cited by the current draft)    always
      10+ other evidence, most decisive kind first, then most recent
      40+ runbook passages, best match first (max 3)
    """
    items: list[Item] = []
    for name, value in facts.items():
        items.append(Item(f"fact:{name}", "fact", f"{name}: {value}", 0))
    for a, b, note in contradictions:
        items.append(Item(f"contradiction:{a}|{b}", "contradiction",
                          f"These sources disagree: {a} vs {b}. {note}", 1, refs=(a, b)))
    merged = merge_repeated_logs(evidence)
    for e in merged:
        ref = f'evidence:{e["evidence_id"]}'
        refs = tuple(e.get("refs", (ref,)))
        if e["kind"] == "gap":
            items.append(Item(f'gap:{e["evidence_id"]}', "gap", f'MISSING: {e["summary"]}', 2, e.get("sha256", "")))
            continue
        pr = 3 if ref in must_include else EVIDENCE_PRIORITY.get(e["kind"], 20)
        label = f'[{e["kind"]} from {e.get("source_system", "?")}, window {e.get("window", "?")}]'
        items.append(Item(ref, "evidence", f'{label} {e["summary"]}', pr, e.get("sha256", ""), refs))
    for rank, h in enumerate(runbook_hits[:3]):
        items.append(Item(f"runbook:{h.chunk_id}", "runbook",
                          f'[runbook {" > ".join(h.heading_path)}, version {h.doc_version}] {h.text}', 40 + rank, h.sha256))

    # Stable order: priority, then recency is already reflected in input order, then ref
    items.sort(key=lambda i: (i.priority, i.ref))
    man = Manifest(prompt_id, prompt_version, index_revision, budget_tokens)
    out: list[dict] = []
    for it in items:
        text = it.text
        cost = tokens(text)
        if cost > max_item_tokens and it.priority >= 3:
            keep = max_item_tokens * 3 - 80
            text = text[:keep] + f" … [truncated; full record: {it.ref}]"     # reference kept (Q10.62)
            cost = tokens(text)
        always = it.priority <= 3
        if man.used_tokens + cost > budget_tokens and not always:
            man.excluded.append({"ref": it.ref, "reason": "token budget", "tokens": cost})
            continue
        man.used_tokens += cost
        out.append({"ref": it.ref, "kind": it.kind, "text": text, "refs": list(it.refs) or [it.ref]})
        man.included.append({"ref": it.ref, "kind": it.kind, "sha256": it.sha256, "tokens": cost,
                             "truncated": text != it.text})
    if man.used_tokens > budget_tokens:
        # Only 'always' items remain over budget: the caller must narrow the evidence, not drop them.
        raise ValueError(f"required context ({man.used_tokens} tokens) exceeds budget ({budget_tokens})")
    return out, man


# ---------------------------------------------------------------------------
# Prompt versioning (10.12)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PromptVersion:
    prompt_id: str               # "diagnose"
    version: int                 # 7
    text: str
    output_schema_name: str      # "DiagnosisDraft"
    model_alias: str             # "default" -> pinned model ID in configuration
    evaluated_on: str            # evaluation run that approved it

    @property
    def ref(self) -> str:
        return f"{self.prompt_id}@{self.version}"

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()


class PromptRegistry:
    """Prompts live in the repository, one file per version; a version is never edited after use.
    A new version goes live only after it passes the evaluation suite (Artifact 9, 9.11)."""

    def __init__(self) -> None:
        self._p: dict[str, PromptVersion] = {}
        self.active: dict[str, int] = {}

    def add(self, p: PromptVersion) -> None:
        if p.ref in self._p and self._p[p.ref].sha256 != p.sha256:
            raise ValueError(f"{p.ref} already exists with different text; create a new version")
        self._p[p.ref] = p

    def activate(self, prompt_id: str, version: int) -> None:
        if f"{prompt_id}@{version}" not in self._p:
            raise KeyError(prompt_id)
        self.active[prompt_id] = version

    def current(self, prompt_id: str) -> PromptVersion:
        return self._p[f"{prompt_id}@{self.active[prompt_id]}"]

    def get(self, ref: str) -> PromptVersion:
        return self._p[ref]                        # old versions stay readable for replay
