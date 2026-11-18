"""ST-19 to ST-22: authentication, authorisation and cross-service access. No HTTP API exists yet, so only the
ADR-50 matrix, the ToolGate and retrieval filters can be exercised."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from aisre.agent.tool_registry import ToolGate, ToolRefused
from aisre.remediation.action import Directory, Perm, Role
from aisre.retrieval.retrieval import (
    Document,
    IndexRevision,
    wrong_permission_hits,
    wrong_service_hits,
)
from aisre.workflow.activities import precheck_requests

T = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
SVC = "checkout"


def gate():
    return ToolGate("inc-1", SVC, T, T + timedelta(minutes=30))


def window(**over):
    return {
        "service": SVC,
        "start": (T - timedelta(hours=1)).isoformat(),
        "end": T.isoformat(),
        **over,
    }


def refused(code, tool, params, skill="collect_evidence"):
    with pytest.raises(ToolRefused) as e:
        gate().check(tool, params, skill)
    assert e.value.code == code


# ---------------------------------------------------------------- ST-19
# The API with the fake issuer's keys (ops/fakes/token_issuer.py) and no reachable database:
# every refusal must come before any data is read. With PostgreSQL, the same cases run in
# tests/integration/test_api_auth.py.
UNREACHABLE = "postgresql+psycopg://aisre_app@127.0.0.1:1/aisre?connect_timeout=1"
PROBLEM_KEYS = {"type", "title", "status", "detail", "instance", "code", "request_id"}


@pytest.fixture(scope="module")
def token_api(token_issuer):
    from fastapi.testclient import TestClient
    from pydantic import SecretStr
    from sqlalchemy import create_engine

    from aisre.api.app import create_app
    from aisre.config import Settings

    issuer, jwks_url = token_issuer
    settings = Settings(
        env="test",
        database_url=SecretStr(UNREACHABLE),
        oidc_issuer=issuer.url,
        oidc_audience=issuer.audience,
        oidc_jwks_url=jwks_url,
    )
    with TestClient(create_app(settings, create_engine(UNREACHABLE))) as c:
        yield c


def st19_tokens(issuer):
    now = int(datetime.now(timezone.utc).timestamp())
    good = issuer.token("alice", ["aisre:incident_lead"])
    head, body, sig = good.split(".")
    flipped = sig[:-2] + ("AA" if sig[-2:] != "AA" else "BA")
    return {
        "no token": None,
        "expired": issuer.sign(issuer.claims("alice", ttl_seconds=60, now=now - 600)),
        "wrong issuer": issuer.sign(issuer.claims("alice") | {"iss": "http://evil.test"}),
        "wrong audience": issuer.sign(issuer.claims("alice") | {"aud": "other-api"}),
        "altered signature": f"{head}.{body}.{flipped}",
        "altered claims": f"{head}.{issuer.token('alice', ['aisre:platform_admin']).split('.')[1]}.{sig}",
        "alg none": f"eyJhbGciOiJub25lIn0.{body}.c2ln",  # {"alg":"none"}
    }


ST19_REASONS = {
    "no token": "missing_token",
    "expired": "expired",
    "wrong issuer": "wrong_issuer",
    "wrong audience": "wrong_audience",
    "altered signature": "bad_signature",
    "altered claims": "bad_signature",
    "alg none": "unsupported_alg",
}


@pytest.mark.parametrize("case", list(ST19_REASONS))
@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/v1/incidents/00000000-0000-7000-8000-0000000000aa"),
        ("GET", "/v1/incidents/00000000-0000-7000-8000-0000000000aa/timeline"),
        ("POST", "/v1/incidents"),
    ],
)
def test_st19_a_bad_token_is_refused_with_no_data_and_logged(
    token_api, token_issuer, caplog, case, method, path
):
    token = st19_tokens(token_issuer[0])[case]
    headers = {"Idempotency-Key": "st19-key-0001"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    with caplog.at_level("INFO", logger="aisre.audit"):
        r = token_api.request(method, path, headers=headers, json={"service": "orders-api"})
    assert r.status_code == 401, r.text
    assert r.headers["content-type"] == "application/problem+json"
    assert set(r.json()) <= PROBLEM_KEYS and r.json()["code"] == "unauthenticated"
    assert r.headers["WWW-Authenticate"].startswith("Bearer")
    events = [json.loads(m) for m in caplog.messages]
    assert [(e["event"], e["reason"]) for e in events] == [
        ("aisre.auth.failed", ST19_REASONS[case])
    ]
    assert events[0]["path"] == path and events[0]["request_id"] == r.headers["X-Request-ID"]
    assert token is None or token not in caplog.text  # the token is never logged


def test_st19_the_development_header_is_refused_once_tokens_are_configured(token_api, caplog):
    with caplog.at_level("INFO", logger="aisre.audit"):
        r = token_api.get(
            "/v1/incidents/00000000-0000-7000-8000-0000000000aa",
            headers={"X-Aisre-Dev-Caller": "alice", "X-Aisre-Dev-Groups": "aisre:incident_lead"},
        )
    assert r.status_code == 401
    assert json.loads(caplog.messages[0])["reason"] == "dev_header_refused"


def test_st19_the_development_header_is_refused_outside_development_and_test():
    from pydantic import SecretStr
    from sqlalchemy import create_engine

    from aisre.api.app import create_app
    from aisre.config import Settings

    with pytest.raises(RuntimeError, match="development-only"):
        create_app(
            Settings(env="production", database_url=SecretStr(UNREACHABLE)),
            create_engine(UNREACHABLE),
        )


def test_st19_an_unreachable_issuer_is_503_not_a_pass(caplog):
    from fastapi.testclient import TestClient
    from pydantic import SecretStr
    from sqlalchemy import create_engine

    from aisre.api.app import create_app
    from aisre.config import Settings

    settings = Settings(
        env="test",
        database_url=SecretStr(UNREACHABLE),
        oidc_issuer="http://issuer.test",
        oidc_audience="aisre-api",
        oidc_jwks_url="http://127.0.0.1:1/jwks",
    )
    token = "eyJhbGciOiJSUzI1NiIsImtpZCI6ImsifQ.e30.c2ln"
    with TestClient(create_app(settings, create_engine(UNREACHABLE))) as c:
        r = c.get(
            "/v1/incidents/00000000-0000-7000-8000-0000000000aa",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 503 and r.json()["code"] == "sign_in_unavailable"


# ---------------------------------------------------------------- ST-20
@pytest.mark.parametrize(
    "role,perm",
    [
        (Role.VIEWER, Perm.CANCEL),
        (Role.MONITORING, Perm.READ),
        (Role.RESPONDER, Perm.CONFIRM_SEVERITY),
        (Role.PLATFORM_ADMIN, Perm.NOTE),
        (Role.VIEWER, Perm.AUDIT_EXPORT),
    ],
)
def test_st20_the_matrix_refuses_each_named_case(role, perm):
    assert Directory({("u", SVC): {role}}).can("u", SVC, perm) is False


@pytest.mark.parametrize(
    "role,perm",
    [
        (Role.RESPONDER, Perm.CANCEL),
        (Role.VIEWER, Perm.READ),
        (Role.INCIDENT_LEAD, Perm.CONFIRM_SEVERITY),
        (Role.RESPONDER, Perm.NOTE),
        (Role.INCIDENT_LEAD, Perm.AUDIT_EXPORT),
    ],
)
def test_st20_control_the_matrix_allows_the_rightful_holder(role, perm):
    assert Directory({("u", SVC): {role}}).can("u", SVC, perm) is True


def test_st20_a_grant_on_one_service_gives_nothing_on_another():
    assert Directory({("u", "payments"): {Role.INCIDENT_LEAD}}).can("u", SVC, Perm.READ) is False


# At the routes (T1.4), with the database unreachable: a denial comes before any read.
INCIDENT = "/v1/incidents/00000000-0000-7000-8000-0000000000aa"


@pytest.mark.parametrize(
    "groups,method,path,perm",
    [
        (["aisre:monitoring"], "GET", INCIDENT + "/timeline", "incident:read"),
        (["aisre:monitoring"], "GET", INCIDENT, "incident:read"),
        (["aisre:viewer"], "POST", "/v1/incidents", "incident:create"),
        (["aisre:platform_admin"], "POST", "/v1/incidents", "incident:create"),
        ([], "GET", INCIDENT, "incident:read"),
        (["aisre:security_lead"], "GET", INCIDENT + "/timeline", "incident:read"),
    ],
)
def test_st20_a_route_refuses_what_the_matrix_refuses_and_logs_it(
    token_api, token_issuer, caplog, groups, method, path, perm
):
    token = token_issuer[0].token("mallory", groups)
    with caplog.at_level("INFO", logger="aisre.audit"):
        r = token_api.request(
            method,
            path,
            json={"service": "orders-api"},
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "st20-key-001"},
        )
    assert r.status_code == 403 and r.json()["code"] == "forbidden"
    assert set(r.json()) <= PROBLEM_KEYS
    (event,) = [json.loads(m) for m in caplog.messages]
    assert event["event"] == "aisre.authz.denied" and event["permission"] == perm
    assert event["caller"] == "user:mallory" and event["path"] == path


@pytest.mark.parametrize(
    "groups,path,body,perm",
    [
        (["aisre:viewer"], INCIDENT + "/cancellation", {"reason": "stop it"}, "incident:cancel"),
        (
            ["aisre:responder"],
            INCIDENT + "/severity-confirmation",
            {"severity": "sev1"},
            "incident:confirm_severity",
        ),
        (["aisre:platform_admin"], INCIDENT + "/notes", {"note": "hello"}, "incident:note"),
    ],
)
def test_st20_cancel_severity_and_note_routes_refuse_and_log(
    token_api, token_issuer, caplog, groups, path, body, perm
):
    """T1.6's routes: Viewer cancels, Responder confirms severity, Platform admin adds a note."""
    token = token_issuer[0].token("mallory", groups)
    with caplog.at_level("INFO", logger="aisre.audit"):
        r = token_api.post(
            path,
            json=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": "st20-key-002",
                "If-Match": '"1"',
            },
        )
    assert r.status_code == 403 and r.json()["code"] == "forbidden"
    (event,) = [json.loads(m) for m in caplog.messages]
    assert event["event"] == "aisre.authz.denied" and event["permission"] == perm


