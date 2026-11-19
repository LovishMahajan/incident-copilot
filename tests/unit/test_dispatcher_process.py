"""Artifact 13.5 T3.5, ADR-44, ADR-73: the dispatcher process refuses to start without what it
needs, and never in production; its directory answers from identity groups, failing closed."""

from __future__ import annotations

import pytest

from aisre.config import Settings
from aisre.remediation.action import Perm
from aisre.remediation.dispatcher_process import GroupDirectory, from_settings
from aisre.remediation.release import ReleaseRefused


def test_production_has_no_dispatcher():
    with pytest.raises(ReleaseRefused):
        from_settings(Settings(env="production"), engine=None)  # type: ignore[arg-type]


def test_development_without_the_release_adapter_refuses():
    s = Settings(
        env="development",
        approval_verify_key="A6EHv/POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg=",
        identity_directory_url="http://token-issuer:8080",
        object_store_url="http://minio:9000",
        object_store_bucket="b",
        object_store_access_key="k",
        object_store_secret_key="s",
    )
    with pytest.raises(ReleaseRefused, match="AISRE_RELEASE_ADAPTER"):
        from_settings(s, engine=None)  # type: ignore[arg-type]


def test_rights_come_from_current_groups_and_an_unknown_subject_has_none():
    groups = {"approver-a": ["aisre:approver:orders-api"], "lead-b": ["aisre:incident_lead"]}
    d = GroupDirectory(lambda subject: groups.get(subject.removeprefix("user:"), []))
    assert d.can("user:approver-a", "orders-api", Perm.APPROVE)
    assert not d.can("user:approver-a", "payments-api", Perm.APPROVE)
    assert d.can("user:lead-b", "orders-api", Perm.RESOLVE_OUTCOME)
    assert not d.can("user:nobody", "orders-api", Perm.READ)
    assert not d.can("system:workflow", "orders-api", Perm.READ)
