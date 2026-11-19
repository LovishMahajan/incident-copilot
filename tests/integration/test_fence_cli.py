"""T4.4 (ADR-50, ADR-69; Artifact 12.5): the manual fence through the CLI, on PostgreSQL, with
tokens from the stack's FAKE issuer (ADR-74).

A deployment of its own, so the stack's other writes are never fenced: an incident lead sets it
with a reason, the dispatcher's fence then refuses that deployment and no other; a responder
can't set one, and lifting needs the Platform admin with an incident lead's sign-off.
"""

from __future__ import annotations

import io
import json
import uuid

import pytest
import stack
from sqlalchemy import text

from aisre.cli import main
from aisre.record import make_engine
from aisre.record.execution import PgFence


def cli(env: dict[str, str], *argv: str) -> tuple[int, str]:
    out = io.StringIO()
    return main(list(argv), env, out), out.getvalue()


@pytest.fixture
def dispatcher_engine(postgres_url, settings):
    engine = make_engine(settings.dispatcher_database_url)
    yield engine
    engine.dispose()


@pytest.fixture
def env(app_engine) -> dict[str, str]:
    code, _ = stack.http("GET", f"{stack.ISSUER}/jwks")
    if code != 200:
        pytest.skip("the stack's fake token issuer isn't answering")
    return {
        "AISRE_ENV": "test",
        "AISRE_DATABASE_URL": app_engine.url.render_as_string(hide_password=False),
        "AISRE_OIDC_ISSUER": "http://token-issuer:8080",
        "AISRE_OIDC_AUDIENCE": "aisre-api",
        "AISRE_OIDC_JWKS_URL": f"{stack.ISSUER}/jwks",
    }


def test_a_manual_fence_is_set_and_lifted_by_the_right_people(env, app_engine, dispatcher_engine):
    dep = f"m4-fence-{uuid.uuid4().hex[:8]}"
    lead = stack.token_for("ines", ["aisre:incident_lead"])
    admin = stack.token_for("pat", ["aisre:platform_admin"])
    responder = stack.token_for("rin", ["aisre:responder"])
    one_service_lead = stack.token_for("lee", ["aisre:incident_lead:orders-api"])
    fence = PgFence(dispatcher_engine)
    epoch = fence.epoch

    for who in (responder, one_service_lead):
        code, _ = cli(env | {"AISRE_TOKEN": who}, "fence", "set", "--why", "x", "--deployment", dep)
        assert code == 1
    code, _ = cli(env | {"AISRE_TOKEN": lead}, "fence", "set", "--deployment", dep, "--why", "")
    assert code == 1  # a manual fence records why
    code, out = cli(
        env | {"AISRE_TOKEN": lead}, "fence", "set", "--deployment", dep, "--why", "bad deploy"
    )
    assert code == 0, out
    shown = json.loads(out)
    assert any(f["scope"] == dep and f["set_by"] == "user:ines" for f in shown["active"])
    allowed, why = fence.writes_allowed(dep, epoch)
    assert not allowed and "manual" in why
    assert fence.writes_allowed(f"{dep}-other", epoch)[0]  # that deployment only

    lift = ("fence", "lift", "--deployment", dep)
    assert cli(env | {"AISRE_TOKEN": lead, "AISRE_SIGN_OFF_TOKEN": lead}, *lift)[0] == 1
    assert cli(env | {"AISRE_TOKEN": admin, "AISRE_SIGN_OFF_TOKEN": admin}, *lift)[0] == 1
    assert cli(env | {"AISRE_TOKEN": admin}, *lift)[0] == 1  # no sign-off
    code, out = cli(env | {"AISRE_TOKEN": admin, "AISRE_SIGN_OFF_TOKEN": lead}, *lift)
    assert code == 0, out
    assert fence.writes_allowed(dep, epoch)[0]
    assert fence.epoch == epoch  # a manual fence never changes the epoch
    with app_engine.connect() as conn:
        cleared = conn.execute(
            text("SELECT cleared_by FROM fence_reasons WHERE scope = :d"), {"d": dep}
        ).scalar_one()
    assert cleared == "user:pat, signed off by user:ines"
