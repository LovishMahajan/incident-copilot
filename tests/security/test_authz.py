"""ST-19 to ST-22: authentication, authorisation and cross-service access. No HTTP API exists yet, so only the
ADR-50 matrix, the ToolGate and retrieval filters can be exercised."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aisre.remediation.action import Directory, Perm, Role
from aisre.workflow.activities import precheck_requests
from aisre.retrieval.retrieval import Document, IndexRevision, wrong_permission_hits, wrong_service_hits
from aisre.agent.tool_registry import ToolGate, ToolRefused

T = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
SVC = "checkout"


def gate():
    return ToolGate("inc-1", SVC, T, T + timedelta(minutes=30))


def window(**over):
    return {"service": SVC, "start": (T - timedelta(hours=1)).isoformat(), "end": T.isoformat(), **over}


def refused(code, tool, params, skill="collect_evidence"):
    with pytest.raises(ToolRefused) as e:
        gate().check(tool, params, skill)
    assert e.value.code == code


# ---------------------------------------------------------------- ST-19
def test_st19_authentication_needs_the_api_and_identity_system():
    pytest.skip("Needs a running system: no HTTP API or token verification exists in the reference modules, so missing, "
                "expired, wrong-issuer, wrong-audience and altered tokens have nothing to reach")


# ---------------------------------------------------------------- ST-20
@pytest.mark.parametrize("role,perm", [(Role.VIEWER, Perm.CANCEL), (Role.MONITORING, Perm.READ),
                                       (Role.RESPONDER, Perm.CONFIRM_SEVERITY), (Role.PLATFORM_ADMIN, Perm.NOTE),
                                       (Role.VIEWER, Perm.AUDIT_EXPORT)])
def test_st20_the_matrix_refuses_each_named_case(role, perm):
    assert Directory({("u", SVC): {role}}).can("u", SVC, perm) is False


@pytest.mark.parametrize("role,perm", [(Role.RESPONDER, Perm.CANCEL), (Role.VIEWER, Perm.READ),
                                       (Role.INCIDENT_LEAD, Perm.CONFIRM_SEVERITY), (Role.RESPONDER, Perm.NOTE),
                                       (Role.INCIDENT_LEAD, Perm.AUDIT_EXPORT)])
def test_st20_control_the_matrix_allows_the_rightful_holder(role, perm):
    assert Directory({("u", SVC): {role}}).can("u", SVC, perm) is True


def test_st20_a_grant_on_one_service_gives_nothing_on_another():
    assert Directory({("u", "payments"): {Role.INCIDENT_LEAD}}).can("u", SVC, Perm.READ) is False


def test_st20_route_enforcement_and_denial_logging_need_the_api():
    pytest.skip("Needs a running API: the matrix is data until a route enforces it and logs the denial")


# ---------------------------------------------------------------- ST-21
def test_st21_existence_probe_needs_the_api_and_intake():
    pytest.skip("Needs a running API and Intake: the same 404, Monitoring's 422, dead-lettering and the lead's "
                "notification are all route behaviour. The dead-letter table is covered by schema-tests.sql (see README)")


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


@pytest.mark.parametrize("code,start,end", [("window_too_old", T - timedelta(hours=26), T - timedelta(hours=25)),
                                            ("window_in_future", T, T + timedelta(hours=2)),
                                            ("window_too_large", T - timedelta(hours=8), T)])
def test_st22_time_outside_the_window_is_refused(code, start, end):
    refused(code, "get_logs", window(start=start.isoformat(), end=end.isoformat()))


def test_st22_refused_request_stays_with_the_activity_not_silently_dropped():
    allowed, refusals = precheck_requests(gate(), [{"tool": "get_logs", "params": window(service="payments")},
                                                   {"tool": "get_logs", "params": window()}])
    assert len(allowed) == 1 and refusals[0]["refused"] == "out_of_scope"


def doc(doc_id, services, groups, text):
    return Document(doc_id, "v1", doc_id, f"# {doc_id}\n\n{text}", frozenset(services), frozenset(groups), T, "sre")


def test_st22_retrieval_filters_service_and_group_before_ranking():
    idx = IndexRevision.build([doc("mine", {SVC}, {"sre"}, "payments timeout rollback steps"),
                               doc("theirs", {"payments"}, {"sre"}, "payments timeout rollback steps"),
                               doc("locked", {SVC}, {"finance"}, "payments timeout rollback steps")], now=T)
    hits = idx.search("payments timeout", service=SVC, groups={"sre"})
    assert [h.doc_id for h in hits] == ["mine"]
    assert wrong_service_hits(hits, SVC) == 0 and wrong_permission_hits(hits, {"sre"}) == 0


def test_st22_artifact_reads_and_read_logging_need_the_api():
    pytest.skip("Needs a running API and evidence adapters: artifact access by role, the refusal log and the log of "
                "every content read aren't in the reference modules. ToolGate records only allowed calls")
