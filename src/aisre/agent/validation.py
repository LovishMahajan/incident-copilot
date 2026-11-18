"""Artifact 9.7–9.8: the checks code runs on every model output, before and beside the critic.

These run in activities (they read the database). The model's word is never accepted on
its own: citations must exist, belong to the incident and still match their fingerprint;
confidence comes from the strength of cited observations (ADR-53), not from the model; and
output carrying a secret, link or command fails validation (ADR-59).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Callable, Iterable, Optional
from uuid import UUID

from pydantic import BaseModel

from aisre.agent.agent_contracts import (
    NO_TIER_KINDS,
    Claim,
    Confidence,
    CriticIssue,
    CriticVerdict,
    DiagnosisDraft,
    Evidence,
    EvidenceKind,
    HypothesisDraft,
    IssueKind,
    ReviewResult,
)


class UnsupportedReference(Exception):
    """A citation to evidence that doesn't exist, or belongs to another incident (Q9.28, Q9.29)."""


class UnsafeOutput(Exception):
    """Model output with a secret, link or command (ADR-59). It counts as invalid output, so
    it is retried. The message names the kind only, never the matched text."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class CitationReport:
    claims: int
    supported: int
    unknown_ids: list[UUID]
    foreign_ids: list[UUID]

    @property
    def supported_ratio(self) -> float:
        return self.supported / self.claims if self.claims else 0.0


def _supports(ev: Evidence) -> bool:
    """Gaps, flagged records and retrieved guidance support nothing (Q9.27, ADR-59, Artifact 10)."""
    return ev.kind not in NO_TIER_KINDS and not ev.flagged_injection


def _claim_supported(c: Claim, evidence: dict[UUID, Evidence]) -> bool:
    return any(e in evidence and _supports(evidence[e]) for e in c.evidence_ids)


def check_citations(
    draft: DiagnosisDraft, evidence: dict[UUID, Evidence], incident_id: UUID
) -> CitationReport:
    """Every cited ID must exist and belong to this incident. Raises on any violation:
    a draft with an invented or foreign reference is rejected whole and counts as invalid output.
    A claim is supported if at least one of its citations is a valid record that supports."""
    unknown, foreign, claims, supported = [], [], 0, 0
    for h in draft.hypotheses:
        for c in h.claims:
            claims += 1
            for eid in c.evidence_ids:
                ev = evidence.get(eid)
                if ev is None:
                    unknown.append(eid)
                elif ev.incident_id != incident_id:
                    foreign.append(eid)
            supported += _claim_supported(c, evidence)
        for eid in h.contradicting_evidence_ids:
            ev = evidence.get(eid)
            if ev is None:
                unknown.append(eid)
            elif ev.incident_id != incident_id:
                foreign.append(eid)
    report = CitationReport(claims, supported, unknown, foreign)
    if unknown or foreign:
        raise UnsupportedReference(f"unknown={len(unknown)} foreign={len(foreign)}")
    return report


def recheck_fingerprints(
    cited: list[Evidence], fetch_sha: Callable[[Evidence], Optional[str]]
) -> list[UUID]:
    """Q9.22: re-read the source's fingerprint for cited evidence. Returns IDs whose source changed
    (None from fetch_sha means the source can no longer be read, which also counts as changed)."""
    return [e.evidence_id for e in cited if fetch_sha(e) != e.content_sha256]


@dataclass(frozen=True)
class ObservationFacts:
    """What code computed about one record. Set by the Evidence Collector or Test Runner from
    the data, never by the model. One field per "Assigned when" rule in 9.7 (provisional)."""

    fails_with_change_clears_without: bool = False  # sandbox reproduction
    only_new_version_failing: bool = False  # error_rate_by_version
    changed_key_on_failing_path: bool = False  # a get_changes key, per a log or trace
    frames_in_changed_file: bool = False  # trace or error-group frames
    first_seen_after_change: bool = False  # error group
    failures_grow_with_rollout: bool = False  # metric
    rollback_and_recovery: bool = False  # change history, then http_5xx_rate
    linked_by_time: bool = False  # any record tied to the change by time only


# (tier, fact, kinds it may be assigned to), strongest first (ADR-53)
_TIER_RULES = [
    (1, "fails_with_change_clears_without", {EvidenceKind.test_result}),
    (2, "only_new_version_failing", {EvidenceKind.metric}),
    (3, "changed_key_on_failing_path", {EvidenceKind.log, EvidenceKind.trace}),
    (4, "frames_in_changed_file", {EvidenceKind.trace, EvidenceKind.error_group}),
    (5, "first_seen_after_change", {EvidenceKind.error_group}),
    (6, "failures_grow_with_rollout", {EvidenceKind.metric}),
    (7, "rollback_and_recovery", {EvidenceKind.change, EvidenceKind.metric}),
]


def assign_tier(kind: EvidenceKind, facts: ObservationFacts) -> Optional[int]:
    """The strength_tier of one record: its strongest matching rule, 8 for a link by time
    alone, None if nothing matches. Gaps and retrieved guidance never get a tier."""
    if kind in NO_TIER_KINDS:
        return None
    for tier, fact, kinds in _TIER_RULES:
        if kind in kinds and getattr(facts, fact):
            return tier
    return 8 if facts.linked_by_time else None


def missing_required_sources(evidence: Iterable[Evidence]) -> list[str]:
    """ADR-54: the alert, the change history, and logs or the error tracker. A gap record has
    kind gap, so a failed source counts as missing here. Returns the sources not collected."""
    kinds = {e.kind for e in evidence}
    missing = []
    if EvidenceKind.alert not in kinds:
        missing.append("alert")
    if EvidenceKind.change not in kinds:
        missing.append("change history")
    if not kinds & {EvidenceKind.log, EvidenceKind.error_group}:
        missing.append("logs or error tracker")
    return missing


def _cited(h: HypothesisDraft, evidence: dict[UUID, Evidence]) -> list[Evidence]:
    ids = {e for c in h.claims for e in c.evidence_ids}
    return [evidence[e] for e in ids if e in evidence]


def _strength(h: HypothesisDraft, evidence: dict[UUID, Evidence]) -> Confidence:
    """The level the cited observations reach on their own, before contradictions and gaps."""
    seen = [e for e in _cited(h, evidence) if _supports(e) and e.strength_tier is not None]
    if any(e.strength_tier <= 3 for e in seen):
        return Confidence.high
    independent = {e.source_system for e in seen if 5 <= e.strength_tier <= 7}
    if any(e.strength_tier == 4 for e in seen) or len(independent) >= 2:
        return Confidence.medium
    return Confidence.low


def _contradicting(h: HypothesisDraft, evidence: dict[UUID, Evidence]) -> list[UUID]:
    """Observations against h. Gaps, retrieved guidance and timing alone don't count."""
    return [
        e
        for e in h.contradicting_evidence_ids
        if e in evidence
        and evidence[e].kind not in NO_TIER_KINDS
        and evidence[e].strength_tier != 8
    ]


