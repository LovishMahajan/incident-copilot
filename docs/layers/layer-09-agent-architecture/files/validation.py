"""Artifact 9.7–9.8: the checks code runs on every model output, before and beside the critic.

These run in activities (they read the database). The model's word is never accepted on
its own: citations must exist, belong to the incident and still match their fingerprint;
confidence comes from evidence strength, not from the model.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Callable, Optional
from uuid import UUID

from agent_contracts import (Confidence, CriticIssue, CriticVerdict, DiagnosisDraft, Evidence,
                             EvidenceKind, HypothesisDraft, IssueKind)


class UnsupportedReference(Exception):
    """A citation to evidence that doesn't exist, or belongs to another incident (Q9.28, Q9.29)."""


@dataclass(frozen=True)
class CitationReport:
    claims: int
    supported: int
    unknown_ids: list[UUID]
    foreign_ids: list[UUID]

    @property
    def supported_ratio(self) -> float:
        return self.supported / self.claims if self.claims else 0.0


def check_citations(draft: DiagnosisDraft, evidence: dict[UUID, Evidence], incident_id: UUID) -> CitationReport:
    """Every cited ID must exist and belong to this incident. Raises on any violation:
    a draft with an invented or foreign reference is rejected whole and counts as invalid output."""
    unknown, foreign, claims, supported = [], [], 0, 0
    for h in draft.hypotheses:
        for c in h.claims:
            claims += 1
            ok = True
            for eid in c.evidence_ids:
                ev = evidence.get(eid)
                if ev is None:
                    unknown.append(eid); ok = False
                elif ev.incident_id != incident_id:
                    foreign.append(eid); ok = False
                elif ev.kind == EvidenceKind.gap:
                    ok = False                       # a gap supports nothing
            supported += ok
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


def recheck_fingerprints(cited: list[Evidence], fetch_sha: Callable[[Evidence], Optional[str]]) -> list[UUID]:
    """Q9.22: re-read the source's fingerprint for cited evidence. Returns IDs whose source changed
    (None from fetch_sha means the source can no longer be read, which also counts as changed)."""
    return [e.evidence_id for e in cited if fetch_sha(e) != e.content_sha256]


# Evidence strength, strongest first (Artifact 3). Used for confidence, not the model's tone.
STRENGTH = {EvidenceKind.test_result: 5, EvidenceKind.trace: 4, EvidenceKind.error_group: 4,
            EvidenceKind.metric: 3, EvidenceKind.log: 3, EvidenceKind.dependency: 3,
            EvidenceKind.change: 1, EvidenceKind.alert: 1, EvidenceKind.past_incident: 1,
            EvidenceKind.runbook: 0, EvidenceKind.gap: 0}


def score_confidence(h: HypothesisDraft, evidence: dict[UUID, Evidence], gaps: int) -> Confidence:
    kinds = {evidence[e].kind for c in h.claims for e in c.evidence_ids if e in evidence}
    best = max((STRENGTH[k] for k in kinds), default=0)
    contradicted = any(STRENGTH[evidence[e].kind] >= 3 for e in h.contradicting_evidence_ids if e in evidence)
    if contradicted or best <= 1:
        return Confidence.low                     # timing or change records alone never exceed low
    level = Confidence.high if best >= 4 and len(kinds) >= 3 else Confidence.medium
    if gaps and level == Confidence.high:
        level = Confidence.medium                 # missing sources lower confidence
    return level


def code_checks(draft: DiagnosisDraft, evidence: dict[UUID, Evidence], incident_id: UUID,
                service: str, gaps: int, changed: list[UUID]) -> list[CriticIssue]:
    """Deterministic review that runs before the model critic; its blocking issues can't be overruled."""
    issues: list[CriticIssue] = []
    if draft.outcome != "established":
        return issues
    lead = draft.hypotheses[draft.leading_index]
    for i, c in enumerate(lead.claims):
        if all(evidence[e].kind == EvidenceKind.gap for e in c.evidence_ids if e in evidence):
            issues.append(CriticIssue(kind=IssueKind.unsupported_claim, detail="claim cites only gaps",
                                      claim_index=i, blocking=True))
    kinds = {evidence[e].kind for c in lead.claims for e in c.evidence_ids if e in evidence}
    if lead.category.startswith("deployment") and kinds <= {EvidenceKind.change, EvidenceKind.alert}:
        issues.append(CriticIssue(kind=IssueKind.correlation_only,
                                  detail="only the change record and timing link the deployment to the failure",
                                  blocking=True))
    strong_against = [e for e in lead.contradicting_evidence_ids if e in evidence and STRENGTH[evidence[e].kind] >= 3]
    if strong_against:
        issues.append(CriticIssue(kind=IssueKind.contradicted, detail="strong evidence contradicts the leading hypothesis",
                                  evidence_ids=strong_against, blocking=True))
    if len(draft.ruled_out) < len(draft.hypotheses) - 1:
        issues.append(CriticIssue(kind=IssueKind.alternative_not_ruled_out,
                                  detail="every alternative needs a reason it was set aside", blocking=True))
    cited = {e for c in lead.claims for e in c.evidence_ids}
    stale = [e for e in changed if e in cited]
    if stale:
        issues.append(CriticIssue(kind=IssueKind.stale_evidence, detail="cited evidence changed since collection",
                                  evidence_ids=stale, blocking=True))
    return issues


def combine(model_verdict: CriticVerdict, issues: list[CriticIssue]) -> CriticVerdict:
    """Code issues are added to the critic's; a blocking code issue turns 'accept' into 'revise'."""
    all_issues = list(model_verdict.issues) + issues
    verdict = model_verdict.verdict
    if verdict == "accept" and any(i.blocking for i in issues):
        verdict = "revise"
    return CriticVerdict(verdict=verdict, issues=all_issues[:20],
                         evidence_requests=model_verdict.evidence_requests,
                         confidence_cap=model_verdict.confidence_cap)


def fingerprint(draft: DiagnosisDraft) -> str:
    """Stable fingerprint of what a diagnosis claims, for detecting no-change revisions."""
    lead = draft.hypotheses[draft.leading_index] if draft.leading_index is not None else None
    core = {"outcome": draft.outcome,
            "lead": lead.statement.lower() if lead else None,
            "cited": sorted(str(e) for c in (lead.claims if lead else []) for e in c.evidence_ids),
            "action": draft.proposed_action}
    return hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()
