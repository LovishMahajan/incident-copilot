"""Artifact 13.5 T2.6 (ADR-71): `run_sandbox_test` end to end, against PostgreSQL and Docker.

The real Test Runner activity: the patch read from the record, the checkout from the reference
repository, the sandbox under runc, the outcome stored with its isolation label and
`aisre.action.tested.v1`. Plus the three-slot cap across processes, in PostgreSQL. Under runc
this proves the flow, never isolation.
"""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
import threading
import time
from uuid import UUID

import pytest
from sqlalchemy import text
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from aisre.evidence.snapshots import MemorySnapshots
from aisre.record import uuid7
from aisre.record.transitions import Transition, record_transition
from aisre.remediation import sandbox as sb
from aisre.remediation import test_runner as tr
from aisre.remediation.patch import Patch, build_patch_from_contents
from aisre.workflow import activities as act


def labelled() -> list[str]:
    out = []
    for args in (["ps", "--all"], ["volume", "ls"]):
        out += subprocess.run(
            ["docker", *args, "--quiet", "--filter", "label=aisre.sandbox=1"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()
    return out


def patch_at(repo, rev: str, path: str, old_line: str, new_line: str, reverts=None) -> Patch:
    """An edit at `rev`; a deployment file is restored by reverting `reverts` (ADR-72)."""
    if reverts is None and path.startswith("deploy/"):
        reverts = rev
    old = subprocess.run(
        ["git", "-C", str(repo), "show", f"{rev}:{path}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return build_patch_from_contents(
        old={path: old},
        new={path: old.replace(old_line, new_line)},
        rationale="put the tax rate back in basis points",
        evidence=["ev-config-diff"],
        incident_id="inc",
        diagnosis_id="diag",
        candidate_no=1,
        action_kind="config_revert",
        repository="orders-api",
        base_revision=sb.checkout(repo, rev)[0],
        target_service="orders-api",
        target_environment="production",
        summary="Revert ORDER_TAX_RATE_BPS to 825",
        reverted_commit=sb.checkout(repo, reverts)[0] if reverts else None,
    )


@pytest.fixture
def testing_action(app_engine, catalogue, quiet):
    """An incident with an established diagnosis and an action in `testing`, whose patch is
    stored as the Test Runner reads it: canonical bytes behind a `patch_diff` artifact."""
    org, service, _ = catalogue
    blobs = MemorySnapshots()

    def make(p: Patch) -> tuple[str, str]:
        incident_id = quiet(org, service)
        with app_engine.begin() as conn:
            status, version = conn.execute(
                text("SELECT status, version FROM incidents WHERE incident_id = :i"),
                {"i": incident_id},
            ).one()
            record_transition(
                conn,
                Transition(incident_id, str(uuid7()), status, "investigating", version, "test"),
            )
        ids = {k: str(uuid7()) for k in ("h1", "dia", "act", "art")}
        key = f"patches/{incident_id}/{ids['act']}.json"
        body = p.canonical_bytes()
        blobs.put(key, body, "application/json")
        sql = [
            """INSERT INTO hypotheses (hypothesis_id, incident_id, ordinal, statement, category,
                 confirm_test, refute_test, confidence)
               VALUES (:h1, :inc, 1, 'Config change', 'deployment_config', 'revert clears',
                 'still fails', 'high')""",
            """INSERT INTO diagnoses (diagnosis_id, incident_id, revision, outcome,
                 leading_hypothesis_id, category, confidence, summary, ruled_out, produced_by)
               VALUES (:dia, :inc, 1, 'established', :h1, 'deployment_config', 'high',
                 'Tax rate in percent', '{}', 'diagnosis-engine')""",
            """INSERT INTO actions (action_id, incident_id, diagnosis_id, kind, target_service_id,
                 target_environment_id, target_deployment, base_revision, expected_after_revision,
                 description, expected_effect, risk, undo_plan, recovery_criteria,
                 recovery_predicates_digest, hold_seconds, high_impact, policy_version)
               SELECT :act, :inc, :dia, 'config_revert', i.service_id, i.environment_id,
                 'orders-prod', :base, :base, 'Revert', 'Errors stop', 'Low', 'Revert back',
                 '[{"metric": "http_5xx_rate", "comparison": "<=", "threshold": 0.01}]', :sha,
                 600, false, 'edit-policy@2'
               FROM incidents i WHERE i.incident_id = :inc""",
            """INSERT INTO artifacts (artifact_id, org_id, incident_id, kind, storage_key,
                 content_sha256, size_bytes, media_type, redacted, retain_until)
               VALUES (:art, :org, :inc, 'patch_diff', :key, :sha, :size, 'application/json',
                 true, now() + interval '1 year')""",
            """INSERT INTO patches (patch_id, action_id, base_revision, diff_artifact_id,
                 patch_sha256, files_changed, lines_added, lines_removed)
               VALUES (gen_random_uuid(), :act, :base, :art, :sha, 1, 1, 1)""",
            "UPDATE actions SET status = 'testing', version = version + 1 WHERE action_id = :act",
        ]
        params = ids | {
            "inc": incident_id,
            "org": str(org),
            "key": key,
            "sha": p.digest,
            "size": len(body),
            "base": p.base_revision,
        }
        with app_engine.begin() as conn:
            for s in sql:
                conn.execute(text(s), params)
        return incident_id, ids["act"]

    return make, blobs


def runner(app_engine, blobs, image, repos, **kw) -> tr.TestRunner:
    spec = kw.pop("spec", None) or sb.sandbox_spec("test", "runc", image)
    return tr.TestRunner(app_engine, tr.PgPatchSource(app_engine, blobs), spec, repos, **kw)


def stored(app_engine, action_id: str):
    with app_engine.connect() as conn:
        runs = conn.execute(
            text(
                "SELECT outcome, reproduced, isolation, test_key FROM test_runs WHERE action_id = :a"
            ),
            {"a": action_id},
        ).all()
        events = conn.execute(
            text(
                "SELECT data FROM timeline_events WHERE type = 'aisre.action.tested.v1' "
                "AND data->>'action_id' = :a"
            ),
            {"a": action_id},
        ).all()
    return runs, [e.data for e in events]


def test_the_revert_passes_and_is_stored_labelled_development_runc(
    app_engine, testing_action, sandbox_image, reference_repo
):
    make, blobs = testing_action
    repo, _ = reference_repo
    p = patch_at(repo, "cfg-42", "deploy/production.env", "=8.25", "=825")
    _, action_id = make(p)
    r = runner(app_engine, blobs, sandbox_image, {"orders-api": repo})
    inp = act.TestIn(action_id, p.digest, test_key=f"{action_id}:test:1")
    out = asyncio.run(ActivityEnvironment().run(r.run_sandbox_test, inp))
    assert (out.outcome, out.reproduced, out.isolation) == ("passed", True, "development-runc")
    runs, events = stored(app_engine, action_id)
    assert [tuple(x) for x in runs] == [("passed", True, "development-runc", inp.test_key)]
    assert events == [
        {
            "action_id": action_id,
            "test_run_id": out.test_run_id,
            "patch_sha256": p.digest,
            "reproduced": True,
            "outcome": "passed",
            "isolation": "development-runc",
        }
    ]
    with app_engine.connect() as conn:
        started = conn.execute(
            text(
                "SELECT data FROM timeline_events WHERE type = 'aisre.action.test_started.v1' "
                "AND data->>'action_id' = :a"
            ),
            {"a": action_id},
        ).scalar_one()
    assert started == {
        "action_id": action_id,
        "patch_sha256": p.digest,
        "isolation": "development-runc",
    }
    again = asyncio.run(ActivityEnvironment().run(r.run_sandbox_test, inp))
    assert again.test_run_id == out.test_run_id and len(stored(app_engine, action_id)[0]) == 1
    assert labelled() == []


def test_a_base_that_does_not_fail_is_not_reproduced_and_the_patch_never_tested(
    app_engine, testing_action, sandbox_image, reference_repo
):
    make, blobs = testing_action
    repo, _ = reference_repo
    p = patch_at(repo, "tolerant-42", "deploy/production.env", "=8.25", "=825")
    _, action_id = make(p)
    r = runner(app_engine, blobs, sandbox_image, {"orders-api": repo})
    out = asyncio.run(
        ActivityEnvironment().run(
            r.run_sandbox_test, act.TestIn(action_id, p.digest, f"{action_id}:nr")
        )
    )
    assert (out.outcome, out.reproduced) == ("not_reproduced", False)
    assert [tuple(x)[:3] for x in stored(app_engine, action_id)[0]] == [
        ("not_reproduced", False, "development-runc")
    ]


def test_a_patch_touching_tests_is_refused_and_nothing_starts(
    app_engine, testing_action, sandbox_image, reference_repo
):
    make, blobs = testing_action
    repo, _ = reference_repo
    p = patch_at(repo, "cfg-42", "tests/smoke/test_smoke.py", '"32.48"', '"32.47"')
    _, action_id = make(p)
    r = runner(app_engine, blobs, sandbox_image, {"orders-api": repo})
    with pytest.raises(ApplicationError) as e:
        asyncio.run(
            ActivityEnvironment().run(
                r.run_sandbox_test, act.TestIn(action_id, p.digest, f"{action_id}:p")
            )
        )
    assert e.value.type == "PatchRejected"
    assert stored(app_engine, action_id) == ([], []) and labelled() == []


def test_a_changed_patch_is_refused(app_engine, testing_action, sandbox_image, reference_repo):
    make, blobs = testing_action
    repo, _ = reference_repo
    p = patch_at(repo, "cfg-42", "deploy/production.env", "=8.25", "=825")
    _, action_id = make(p)
    r = runner(app_engine, blobs, sandbox_image, {"orders-api": repo})
    with pytest.raises(ApplicationError) as e:
        asyncio.run(
            ActivityEnvironment().run(
                r.run_sandbox_test, act.TestIn(action_id, "f" * 64, f"{action_id}:c")
            )
        )
    assert e.value.type == "PatchChanged"


def test_cancel_kills_the_container_and_stores_nothing(
    app_engine, testing_action, sandbox_image, reference_repo, tmp_path
):
    """A long harness run (a sleeping unit test on a copy of the repository), cancelled."""
    import shutil

    make, blobs = testing_action
    repo = tmp_path / "orders-api"
    shutil.copytree(reference_repo[0], repo)
    (repo / "tests/unit/test_slow.py").write_text(
        "import time, unittest\n\n\nclass Slow(unittest.TestCase):\n"
        "    def test_hangs(self):\n        time.sleep(600)\n"
    )
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    }
    for args in (["add", "--all"], ["commit", "-q", "--no-gpg-sign", "-m", "slow"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)
    p = patch_at(repo, "main", "deploy/production.env", "=8.25", "=825", reverts="cfg-42")
    _, action_id = make(p)
    r = runner(app_engine, blobs, sandbox_image, {"orders-api": repo})
    activity_env = ActivityEnvironment()

    async def go():
        task = asyncio.ensure_future(
            activity_env.run(r.run_sandbox_test, act.TestIn(action_id, p.digest, f"{action_id}:x"))
        )
        for _ in range(150):
            if [c for c in labelled() if c]:
                running = subprocess.run(
                    ["docker", "ps", "--quiet", "--filter", "label=aisre.sandbox=1"],
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                if running:
                    break
            await asyncio.sleep(0.2)
        await asyncio.sleep(2)  # into the hanging suite
        started = time.monotonic()
        activity_env.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return time.monotonic() - started

    assert asyncio.run(go()) < 15
    assert labelled() == [] and stored(app_engine, action_id) == ([], [])


def test_three_slots_across_processes(app_engine):
    """ADR-56: a fourth holder, on its own connection as another process would be, waits for a
    slot. Each holder takes and gives back its slot in its own thread, as run_sandbox does."""
    done, got = threading.Event(), {}

    def hold(n: int, wait_s: float) -> None:
        slots = tr.PgSandboxSlots(app_engine)
        got[n] = slots.acquire(wait_s)
        if got[n]:
            done.wait(20) if n else first_out.wait(20)
            slots.release()

    first_out = threading.Event()
    threads = [threading.Thread(target=hold, args=(n, 0)) for n in range(3)]
    for t in threads:
        t.start()
    time.sleep(0.5)
    assert got == {0: True, 1: True, 2: True}
    fourth = threading.Thread(target=hold, args=(3, 10))
    fourth.start()
    time.sleep(1)
    assert 3 not in got  # still waiting
    first_out.set()  # holder 0 gives its slot back
    for _ in range(50):
        if 3 in got:
            break
        time.sleep(0.1)
    assert got[3] is True
    done.set()
    for t in [*threads, fourth]:
        t.join(10)
    again = tr.PgSandboxSlots(app_engine)
    assert again.acquire(0)  # all free again
    again.release()


def test_the_test_runs_table_refuses_an_unlabelled_run(app_engine, testing_action):
    with pytest.raises(Exception, match="isolation"):
        with app_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO test_runs (test_run_id, action_id, patch_sha256, reproduced,"
                    " outcome, started_at, finished_at) VALUES (:t, :a, :p, true, 'passed',"
                    " now(), now())"
                ),
                {"t": UUID(int=1), "a": UUID(int=2), "p": hashlib.sha256(b"x").hexdigest()},
            )