def score_confidence(
    h: HypothesisDraft, evidence: dict[UUID, Evidence], gaps: int, required_missing: list[str]
) -> Confidence:
    """ADR-53 levels. A missing required source (ADR-54) or a contradiction gives low; any
    other gap lowers high to medium."""
    level = _strength(h, evidence)
    if required_missing or _contradicting(h, evidence):
        return Confidence.low
    if gaps and level == Confidence.high:
        return Confidence.medium
    return level


def code_checks(
    draft: DiagnosisDraft,
    evidence: dict[UUID, Evidence],
    incident_id: UUID,
    gaps: int,
    changed: list[UUID],
) -> list[CriticIssue]:
    """Deterministic review that runs before the model critic; its blocking issues can't be overruled."""
    issues: list[CriticIssue] = []
    if draft.outcome != "established":
        return issues
    claims = [c for h in draft.hypotheses for c in h.claims]  # claim_index counts across the draft
    for i, c in enumerate(claims):
        if not _claim_supported(c, evidence):
            issues.append(
                CriticIssue(
                    kind=IssueKind.unsupported_claim,
                    detail="claim has no valid supporting evidence",
                    claim_index=i,
                    blocking=True,
                )
            )
    lead = draft.hypotheses[draft.leading_index]
    deployment = lead.category.startswith("deployment")
    if deployment and _strength(lead, evidence) == Confidence.low:
        issues.append(
            CriticIssue(
                kind=IssueKind.correlation_only,
                detail="the observations cited don't reach the medium bar for causation",
                blocking=True,
            )
        )
    against = _contradicting(lead, evidence)
    if against:
        issues.append(
            CriticIssue(
                kind=IssueKind.contradicted,
                detail="evidence contradicts the leading hypothesis",
                evidence_ids=against,
                blocking=True,
            )
        )
    if len(draft.ruled_out) < len(draft.hypotheses) - 1:
        issues.append(
            CriticIssue(
                kind=IssueKind.alternative_not_ruled_out,
                detail="every alternative needs a reason it was set aside",
                blocking=True,
            )
        )
    cited = {e for c in lead.claims for e in c.evidence_ids}
    stale = [e for e in changed if e in cited]
    if stale:
        issues.append(
            CriticIssue(
                kind=IssueKind.stale_evidence,
                detail="cited evidence changed since collection",
                evidence_ids=stale,
                blocking=True,
            )
        )
    if draft.proposed_action is not None and not (
        deployment and any(e.tool == "get_changes" for e in _cited(lead, evidence))
    ):
        issues.append(
            CriticIssue(
                kind=IssueKind.scope,
                blocking=True,
                detail="an action needs a deployment cause and cited get_changes evidence",
            )
        )
    return issues


