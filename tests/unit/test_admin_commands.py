"""Artifact 13.5 T4.4 (ADR-69, 12.6): the admin commands' offline parts: who the caller is, the
person's decisions, the backup audit (NFR-46). The fence and restore writes run against
PostgreSQL in tests/integration/test_fence_cli.py and test_restore_drill.py."""

from __future__ import annotations

import io
import json
import os
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest

from aisre.api.tokens import Claims, InvalidToken
from aisre.cli import main
from aisre.reliability import admin
from aisre.reliability.admin_cli import actor, decisions


def claims(*groups: str) -> Claims:
    now = datetime.now(UTC)
    return Claims("pat", uuid4(), tuple(groups), now, True, now + timedelta(minutes=5))


@pytest.mark.parametrize(
    ("groups", "role"),
    [
        (["aisre:platform_admin"], "platform_admin"),
        (["aisre:incident_lead", "aisre:platform_admin"], "incident_lead"),
        (["aisre:incident_lead:orders-api"], "none"),  # one service's lead can't fence them all
        (["aisre:approver"], "none"),
    ],
)
def test_a_role_counts_only_on_every_service(groups, role):
    from aisre.remediation.action import Role

    got = actor("t", lambda _: claims(*groups), (Role.INCIDENT_LEAD, Role.PLATFORM_ADMIN), "x")
    assert got.subject == "user:pat" and got.role == role


def test_a_missing_or_refused_token_is_refused():
    def bad(_: str) -> Claims:
        raise InvalidToken("expired")

    with pytest.raises(admin.Refused, match="no token"):
        actor(None, bad, (), "AISRE_SIGN_OFF_TOKEN")
    with pytest.raises(admin.Refused, match="InvalidToken"):
        actor("t", bad, (), "AISRE_TOKEN")


def test_decisions_are_applied_or_not_applied_only():
    assert decisions(["a=applied", "b=not_applied"]) == {"a": "applied", "b": "not_applied"}
    with pytest.raises(admin.Refused):
        decisions(["a=unknown"])


def test_complete_refuses_decisions_on_actions_that_are_not_unresolved():
    rep = admin.ReconciliationReport({"a": "applied"}, ["b"])
    lead = admin.Actor("user:l", "incident_lead")
    with pytest.raises(admin.Refused, match="aren't unresolved"):
        admin.complete_restore(None, rep, lead, lead, {"a": "not_applied"})  # type: ignore[arg-type]


def marker(dest, bid: str, at: datetime) -> None:
    folder = dest / "base" / bid
    folder.mkdir(parents=True)
    (folder / "base.tar").write_bytes(b"x")
    admin.Marker(bid, at.isoformat(), at.isoformat(), "user:pat", 1.0, []).save(folder)


def test_backup_check_names_missed_days_and_prunes_past_35(tmp_path):
    today = date(2026, 11, 19)
    noon = datetime(2026, 11, 19, 12, tzinfo=UTC)
    for days in (0, 1, 3, 40):
        marker(tmp_path, f"b{days}", noon - timedelta(days=days))
    wal = tmp_path / "wal"
    wal.mkdir()
    (wal / "old").write_bytes(b"w")
    (wal / "new").write_bytes(b"w")
    stamp = (noon - timedelta(days=39)).timestamp()
    os.utime(wal / "old", (stamp, stamp))
    report = admin.check_backups(tmp_path, today, days=4)
    assert report["missed_days"] == ["2026-11-17"] and report["past_retention"] == ["b40"]
    assert (tmp_path / "base" / "b40").exists()  # not without --prune
    report = admin.check_backups(tmp_path, today, days=4, prune=True)
    assert not (tmp_path / "base" / "b40").exists() and report["wal_files_removed"] == 1
    assert sorted(p.name for p in wal.iterdir()) == ["new"]


def test_the_cli_routes_admin_commands_and_refuses_without_a_verifier(capsys):
    out = io.StringIO()
    env = {"AISRE_ENV": "test", "AISRE_DATABASE_URL": "postgresql+psycopg://x@127.0.0.1:1/x"}
    assert main(["fence", "set", "--why", "drill"], env, out) == 1
    assert "AISRE_OIDC_ISSUER" in capsys.readouterr().err


def test_backup_check_through_the_cli(tmp_path):
    marker(tmp_path, "b0", datetime.now(UTC))
    out = io.StringIO()
    assert main(["backup", "check", str(tmp_path), "--days", "1"], {}, out) == 0
    assert json.loads(out.getvalue())["missed_days"] == []
