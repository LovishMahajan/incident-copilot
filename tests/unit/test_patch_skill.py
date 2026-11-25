"""Artifact 13.5 T2.8: the `draft_patch` skill and the review system, offline.

The reference incident's evidence and diagnosis come from the skill rig (the real collector, the
real Model Gateway over the fake provider's scripts). The review system is Compose's fake
(ops/fakes/review.py), in-process, holding the reference repository built by build_repo.py, read
through the real client. FR-17 to FR-20, FR-40, FR-47, FR-48.
"""

from __future__ import annotations

import importlib.util
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import SecretStr
from skill_rig import ScriptedTransport, collected, investigate, run, skill_in, skills

from aisre.agent.agent_contracts import DiagnosisDraft
from aisre.agent.skills.patching import PatchSkill, candidate_kind
from aisre.gateway.adapters import HttpResponse
from aisre.record.patches import NewPatch, Stored
from aisre.remediation.patch import Patch, check_boundaries
from aisre.remediation.review import DraftPullRequest, ReviewClient
from aisre.remediation.sandbox import refuse_protected

ROOT = Path(__file__).parents[2]
ENV = "deploy/production.env"


def _fake_review():
    spec = importlib.util.spec_from_file_location("fake_review", ROOT / "ops/fakes/review.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fake_review = _fake_review()


@pytest.fixture(scope="module")
def review_url(tmp_path_factory):
    dest = tmp_path_factory.mktemp("review") / "orders-api"
    tags = fake_review.build(dest)
    server = fake_review.serve(fake_review.Review(fake_review.Repo(dest, "main")))
    yield f"http://127.0.0.1:{server.server_address[1]}", tags, dest
    server.shutdown()


@pytest.fixture(scope="module")
def reference():
    """The reference incident's evidence and its reviewed, established diagnosis."""
    inputs = collected()
    _, store, _ = investigate(inputs)
    ((diagnosis_id, saved),) = store.diagnoses.items()
    return inputs, diagnosis_id, saved["draft"]


class MemoryPatchStore:
    def __init__(self) -> None:
        self.saved: dict[UUID, NewPatch] = {}

    async def drafted(self, action_id: UUID) -> Stored | None:
        p = self.saved.get(action_id)
        return Stored(p.action_id, p.patch_sha256, p.kind, 900) if p else None

    async def save(self, patch: NewPatch) -> Stored:
        self.saved.setdefault(patch.action_id, patch)
        return Stored(patch.action_id, patch.patch_sha256, patch.kind, 900)


def drafting(reference, review_url, transport=None):
    inputs, _, _ = reference
    sk, _, transport = skills(inputs, transport)
    client = ReviewClient(review_url[0], SecretStr("fake-review-token"))
    store = MemoryPatchStore()
    return PatchSkill(sk, client, store), store, transport, client


def draft_in(reference, key="k:p1", candidate_no=1, diagnosis: DiagnosisDraft | None = None):
    inputs, diagnosis_id, draft = reference
    return skill_in(
        inputs,
        key,
        diagnosis=diagnosis or draft,
        diagnosis_id=str(diagnosis_id),
        candidate_no=candidate_no,
    )


def drafted_patch(store: MemoryPatchStore) -> tuple[NewPatch, Patch]:
    (saved,) = store.saved.values()
    return saved, Patch.model_validate_json(saved.review)


def answering(answer: dict[str, Any]):
    """A transport whose draft_patch answer is `answer`; the other steps keep their scripts."""

    def override(body: dict[str, Any]) -> HttpResponse | None:
        if "You draft one remediation candidate" not in body["system"]:
            return None
        msg = {
            "model": body["model"],
            "content": [{"type": "text", "text": json.dumps(answer)}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 10},
        }
        return HttpResponse(200, msg, {})

    return ScriptedTransport(override=override)


def scripted_answer() -> dict[str, Any]:
    script = json.loads(
        (ROOT / "ops/fakes/provider-scripts/reference-draft-patch.json").read_text()
    )
    return json.loads(json.dumps(script["answer"]))


def with_evidence(answer: dict[str, Any], inputs) -> dict[str, Any]:
    change = next(e for e in inputs.evidence if e.kind.value == "change")
    for f in answer["files"]:
        f["evidence_ids"] = [str(change.evidence_id)]
    return answer


# ---------------------------------------------------------------- the reference fix
def test_the_reference_fix_reverts_only_the_changed_value(reference, review_url):
    """FR-20: the diff changes exactly ORDER_TAX_RATE_BPS, back to 825, against cfg-42."""
    skill, store, transport, _ = drafting(reference, review_url)
    result = run(skill.draft_patch(draft_in(reference)))
    assert result.failure is None, result.failure
    saved, patch = drafted_patch(store)
    _, tags, _ = review_url
    assert (patch.base_revision, patch.reverted_commit) == (tags["cfg-42"], tags["cfg-42"])
    assert patch.repository == patch.target_service == "orders-api"  # FR-48
    changed = [line for f in patch.files for h in f.hunks for line in h.lines if line[:1] in "+-"]
    assert [f.path for f in patch.files] == [ENV]
    assert changed == ["-ORDER_TAX_RATE_BPS=8.25", "+ORDER_TAX_RATE_BPS=825"]
    assert result.output.patch_sha256 == patch.digest == saved.patch_sha256
    assert saved.canonical == patch.canonical_bytes()
    assert (saved.files_changed, saved.lines_added, saved.lines_removed) == (1, 1, 1)
    # Through the gateway with the prompt's alias (ADR-69), prompt v2 (ADR-63)
    (body,) = [b for b in transport.bodies if "You draft one" in b["system"]]
    assert body["model"] == "claude-sonnet-5-5"
    assert body["metadata"]["user_id"] == str(reference[0].context.incident_id)


def test_each_patch_carries_hypothesis_evidence_diff_effect_risk_and_undo(reference, review_url):
    """FR-19, as the action and the patch store them."""
    skill, store, _, _ = drafting(reference, review_url)
    run(skill.draft_patch(draft_in(reference)))
    saved, patch = drafted_patch(store)
    inputs, diagnosis_id, draft = reference
    lead = draft.hypotheses[draft.leading_index]
    assert lead.statement in saved.expected_effect and saved.diagnosis_id == diagnosis_id
    known = {str(e.evidence_id) for e in inputs.evidence}
    cited = {e for f in patch.files for h in f.hunks for e in h.evidence_ids}
    assert cited and cited <= known
    assert "-ORDER_TAX_RATE_BPS=8.25" in patch.unified_diff() and "# why:" in patch.unified_diff()
    for text in (saved.description, saved.expected_effect, saved.risk, saved.undo_plan):
        assert 10 <= len(text) <= 500
    assert saved.recovery_criteria and saved.kind == "config_revert"
    assert saved.policy_version.startswith("edit-policy@2|")


def test_the_draft_and_the_launcher_accept_the_same_restoration(reference, review_url):
    """ADR-72: the facts from the review system and from a mirror agree on the reference fix."""
    from aisre.remediation.sandbox import revert_facts

    skill, store, _, client = drafting(reference, review_url)
    run(skill.draft_patch(draft_in(reference)))
    _, patch = drafted_patch(store)
    _, _, mirror = review_url
    facts = revert_facts(mirror, patch.reverted_commit)
    assert check_boundaries(patch, revert=facts) == refuse_protected(patch.canonical(), facts)


def test_a_retried_step_stores_nothing_twice_and_asks_nothing(reference, review_url):
    skill, store, transport, _ = drafting(reference, review_url)
    first = run(skill.draft_patch(draft_in(reference)))
    calls = len(transport.bodies)
    again = run(skill.draft_patch(draft_in(reference)))
    assert again.output == first.output and len(store.saved) == 1
    assert len(transport.bodies) == calls


# ---------------------------------------------------------------- what is never drafted
def test_no_patch_from_an_inconclusive_diagnosis(reference, review_url):
    """FR-17, before any model call."""
    _, _, draft = reference
    inconclusive = draft.model_copy(
        update={"outcome": "inconclusive", "leading_index": None, "proposed_action": None}
    )
    skill, store, transport, _ = drafting(reference, review_url)
    result = run(skill.draft_patch(draft_in(reference, diagnosis=inconclusive)))
    assert result.failure.code == "no_allowed_fix" and "FR-17" in result.failure.detail
    assert store.saved == {} and transport.bodies == []


def test_candidates_go_from_the_smallest_reversal_up():
    """FR-47: configuration value, then commit, then release; never beyond three."""
    assert [candidate_kind("config_revert", n) for n in (1, 2, 3, 4)] == [
        "config_revert",
        "commit_revert",
        "release_rollback",
        None,
    ]
    assert candidate_kind("commit_revert", 1) == "commit_revert"
    assert candidate_kind("release_rollback", 2) is None
    assert candidate_kind("code_fix", 1) is None  # FR-40: only the allowed list


@pytest.mark.parametrize("candidate_no", [2, 3])
def test_a_later_candidate_with_nothing_it_may_edit_is_no_allowed_fix(
    reference, review_url, candidate_no
):
    """A commit revert may not touch deploy/, and a release rollback is a person's selection."""
    skill, store, transport, _ = drafting(reference, review_url)
    result = run(
        skill.draft_patch(draft_in(reference, key=f"k:c{candidate_no}", candidate_no=candidate_no))
    )
    assert result.failure.code == "no_allowed_fix"
    assert store.saved == {} and transport.bodies == []


@pytest.mark.parametrize(
    ("change", "said"),
    [
        (lambda a: a["params"].update(service="payments-api"), "only the incident's service"),
        (lambda a: a["params"].update(action_kind="commit_revert"), "config_revert"),
        (lambda a: a["params"].update(target_ref="cfg-42"), "just before one of the recent"),
        (
            lambda a: a["files"][0].update(
                new_content=a["files"][0]["new_content"].replace(
                    "LOG_LEVEL=INFO", "LOG_LEVEL=DEBUG"
                )
            ),
            "not_a_restoration",
        ),
        (
            lambda a: a["files"][0].update(path="orders/pricing.py"),
            "didn't change orders/pricing.py",
        ),
    ],
)
def test_an_answer_outside_the_allowed_change_is_asked_again_then_refused(
    reference, review_url, change, said
):
    """FR-40, FR-47, FR-48, ADR-72: the service, the kind, the target, the restoration, no code fix."""
    answer = with_evidence(scripted_answer(), reference[0])
    change(answer)
    skill, store, transport, _ = drafting(reference, review_url, answering(answer))
    result = run(skill.draft_patch(draft_in(reference, key=f"k:{said[:8]}")))
    assert result.failure is not None and result.failure.code == "invalid_output"
    assert said in result.failure.detail
    asked = [b for b in transport.bodies if "You draft one" in b["system"]]
    assert len(asked) == 3 and store.saved == {}  # three attempts (ADR-27), nothing stored
    assert "failed validation" in json.dumps(asked[1]["messages"])


# ---------------------------------------------------------------- the review system (FR-18)
def test_the_review_system_takes_only_draft_pull_requests_and_never_merges(reference, review_url):
    skill, store, _, client = drafting(reference, review_url)
    run(skill.draft_patch(draft_in(reference)))
    _, patch = drafted_patch(store)
    pr = DraftPullRequest(
        title="t", body="b", base="main", base_revision=patch.base_revision,
        head="aisre/x", patch=patch.canonical(),
    )  # fmt: skip
    opened = client.open_draft(patch.repository, pr, "key-1")
    assert opened.draft and opened.state == "open" and not opened.merged
    assert opened.required_reviewers == ["security-lead"]  # deploy/: Artifact 11.1's third barrier
    assert client.open_draft(patch.repository, pr, "key-1").number == opened.number  # deduped
    assert client.pull(patch.repository, opened.number).draft
    assert not any(hasattr(client, m) for m in ("merge", "push", "commit"))
    url, _, _ = review_url
    for method, path, body in (
        ("PUT", f"/v1/repos/orders-api/pulls/{opened.number}/merge", {}),
        ("POST", "/v1/repos/orders-api/pulls", {**_json(pr), "draft": False}),
    ):
        req = urllib.request.Request(
            url + path, data=json.dumps(body).encode(), method=method,
            headers={"Authorization": "Bearer fake-review-token"},
        )  # fmt: skip
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code in (403, 422)


def test_a_pull_request_whose_patch_does_not_apply_at_its_base_is_refused(reference, review_url):
    skill, store, _, client = drafting(reference, review_url)
    run(skill.draft_patch(draft_in(reference)))
    _, patch = drafted_patch(store)
    _, tags, _ = review_url
    pr = DraftPullRequest(
        title="t", body="b", base="main", base_revision=tags["cfg-41"],
        head="aisre/y", patch=patch.canonical(),
    )  # fmt: skip
    with pytest.raises(Exception, match="does not apply"):
        client.open_draft(patch.repository, pr, "key-2")


def test_the_review_system_needs_a_token(review_url):
    url, _, _ = review_url
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(url + "/v1/repos/orders-api")
    assert e.value.code == 401


def _json(pr: DraftPullRequest) -> dict[str, Any]:
    return {
        "title": pr.title,
        "body": pr.body,
        "base": pr.base,
        "base_revision": pr.base_revision,
        "head": pr.head,
        "patch": pr.patch,
    }