# ADR-59. All three lists are provisional: each wider form can reject ordinary text, and the first
# evaluation run decides how far they go. T2.10 (ADR-72) decided the two cases known to reject real
# text: a bare host with a path (`registry.internal/app`) stays a link in anything the model
# writes, and is allowed only in file content a patch restores from an earlier revision
# (CONTENT_FIELDS); a four-part version (`10.2.1.3`) is a release, not an address, in the error
# tracker's `release` parameter (tool_registry.ReleaseText). The secret patterns are telemetry.SECRET_PATTERNS'
# credential rows (Layer 12, tested equal); its e-mail, card and address rows would match ordinary IDs.
_SECRET_PATTERNS = [
    re.compile(
        r"""(?i)(?:bearer|token|api[_-]?key|access[_-]?key|private[_-]?key|secret|password|passwd|pwd)"""
        r"""(?:[_-](?:access[_-])?(?:key|value))?["']?\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s"',;]+)"""
    ),
    re.compile(
        r"""(?i)\bauthorization["']?\s*[:=]\s*["']?(?:bearer|basic|digest|token)\s+[^\s"',;]+"""
    ),
    re.compile(
        r"(?i)\bbearer\s+(?=[\w.~+/-]*\d)[\w.~+/-]{16,}=*"
    ),  # needs a digit: "bearer authentication" is prose
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+"),  # a JWT
    re.compile(
        r"(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)"
    ),  # with its body
]
_LINK = re.compile(
    r"(?i)\b[a-z][a-z0-9+.-]*://|\bwww\."
    r"|(?<![\w:/.-])//[\w-]+(?:\.[\w-]+)+"  # scheme-relative
    r"|(?<![\w/.@-])(?:[a-z0-9-]+\.)+[a-z]{2,}/\S*"  # a bare host with a path, not a/b/file.yaml
    r"|\b(?:javascript|vbscript):\S|\bdata:[a-z]+/[\w.+-]+[;,]"
)
_COMMAND = re.compile(
    r"(?im)^\s*\$\s"
    r"|\b(sudo\s+\S|rm\s+-[rf]|curl\s+\S|wget\s+\S|kubectl\s+\S|psql\s+\S|bash\s+-c|sh\s+-c|chmod\s+\S|docker\s+(run|exec)\b"
    r"|terraform\s+(destroy|apply)\b|helm\s+(uninstall|delete)\b|systemctl\s+(stop|disable|mask|restart)\b"
    r"|(python\d?|perl|ruby|node)\s+-[ce]\b)"
    r"|\b(drop\s+(table|database|schema)|delete\s+from|truncate\s+table|update\s+\S+\s+set\s+\S+\s*=)"
)


# File content a patch proposes (agent_contracts.PatchFileEdit.new_content). Most of it is the
# repository's own text, which may name hosts and URLs (`PAYMENTS_URL=http://payments.internal`),
# so the gateway scans it for secrets only; once code has diffed it against the base, the lines it
# adds are scanned for links and commands too (check_added_lines), except the lines of a file
# check_paths verified as an exact restoration of an earlier revision (T2.10, ADR-72).
CONTENT_FIELDS = frozenset({"new_content"})


def _strings(value, content: bool = False) -> Iterable[tuple[str, bool]]:
    """Every string, and whether it is file content (CONTENT_FIELDS)."""
    if isinstance(value, str):
        yield value, content
    elif isinstance(value, dict):
        for k, v in value.items():  # keys too: a secret can sit in a key
            inner = content or k in CONTENT_FIELDS
            yield str(k), content
            if isinstance(v, str):
                yield f"{k}: {v}", inner  # a secret split across a key and its value
            yield from _strings(v, inner)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v, content)


