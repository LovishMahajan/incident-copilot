"""Artifact 7.2, 9.7, 9.8, 13.5 T1.14, T1.15 (FR-12, ADR-69): diagnoses against PostgreSQL.

The real Evidence Collector over the fake monitoring source (in-process), then the real
hypothesise, diagnose and review_diagnosis bodies over PgSkillStore as aisre_app, with the
Model Gateway answering from the fake provider's scripts in-process. Checks the `diagnoses`,
`hypotheses` and `hypothesis_evidence` rows, the review written once, the timeline events, and
the diagnosis read the API serves.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from monitoring_scenarios import CUT_OFF, FIXTURES, copy_reference, serving, sources
from skill_rig import NOW, ROOT, ScriptedTransport, gateway
from sqlalchemy import func, text, update

from aisre.agent.agent_contracts import Confidence
from aisre.agent.skills import reasoning
from aisre.agent.skills.activities import ModelSkills
from aisre.agent.skills.store import PgSkillStore
from aisre.contracts import IncidentCreate
from aisre.record import create_incident, uuid7
from aisre.record import diagnoses as rec
from aisre.record.reads import current_diagnosis
from aisre.record.tables import outbox
from aisre.workflow import activities as act
from aisre.workflow.record_activities import EvidenceActivities

WITHHELD = ("release.json", "config-diff.txt")


@pytest.fixture
def incident(app_engine, add_service):
    org_id = uuid7()
    add_service(org_id, "orders-api")
    alert = json.loads((FIXTURES / "reference-incident/alert.json").read_text())
    with app_engine.begin() as conn:
        intake = create_incident(
            conn,
            org_id=org_id,
            caller_id="monitoring",
            body=IncidentCreate.model_validate(alert["incident_create"]),
            grouping_window=timedelta(0),
        )
        # Kept from the Compose relay, so its worker never runs this incident.
        conn.execute(
            update(outbox)
            .where(outbox.c.incident_id == intake.incident_id)
            .values(delivered_at=func.now())
        )
    return intake.incident_id


def baseline(engine, incident_id, root=FIXTURES) -> list[str]:
    with serving(root) as url:
        acts = EvidenceActivities(engine, sources(url, replay_as_of=CUT_OFF.isoformat()))
        out = asyncio.run(acts.collect_evidence(act.EvidenceIn(str(incident_id), "c:1")))
    return out.output.evidence_ids


def run_skills(engine, incident_id, ids) -> dict[str, Any]:
    skills = ModelSkills(
        gateway(ScriptedTransport()),
        reasoning.load_prompts(ROOT / "prompts"),
        PgSkillStore(engine, clock=lambda: datetime.now(UTC)),
        clock=lambda: NOW,
    )
    i = str(incident_id)
    hyps = asyncio.run(skills.hypothesise(act.SkillIn(i, "s:2", evidence_ids=ids))).output
    diag = asyncio.run(skills.diagnose(act.SkillIn(i, "s:3", evidence_ids=ids, hypotheses=hyps)))
    out: dict[str, Any] = {"skills": skills, "hyps": hyps, "diagnose": diag}
    if diag.output.draft.outcome == "established":
        review_in = act.SkillIn(
            i,
            "s:4",
            evidence_ids=ids,
            diagnosis=diag.output.draft,
            diagnosis_id=str(diag.output.diagnosis_id),
        )
        out["review"] = asyncio.run(skills.review_diagnosis(review_in))
        out["review_in"] = review_in
    return out


def rows(engine, sql: str, incident_id: UUID) -> list[Any]:
    with engine.connect() as conn:
        return conn.execute(text(sql), {"i": incident_id}).mappings().all()


def test_reference_diagnosis_is_stored_with_its_hypotheses_links_and_review(app_engine, incident):
    ids = baseline(app_engine, incident)
    out = run_skills(app_engine, incident, ids)
    assert out["review"].output.verdict.verdict == "accept"
    (d,) = rows(app_engine, "SELECT * FROM diagnoses WHERE incident_id = :i", incident)
    assert (d["revision"], d["outcome"], d["confidence"]) == (1, "established", "high")
    assert (d["reviewed"], d["final_outcome"], d["final_confidence"]) == (
        True,
        "established",
        "high",
    )
    assert d["category"] == "deployment_config" and d["produced_by"] == "diagnose:diagnose@1"
    assert any("ORDER_TAX_RATE_BPS" in c["text"] for c in d["claims"])
    hyps = rows(
        app_engine, "SELECT * FROM hypotheses WHERE incident_id = :i ORDER BY ordinal", incident
    )
    assert [h["ordinal"] for h in hyps] == [1, 2, 3]
    assert [h["status"] for h in hyps] == ["supported", "ruled_out", "ruled_out"]
    assert d["leading_hypothesis_id"] == hyps[0]["hypothesis_id"]
    links = rows(
        app_engine,
        "SELECT l.relation, e.kind FROM hypothesis_evidence l JOIN hypotheses h USING "
        "(hypothesis_id) JOIN evidence e USING (evidence_id) WHERE h.incident_id = :i",
        incident,
    )
    assert {("supports", "change"), ("supports", "metric"), ("contradicts", "metric")} <= {
        (link["relation"], link["kind"]) for link in links
    }
    types = [
        r["type"]
        for r in rows(
            app_engine,
            "SELECT type FROM timeline_events WHERE incident_id = :i ORDER BY seq",
            incident,
        )
    ]
    assert types[-5:] == ["aisre.hypothesis.created.v1"] * 3 + [
        "aisre.diagnosis.produced.v1",
        "aisre.diagnosis.reviewed.v1",
    ]
    with app_engine.connect() as conn:
        shown = current_diagnosis(conn, incident)
    assert shown.final_outcome == "established" and len(shown.hypotheses) == 3
    assert {link.relation.value for link in shown.hypotheses[0].evidence} == {"supports"}

    # A retried step writes nothing twice: same diagnosis ID, same review
    again = asyncio.run(
        out["skills"].diagnose(
            act.SkillIn(str(incident), "s:3", evidence_ids=ids, hypotheses=out["hyps"])
        )
    )
    assert again.output.diagnosis_id == out["diagnose"].output.diagnosis_id
    asyncio.run(out["skills"].review_diagnosis(out["review_in"]))
    assert len(rows(app_engine, "SELECT 1 FROM diagnoses WHERE incident_id = :i", incident)) == 1
    assert len(rows(app_engine, "SELECT 1 FROM hypotheses WHERE incident_id = :i", incident)) == 3
    reviewed = rows(
        app_engine,
        "SELECT 1 FROM timeline_events WHERE incident_id = :i "
        "AND type = 'aisre.diagnosis.reviewed.v1'",
        incident,
    )
    assert len(reviewed) == 1


def test_honesty_variant_is_stored_inconclusive_with_what_is_missing(
    app_engine, incident, tmp_path
):
    ids = baseline(app_engine, incident, copy_reference(tmp_path, without=WITHHELD))
    out = run_skills(app_engine, incident, ids)
    assert "review" not in out
    (d,) = rows(app_engine, "SELECT * FROM diagnoses WHERE incident_id = :i", incident)
    assert (d["outcome"], d["leading_hypothesis_id"], d["confidence"]) == (
        "inconclusive",
        None,
        "low",
    )
    assert d["summary"].startswith("Root cause not established")
    assert d["stop_reason"] == rec.UNDECIDED
    assert any("Change history" in m for m in d["missing_evidence"])
    assert d["final_outcome"] is None and not d["reviewed"]
    hyps = rows(app_engine, "SELECT status FROM hypotheses WHERE incident_id = :i", incident)
    assert {h["status"] for h in hyps} == {"open"}


def test_an_established_draft_that_scores_low_is_stored_inconclusive(app_engine, incident):
    ids = baseline(app_engine, incident)
    out = run_skills(app_engine, incident, ids)
    draft = out["diagnose"].output.draft
    with app_engine.begin() as conn:
        revision = rec.store_diagnosis(
            conn,
            diagnosis_id=uuid7(),
            incident_id=incident,
            draft=draft,
            confidence=Confidence.low,
            hypothesis_confidence=[Confidence.low] * len(draft.hypotheses),
            produced_by="test",
            now=datetime.now(UTC),
        )
    assert revision == 2
    d = rows(
        app_engine, "SELECT * FROM diagnoses WHERE incident_id = :i AND revision = 2", incident
    )[0]
    assert (d["outcome"], d["leading_hypothesis_id"]) == ("inconclusive", None)
    assert d["stop_reason"] == rec.LOW_NOT_ESTABLISHED
    ordinals = rows(app_engine, "SELECT ordinal FROM hypotheses WHERE incident_id = :i", incident)
    assert sorted(o["ordinal"] for o in ordinals) == [1, 2, 3, 4, 5, 6]