def test_st20_audit_export_route_arrives_with_t1_20():
    pytest.skip(
        "Viewer reads the full audit export needs the route T1.20 adds with the write-once "
        "audit copy; it will declare audit:export with aisre.api.permissions.requires"
    )


# ---------------------------------------------------------------- ST-21
def test_st21_existence_probe_runs_against_postgresql():
    pytest.skip(
        "Dead-lettering needs the database: tests/integration/test_intake.py::"
        "test_st21_existence_probe runs the case (the person's same 404 for an unknown and a "
        "forbidden service, Monitoring's 422, three dead letters and three lines for the lead)"
    )


# ---------------------------------------------------------------- ST-22
@pytest.mark.parametrize("tool", ["get_logs", "get_changes", "get_traces", "get_error_groups"])
def test_st22_tool_request_for_another_service_is_refused(tool):
    refused("out_of_scope", tool, window(service="payments"))


def test_st22_another_teams_dependency_is_out_of_scope():
    refused("out_of_scope", "get_dependency_health", {"service": "payments", "at": T.isoformat()})


def test_st22_there_is_no_environment_argument_to_widen():
    refused("invalid_params", "get_logs", window(environment="staging"))


def test_st22_control_the_incidents_own_service_is_allowed():
    tool, params, _ = gate().check("get_logs", window(), "collect_evidence")
    assert tool.name == "get_logs" and params.service == SVC


