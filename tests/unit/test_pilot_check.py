"""Artifact 13.5 T5.1 (ADR-44, ADR-75): the go-live check refuses unless Release A holds.

A good production configuration passes; each thing Release A forbids, set alone on top of it,
fails exactly its own check; and no value is ever printed.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from aisre import cli
from aisre.evidence import sources
from aisre.pilot import check

ROOT = Path(__file__).parents[2]
SECRET = "sk-not-a-real-key-0f9e8d7c6b5a"


class RealishSource:
    """Stands in for a production adapter, which pass 2 adds to ADAPTERS."""

    def __init__(self, url: str) -> None:
        self.url = url

    def fetch(self, tool, params, environment):  # pragma: no cover - never called here
        raise sources.SourceUnavailable("not called")


@pytest.fixture(autouse=True)
def realish_adapter(monkeypatch):
    monkeypatch.setitem(sources.ADAPTERS, "realish", RealishSource)


EVIDENCE = {
    "services": {
        "orders-api/production": {"*": {"adapter": "realish", "url": "https://metrics.example"}},
        "payments-api": {"*": {"adapter": "realish", "url": "https://metrics.example"}},
    }
}
GOOD = {
    "AISRE_ENV": "production",
    "AISRE_PROVIDER_API_KEY": SECRET,
    "AISRE_EVIDENCE_SOURCES": json.dumps(EVIDENCE),
    "AISRE_REVIEW_ADAPTER": "github",
    "AISRE_REVIEW_URL": "https://api.github.com",
    "AISRE_REVIEW_TOKEN": "github_pat_not_a_real_token",
    "AISRE_REVIEW_REPOS": json.dumps(
        {"orders-api": "example-org/orders-api", "payments-api": "example-org/payments-api"}
    ),
}
PAGING = """route:
  receiver: platform_owner
receivers:
  - name: platform_owner
    pagerduty_configs:
      - routing_key_file: /run/secrets/pd-platform
  - name: incident_lead
    webhook_configs:
      - url_file: /run/secrets/pager-incident-lead
  - name: security_lead
    webhook_configs:
      - url: https://pager.example/hooks/security