def check_output(output: BaseModel) -> None:
    """Scan every string in one model output. Raises UnsafeOutput on a secret, link or command.
    File content is scanned for secrets here and line by line later (check_added_lines)."""
    for text, content in _strings(output.model_dump(mode="json")):
        if any(p.search(text) for p in _SECRET_PATTERNS):
            raise UnsafeOutput("secret")
        if content:
            continue
        if _LINK.search(text):
            raise UnsafeOutput("link")
        if _COMMAND.search(text):
            raise UnsafeOutput("command")


def check_added_lines(lines: Iterable[str]) -> None:
    """The lines a patch adds that no earlier revision had: secrets, links and commands, as in
    any model text. Raises UnsafeOutput."""
    for line in lines:
        if any(p.search(line) for p in _SECRET_PATTERNS):
            raise UnsafeOutput("secret")
        if _LINK.search(line):
            raise UnsafeOutput("link")
        if _COMMAND.search(line):
            raise UnsafeOutput("command")


# ST-33: text in evidence that addresses the reader instead of describing the system. Provisional,
# like the lists above. A hit only flags the record: it stays readable, and supports no claim.
_INJECTION = re.compile(
    r"(?is)\b(ignore|disregard|forget|override)\b.{0,40}\b(instructions?|rules|prompts?|guidance|guardrails)\b"
    r"|\byou are now\b|\bnew instructions?\b|\bas an? (ai|assistant|language model)\b"
    r"|\b(skip|bypass|without|no need for|don'?t wait for)\b.{0,30}\b(approvals?|approvers?|review|verification)\b"
    r"|\b(hide|conceal|suppress|do not (report|mention|show)|don'?t (report|mention|show))\b.{0,40}"
    r"\b(errors?|failures?|alerts?|incidents?|evidence)\b"
    r"|\b(run|execute)\b.{0,20}`[^`\n]+`"
    r"|\bmark (this|the) (incident|alert|diagnosis) as (resolved|recovered|closed|established)\b"
    r"|<\s*/?\s*(system|instructions?)\s*>"
)


def flags_injection(text: str) -> bool:
    """Deterministic, and run on every extract at evidence normalisation, before storage (ST-33)."""
    return bool(_INJECTION.search(text))


def flag_injection(ev: Evidence) -> Evidence:
    """The normalisation step itself: the record as it is stored. A flagged record supports nothing
    (_supports) and is shown to the approver as flagged."""
    return ev.model_copy(update={"flagged_injection": True}) if flags_injection(ev.summary) else ev


def mask_secrets(text: str) -> str:
    """For any copy of failed output stored for audit (ADR-59)."""
    for p in _SECRET_PATTERNS:
        text = p.sub("[MASKED]", text)
    return text


_ORDER = [Confidence.low, Confidence.medium, Confidence.high]


def combine(
    model_verdict: CriticVerdict, issues: list[CriticIssue], scored: Confidence
) -> ReviewResult:
    """Code issues go ahead of the critic's, so the 20-issue limit can't drop them; a blocking
    code issue turns 'accept' into 'revise'. Final confidence is the lower of the scored level
    and the critic's confidence_cap (ADR-53)."""
    all_issues = issues + list(model_verdict.issues)
    verdict = model_verdict.verdict
    if verdict == "accept" and any(i.blocking for i in issues):
        verdict = "revise"
    final = min(scored, model_verdict.confidence_cap, key=_ORDER.index)
    return ReviewResult(
        verdict=CriticVerdict(
            verdict=verdict,
            issues=all_issues[:20],
            evidence_requests=model_verdict.evidence_requests,
            confidence_cap=model_verdict.confidence_cap,
        ),
        confidence=final,
    )


def fingerprint(draft: DiagnosisDraft) -> str:
    """Stable fingerprint of what a diagnosis claims, for detecting no-change revisions."""
    lead = draft.hypotheses[draft.leading_index] if draft.leading_index is not None else None
    core = {
        "outcome": draft.outcome,
        "lead": lead.statement.lower() if lead else None,
        "cited": sorted(str(e) for c in (lead.claims if lead else []) for e in c.evidence_ids),
        "action": draft.proposed_action,
    }
    return hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()