@pytest.mark.parametrize(
    "code,start,end",
    [
        ("window_too_old", T - timedelta(hours=26), T - timedelta(hours=25)),
        ("window_in_future", T, T + timedelta(hours=2)),
        ("window_too_large", T - timedelta(hours=8), T),
    ],
)
def test_st22_time_outside_the_window_is_refused(code, start, end):
    refused(code, "get_logs", window(start=start.isoformat(), end=end.isoformat()))


def test_st22_refused_request_stays_with_the_activity_not_silently_dropped():
    allowed, refusals = precheck_requests(
        gate(),
        [
            {"tool": "get_logs", "params": window(service="payments")},
            {"tool": "get_logs", "params": window()},
        ],
        "hypothesise",
    )
    assert len(allowed) == 1 and refusals[0]["refused"] == "out_of_scope"


def test_st22_a_request_is_checked_under_the_skill_that_made_it():
    """`diagnose` has no tools (9.2), so a request it slips in is refused; the critic's own is not."""
    ask = [{"tool": "get_logs", "params": window()}]
    assert precheck_requests(gate(), ask, "diagnose")[1][0]["refused"] == "tool_not_allowed"
    assert (
        precheck_requests(gate(), ask, "retrieve_knowledge")[1][0]["refused"] == "tool_not_allowed"
    )
    assert precheck_requests(gate(), ask, "critique") == (ask, [])


def doc(doc_id, services, groups, text):
    return Document(
        doc_id,
        "v1",
        doc_id,
        f"# {doc_id}\n\n{text}",
        frozenset(services),
        frozenset(groups),
        T,
        "sre",
    )


def test_st22_retrieval_filters_service_and_group_before_ranking():
    idx = IndexRevision.build(
        [
            doc("mine", {SVC}, {"sre"}, "payments timeout rollback steps"),
            doc("theirs", {"payments"}, {"sre"}, "payments timeout rollback steps"),
            doc("locked", {SVC}, {"finance"}, "payments timeout rollback steps"),
        ],
        now=T,
    )
    hits = idx.search("payments timeout", service=SVC, groups={"sre"})
    assert [h.doc_id for h in hits] == ["mine"]
    assert wrong_service_hits(hits, SVC) == 0 and wrong_permission_hits(hits, {"sre"}) == 0


def test_st22_artifact_reads_and_read_logging_need_the_api():
    pytest.skip(
        "Needs a running API and evidence adapters: artifact access by role, the refusal log and the log of "
        "every content read aren't in the reference modules. ToolGate records only allowed calls"
    )
