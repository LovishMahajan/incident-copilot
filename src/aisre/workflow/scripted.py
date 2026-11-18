"""Artifact 13.4 M0, 13.5 T0.11: scripted results for the activities M0 doesn't build.

DEVELOPMENT AND TEST ONLY. M0 proves the path from an alert to a durable workflow with no model
at all (ADR-68): these activities answer with fixed, deterministic results, and touch no
network, no model and no database. The worker registers them only when
`Settings.scripted_skills` is true, which `Settings` refuses when `AISRE_ENV` is production, and
the worker checks again (aisre.workflow.worker). Each later milestone replaces one of them with
the real activity: evidence and reasoning at M1, the patch at M2.

The script, for any incident: the slot is granted at once; spend allows model steps;
baseline evidence and knowledge each return fixed IDs; two hypotheses (one not blaming the
deployment, as HypothesesOut requires) with no evidence requests; an established diagnosis the
critic accepts at high confidence, so the coordinator finishes `established`; then
`draft_patch` answers `no_allowed_fix`, because nothing is drafted before M2. The workflow
therefore goes open -> investigating -> diagnosed -> handed_over, with the real
record_transition and hand_over writing each step. Notifications are logged, not sent.

The texts say they are scripted, so no one reads them as findings. Nothing here comes from the
reference incident's answer key, which the platform never sees.

Activities M0 never reaches (sandbox, approval, verification, dispatch, reconciliation) are not
here: they stay the interfaces in activities.py, and dispatcher-queue activities are not
registered at all.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid5

from temporalio import activity

from aisre.agent.agent_contracts import (
    Claim,
    Confidence,
    CriticVerdict,
    DiagnosisDraft,
    DiagnosisResult,
    HypothesesOut,
    HypothesisDraft,
    KnowledgeOut,
    ReviewResult,
    SkillFailure,
    SkillResult,
    SkillUsage,
)
from aisre.workflow import activities as act

log = logging.getLogger(__name__)

SCRIPT_NAMESPACE = UUID("2c7d4b1e-9a3f-5e60-8b17-4d2a6f9c0e85")
NO_USAGE = SkillUsage()  # no model call, no tool call, no spend
NOT_DRAFTED = "scripted M0 run: no patch is drafted before M2"
SCRIPTED = "Scripted M0 result, no model ran"
MAX_STEP_SECONDS = 10.0  # below the 20 s heartbeat timeout of the skill steps (8.5)
_step_seconds = 0.0  # set once by scripted_activities(), before the worker starts


def scripted_activities(step_seconds: float = 0.0) -> list[Callable[..., Any]]:
    """The scripted activities, each skill step taking `step_seconds` first. The pause is for
    the M0 demo only, so a worker kill lands mid-run; it is one setting for the process."""
    global _step_seconds
    if not 0 <= step_seconds <= MAX_STEP_SECONDS:
        raise ValueError(f"step_seconds must be 0 to {MAX_STEP_SECONDS}")
    _step_seconds = step_seconds
    return list(SCRIPTED_ACTIVITIES)


async def _pace() -> None:
    if _step_seconds:
        await asyncio.sleep(_step_seconds)


def scripted_id(incident_id: str, name: str) -> UUID:
    """A stable ID per incident and name: the same input always gives the same result."""
    return uuid5(SCRIPT_NAMESPACE, f"{incident_id}:{name}")


def _hypotheses(incident_id: str) -> list[HypothesisDraft]:
    cite = [scripted_id(incident_id, "evidence:baseline:1")]
    return [
        HypothesisDraft(
            statement=f"{SCRIPTED}: a recent change caused the alert",
            category="deployment_config",
            claims=[Claim(text=f"{SCRIPTED}: placeholder claim", evidence_ids=cite)],
            confirm_test="scripted: none",
            refute_test="scripted: none",
        ),
        HypothesisDraft(
            statement=f"{SCRIPTED}: a dependency caused the alert",
            category="dependency",
            claims=[Claim(text=f"{SCRIPTED}: placeholder claim", evidence_ids=cite)],
            confirm_test="scripted: none",
            refute_test="scripted: none",
        ),
    ]


@activity.defn(name="acquire_investigation_slot")
async def acquire_investigation_slot(inp: act.SlotIn) -> None:
    return None  # granted at once; M0 has no queue


@activity.defn(name="read_spend_state")
async def read_spend_state(inp: act.IncidentIn) -> act.SpendOut:
    return act.SpendOut(evidence_only=False)


@activity.defn(name="notify")
async def notify(inp: act.NotifyIn) -> None:
    log.info("scripted notify %s for incident %s to %s", inp.kind, inp.incident_id, inp.audience)


@activity.defn(name="collect_evidence")
async def collect_evidence(inp: act.EvidenceIn) -> SkillResult[act.EvidenceOut]:
    await _pace()
    ids = [str(scripted_id(inp.incident_id, f"evidence:{inp.mode}:{n}")) for n in (1, 2)]
    return SkillResult(output=act.EvidenceOut(evidence_ids=ids, gap_count=0), usage=NO_USAGE)


@activity.defn(name="retrieve_knowledge")
async def retrieve_knowledge(inp: act.SkillIn) -> SkillResult[KnowledgeOut]:
    await _pace()
    out = KnowledgeOut(
        evidence_ids=[str(scripted_id(inp.incident_id, "knowledge:1"))],
        gap_count=0,
        index_revision="scripted",
    )
    return SkillResult(output=out, usage=NO_USAGE)


@activity.defn(name="hypothesise")
async def hypothesise(inp: act.SkillIn) -> SkillResult[HypothesesOut]:
    await _pace()
    out = HypothesesOut(hypotheses=_hypotheses(inp.incident_id))
    return SkillResult(output=out, usage=NO_USAGE)


@activity.defn(name="diagnose")
async def diagnose(inp: act.SkillIn) -> SkillResult[DiagnosisResult]:
    await _pace()
    draft = DiagnosisDraft(
        outcome="established",
        leading_index=0,
        hypotheses=_hypotheses(inp.incident_id),
        summary=f"{SCRIPTED}. The workflow path is real; this diagnosis is not.",
        proposed_action="config_revert",
    )
    out = DiagnosisResult(
        diagnosis_id=scripted_id(inp.incident_id, "diagnosis:1"),
        draft=draft,
        fingerprint=scripted_id(inp.incident_id, "fingerprint").hex * 2,
        supported_ratio=1.0,
    )
    return SkillResult(output=out, usage=NO_USAGE)


@activity.defn(name="review_diagnosis")
async def review_diagnosis(inp: act.SkillIn) -> SkillResult[ReviewResult]:
    await _pace()
    out = ReviewResult(
        verdict=CriticVerdict(verdict="accept", confidence_cap=Confidence.high),
        confidence=Confidence.high,
    )
    return SkillResult(output=out, usage=NO_USAGE)


@activity.defn(name="draft_patch")
async def draft_patch(inp: act.SkillIn) -> SkillResult[act.PatchOut]:
    await _pace()
    failure = SkillFailure(skill="draft_patch", code="no_allowed_fix", detail=NOT_DRAFTED)
    return SkillResult(failure=failure, usage=NO_USAGE)


SCRIPTED_ACTIVITIES: list[Callable[..., Any]] = [
    acquire_investigation_slot,
    read_spend_state,
    notify,
    collect_evidence,
    retrieve_knowledge,
    hypothesise,
    diagnose,
    review_diagnosis,
    draft_patch,
]