"""


@pytest.fixture
def paging(tmp_path) -> Path:
    path = tmp_path / "alertmanager.yml"
    path.write_text(PAGING)
    return path


def failing(env: dict[str, str], paging: Path, services=("orders-api", "payments-api")):
    return [r for r in check.run(env, list(services), paging) if not r.ok]


def test_a_release_a_configuration_passes(paging):
    assert failing(GOOD, paging) == []


@pytest.mark.parametrize(
    ("extra", "fails"),
    [
        ({"AISRE_ENV": "test"}, "AISRE_ENV is production"),
        ({"AISRE_RELEASE_TOKEN": SECRET}, "no release or production write credential"),
        ({"AISRE_RELEASE_GITHUB_TOKEN_FILE": "/run/x"}, "no release or production write"),
        ({"AISRE_RELEASE_ADAPTER": "fake_release"}, "no fake release process"),
        ({"AISRE_SCRIPTED_SKILLS": "true"}, "no scripted skills"),
        ({"AISRE_SANDBOX_RUNTIME": "runc"}, "sandbox runtime is runsc"),
        ({"AISRE_APPROVAL_SIGNING_KEY": SECRET}, "no development signing key"),
        ({"AISRE_APPROVAL_VERIFY_KEY": "abc"}, "no development signing key"),
        ({"AISRE_REVIEW_ADAPTER": "fake_review"}, "no fake review system"),
        ({"AISRE_IDENTITY_DIRECTORY_URL": "http://x"}, "no fake review system"),
        ({"AISRE_VERIFIER_MONITORING_URL": "http://x"}, "no fake review system"),
        ({"AISRE_PROVIDER_API_KEY": ""}, "real model"),
        ({"AISRE_PROVIDER_API_KEY": "fake-provider-key"}, "real model"),
        ({"AISRE_PROVIDER_URL": "http://fake-provider:8080"}, "real model"),
        ({"AISRE_EVIDENCE_SOURCES": ""}, "evidence sources configured"),
        # ADR-75: Release A drafts pull requests; no real review system is a FAIL.
        (
            {"AISRE_REVIEW_ADAPTER": "", "AISRE_REVIEW_URL": "", "AISRE_REVIEW_TOKEN": ""},
            "real review system",
        ),
        ({"AISRE_REVIEW_REPOS": json.dumps({"orders-api": "o/orders-api"})}, "real review system"),
        # The GitHub Actions release adapter, configured: its AISRE_RELEASE_* token is refused.
        (
            {
                "AISRE_RELEASE_ADAPTER": "github_actions",
                "AISRE_RELEASE_URL": "https://api.github.com",
                "AISRE_RELEASE_TOKEN": SECRET,
                "AISRE_RELEASE_GITHUB": json.dumps(
                    {"repository": "o/deploys", "apply_workflow": "a.yml", "undo_workflow": "u.yml"}
                ),
            },
            "no release or production write credential",
        ),
        (
            {
                "AISRE_EVIDENCE_SOURCES": json.dumps(
                    {"sources": {"*": {"adapter": "fake_monitoring", "url": "http://f"}}}
                )
            },
            "evidence sources load in production",
        ),
    ],
)
def test_each_thing_release_a_forbids_is_refused(paging, extra, fails):
    assert any(r.name.startswith(fails) for r in failing({**GOOD, **extra}, paging))


def test_remediation_on_is_refused(paging):
    """remediation_enabled: the approval path is on only in development or test with the fake
    release process, so this is the configuration that turns it on."""
    found = failing({**GOOD, "AISRE_ENV": "test", "AISRE_RELEASE_ADAPTER": "fake_release"}, paging)
    assert "remediation off: a tested fix is handed over, never sent for approval" in {
        r.name for r in found
    }


def test_a_pilot_service_without_sources_for_every_tool_is_refused(paging):
    partial = {"services": {"orders-api": {"get_logs": {"adapter": "realish", "url": "https://l"}}}}
    found = failing({**GOOD, "AISRE_EVIDENCE_SOURCES": json.dumps(partial)}, paging, ["orders-api"])
    (r,) = found
    assert r.name == "evidence sources for orders-api in production"
    assert "get_alert" in r.detail and "get_logs" not in r.detail
    assert {r.name for r in failing(GOOD, paging, ["checkout"])} == {
        "evidence sources for checkout in production",
        "real review system: draft pull requests on GitHub for every pilot service",
    }
    assert failing(GOOD, paging, [])[0].name == "pilot services named"


def test_the_repositorys_paging_is_the_fake_pager_and_is_refused():
    found = [r.name for r in check.check_paging(ROOT / "ops/compose/alertmanager.yml") if not r.ok]
    assert found == ["no fake pager (ADR-74)"]


@pytest.mark.parametrize(
    "text",
    [
        PAGING.replace("  - name: security_lead\n", "  - name: security\n"),
        PAGING.split("  - name: security_lead")[0] + "  - name: security_lead\n",
        "route:\n  receiver: x\n",
    ],
)
def test_a_paging_role_without_a_receiver_is_refused(tmp_path, text):
    path = tmp_path / "am.yml"
    path.write_text(text)
    assert not check.check_paging(path)[0].ok
    assert not check.check_paging(tmp_path / "missing.yml")[0].ok


def test_the_command_exits_1_and_never_prints_a_value(paging):
    env = {
        **GOOD,
        "AISRE_RELEASE_TOKEN": SECRET + "-release",
        "AISRE_APPROVAL_SIGNING_KEY": SECRET + "-signing",
        "AISRE_DATABASE_URL": f"postgresql+psycopg://aisre_app:{SECRET}-db@db/aisre",
        "AISRE_OIDC_JWKS_URL": f"http://{SECRET}-jwks",  # refused: not https in production
        "AISRE_SANDBOX_IMAGE": SECRET,  # malformed: not a digest
    }
    out = io.StringIO()
    code = cli.main(
        ["pilot", "check", "--service", "orders-api", "--alertmanager", str(paging)], env, out
    )
    text = out.getvalue()
    assert code == 1 and "FAIL" in text and "refused" in text.splitlines()[-1]
    assert SECRET not in text


def test_the_command_exits_0_when_release_a_holds(paging):
    out = io.StringIO()
    args = ["pilot", "check", "--service", "orders-api", "--alertmanager", str(paging)]
    assert cli.main(args, GOOD, out) == 0
    assert out.getvalue().splitlines()[-1].endswith("Release A holds")


def test_evidence_credentials_referenced_must_be_present_and_are_never_printed(paging):
    dd = {
        "adapter": "datadog",
        "api_key": {"env": "DD_API_KEY"},
        "app_key": {"env": "DD_APP_KEY"},
    }
    env = {
        **GOOD,
        "AISRE_EVIDENCE_SOURCES": json.dumps(
            {"services": {**EVIDENCE["services"], "orders-api": {"get_metric": dd}}}
        ),
    }
    bad = [r for r in failing(env, paging) if r.name.startswith("evidence credentials")]
    assert len(bad) == 1 and "DD_API_KEY" in bad[0].detail and "DD_APP_KEY" in bad[0].detail
    present = {**env, "DD_API_KEY": SECRET, "DD_APP_KEY": SECRET}
    assert not [r for r in failing(present, paging) if r.name.startswith("evidence credentials")]
    assert SECRET not in "".join(r.line() for r in check.run(present, ["orders-api"], paging))
