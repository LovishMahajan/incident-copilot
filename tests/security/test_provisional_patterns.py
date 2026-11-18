"""Artifact 13.5 T2.10 (ADR-72): the two provisional-pattern cases known to reject real text,
decided against the development split, each with its controls.

1. `registry.internal/app`, and any URL a repository's own file holds (orders-api's
   `PAYMENTS_URL=http://payments.internal`): file content a patch proposes is scanned for secrets
   in the gateway, and line by line after the diff. A line the patch adds is scanned for links and
   commands too, unless its file is an exact restoration of an earlier revision. Model prose
   (summary, rationale) is scanned as before.
2. `10.2.1.3`: a four-part version is a release name in the error tracker's `release`
   parameter, not an address. A port, path, scheme or host name around it is still refused.

The development split itself (tests/fixtures/eval/dev-seed-1, 2,076 strings) has no link,
command, secret or IPv4-shaped string under either rule, so neither change moves its results.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from aisre.agent.agent_contracts import PatchProposal
from aisre.agent.tool_registry import ErrorGroupQuery
from aisre.agent.validation import UnsafeOutput, check_added_lines, check_output

ROOT = Path(__file__).resolve().parents[2]
ENV = (ROOT / "ops/reference-app/orders-api/deploy/production.env").read_text()


def proposal(
    content: str = ENV, summary: str = "Return ORDER_TAX_RATE_BPS to 825."
) -> PatchProposal:
    return PatchProposal.model_validate(
        {
            "params": {
                "service": "orders-api",
                "action_kind": "config_revert",
                "target_ref": "cfg-41",
            },
            "files": [
                {
                    "path": "deploy/production.env",
                    "new_content": content,
                    "rationale": "The rate is in basis points; 8.25 fails int().",
                    "evidence_ids": [str(uuid4())],
                }
            ],
            "summary": summary,
        }
    )


def test_the_reference_file_with_its_own_urls_passes_the_gateway_check():
    assert "http://payments.internal" in ENV
    check_output(proposal())


@pytest.mark.parametrize(
    "summary",
    [
        "Pull the old image from registry.internal/app and redeploy.",
        "See http://wiki.internal/runbook for the steps.",
    ],
)
def test_control_a_link_in_model_prose_is_still_refused(summary):
    with pytest.raises(UnsafeOutput) as e:
        check_output(proposal(summary=summary))
    assert e.value.code == "link"


def test_control_a_secret_in_file_content_is_still_refused_at_the_gateway():
    with pytest.raises(UnsafeOutput) as e:
        check_output(proposal(content=ENV + "API_KEY=sk-abcdefghijklmnopqrstuvwx\n"))
    assert e.value.code == "secret"


@pytest.mark.parametrize(
    ("line", "kind"),
    [
        ("IMAGE=registry.internal/app:2.14.1", "link"),
        ("WEBHOOK=https://attacker.example/x", "link"),
        ("POST_START=curl http://x.example", "link"),
        ("HOOK=sudo rm -rf /", "command"),
        ("password=hunter2", "secret"),
    ],
)
def test_control_a_line_the_patch_adds_is_scanned_like_model_text(line, kind):
    with pytest.raises(UnsafeOutput) as e:
        check_added_lines(["ORDER_TAX_RATE_BPS=825", line])
    assert e.value.code == kind


def test_an_ordinary_added_line_passes():
    check_added_lines(["ORDER_TAX_RATE_BPS=825", "ORDER_MAX_ITEMS=50"])


NOW = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)


def errors(release: str) -> ErrorGroupQuery:
    return ErrorGroupQuery(
        service="orders-api", start=NOW - timedelta(hours=1), end=NOW, release=release
    )


@pytest.mark.parametrize(
    "release", ["10.2.1.3", "v2.14.1", "2.14.1", "2.14.1-rc.1", "1.0.0+build.7"]
)
def test_a_version_is_a_release_not_an_address(release):
    assert errors(release).release == release


@pytest.mark.parametrize(
    "release",
    [
        "10.2.1.3:8080",
        "10.2.1.3/admin",
        "10.2.1.3%2Fadmin",
        "http://10.2.1.3",
        "//10.2.1.3",
        "10.2.1.3-x.internal",
        "10.2.1.3.4",
        "orders.internal",
        "localhost",
        "0x0a020103",
    ],
)
def test_control_anything_else_address_like_is_still_refused(release):
    with pytest.raises(ValueError):
        errors(release)
