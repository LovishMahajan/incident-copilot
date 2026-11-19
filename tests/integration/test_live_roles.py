"""Artifact 13.5 T3.12 (ST-18, ADR-51): the database roles on the running stack's PostgreSQL.

Each process connects as its own role and holds only what it needs. The worker and API
(`aisre_app`) can't record an execution or a deployment block, which only the dispatcher writes;
the dispatcher (`aisre_dispatcher`) can't sign, decide, verify or change what an action is, and
can't read an incident beyond its confirmed severity. db/schema-tests.sql checks the grants
themselves; this runs them as the processes would. Secrets manager and real release and review
systems: not chosen (ADR-73), so not here.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

from aisre.record import make_engine

pytestmark = pytest.mark.integration

APP_REFUSED = [
    "INSERT INTO execution_attempts (attempt_id, action_id, approval_id, action_digest, "
    "execution_key, attempt_no) VALUES (:u, :u, :u, repeat('a', 64), :u, 1)",
    "UPDATE execution_attempts SET outcome = 'applied'",
    "INSERT INTO deployment_blocks (attempt_id, action_id, reason, target_deployment) "
    "VALUES (:u, :u, 'unresolved', 'x')",
    "UPDATE deployment_blocks SET cleared_by = 'user:x'",
    "DELETE FROM verifications",
    "UPDATE approval_decisions SET decision = 'approved'",
]
DISPATCHER_REFUSED = [
    "INSERT INTO approval_decisions (approval_id) VALUES (:u)",
    "INSERT INTO verifications (verification_id) VALUES (:u)",
    "UPDATE actions SET description = 'x'",
    "UPDATE actions SET recovery_criteria = '[]'::jsonb",
    "UPDATE incidents SET status = 'closed'",
    "SELECT title FROM incidents LIMIT 1",
    "UPDATE timeline_events SET actor = 'x'",
]


@pytest.fixture(scope="module")
def dispatcher_engine(postgres_url, settings):
    engine = make_engine(settings.dispatcher_database_url)
    yield engine
    engine.dispose()


@pytest.mark.parametrize("sql", APP_REFUSED)
def test_st18_the_app_role_cannot_record_executions_or_rewrite_decisions(app_engine, sql):
    with app_engine.begin() as conn, pytest.raises(Exception, match="permission denied"):
        conn.execute(text(sql), {"u": uuid4()} if ":u" in sql else {})


@pytest.mark.parametrize("sql", DISPATCHER_REFUSED)
def test_st18_the_dispatcher_role_cannot_decide_verify_or_reshape_an_action(dispatcher_engine, sql):
    with dispatcher_engine.begin() as conn, pytest.raises(Exception, match="permission denied"):
        conn.execute(text(sql), {"u": uuid4()} if ":u" in sql else {})


def test_st18_control_each_role_reads_what_it_needs(app_engine, dispatcher_engine):
    with dispatcher_engine.connect() as conn:
        assert conn.execute(text("SELECT current_user")).scalar() == "aisre_dispatcher"
        conn.execute(text("SELECT confirmed_severity FROM incidents LIMIT 1")).all()
        conn.execute(text("SELECT count(*) FROM execution_attempts")).scalar_one()
    with app_engine.connect() as conn:
        assert conn.execute(text("SELECT current_user")).scalar() == "aisre_app"
        conn.execute(text("SELECT count(*) FROM verifications")).scalar_one()
