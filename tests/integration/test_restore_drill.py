"""T4.4 (Artifact 12.6; NFR-21 to NFR-23, NFR-46; ADR-69): the restore drill, on the Compose
stack, through the CLI. A STAND-IN for production point-in-time recovery (ADR-74): the stack's
PostgreSQL archives its WAL to a local folder (`.aisre-backups/wal`, compose.yaml, at least every
60 s), and the restore goes into a separate PostgreSQL (`postgres-restore`, profile `drill`),
never into the stack's own data.

`fault`: minutes long, and the backup fences writes on the stack for its few seconds.

1. `aisre backup take` (Platform admin): writes fenced (`backup_quiesce`), a base backup with
   pg_basebackup inside the postgres container, a marker with its hashes, the fence lifted.
2. Incidents are recorded every WRITE_EVERY_S for WRITES; the "failure" is a copy of the WAL
   archive as it stands then (what survived the loss), with nothing forced out first.
3. Point-in-time: a restore to a moment between two writes has exactly the incidents before it.
4. The drill: restore everything the archive holds, then the CLI on the restored database:
   `restore begin` (new epoch: the old fence token is refused, NFR-23), readiness checks pass
   (read-only mode: first recovery time, NFR-22), `restore complete` refused while actions are
   unresolved and without the lead's sign-off, then the drill (standing in for the incident lead,
   12.6 step 9) decides each unresolved action as `not_applied` and the lift goes through
   (full recovery time). RPO (NFR-21) is the last incident recorded before the failure against
   the last one restored.

The timings print as one line for the M4 record; measured on this Mac with a small database.
"""

from __future__ import annotations

import asyncio
import io
import json
import shutil
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import stack
from conftest import add_catalogue
from sqlalchemy import create_engine, func, text, update
from sqlalchemy.engine import Engine

from aisre.cli import main
from aisre.contracts import IncidentCreate
from aisre.record import uuid7
from aisre.record.execution import PgFence
from aisre.record.incidents import create_incident
from aisre.record.tables import outbox
from aisre.reliability import health

pytestmark = pytest.mark.fault

BACKUPS = stack.ROOT / ".aisre-backups"
RESTORED = "postgresql+psycopg://aisre_app:fake-app-password@127.0.0.1:55433/aisre"
WRITES, WRITE_EVERY_S = 30, 5  # 150 s of writes: over two archive_timeout periods
BASEBACKUP = (
    f"docker compose --project-directory {stack.ROOT} --env-file {stack.ROOT}/.env.example "
    "exec -T postgres pg_basebackup -U postgres -D /backups/base/{id} -Ft -X fetch -c fast"
)
RPO_S, RTO_READ_ONLY_S, RTO_FULL_S = 300, 3600, 4 * 3600  # NFR-21, NFR-22 (provisional)


def cli(env: dict[str, str], *argv: str) -> tuple[int, list[dict]]:
    out = io.StringIO()
    code = main(list(argv), env, out)
    return code, [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def cli_env(db: str) -> dict[str, str]:
    return {
        "AISRE_ENV": "test",
        "AISRE_DATABASE_URL": db,
        "AISRE_OIDC_ISSUER": "http://token-issuer:8080",
        "AISRE_OIDC_AUDIENCE": "aisre-api",
        "AISRE_OIDC_JWKS_URL": f"{stack.ISSUER}/jwks",
        "AISRE_RELEASE_ADAPTER": "fake_release",
        "AISRE_RELEASE_URL": stack.RELEASE,
        "AISRE_RELEASE_TOKEN": "fake-release-token",
        "AISRE_TOKEN": stack.token_for("drill-admin", ["aisre:platform_admin"]),
        "AISRE_SIGN_OFF_TOKEN": stack.token_for("drill-lead", ["aisre:incident_lead"]),
    }


def restore_into_drill_server(backup_id: str, wal: str, target: str = "") -> float:
    """Starts postgres-restore on the backup; returns the seconds until it was promoted."""
    stack.compose("--profile", "drill", "rm", "-sfv", "postgres-restore")
    t0 = time.monotonic()
    stack.compose(
        "--profile",
        "drill",
        "up",
        "-d",
        "--no-deps",
        "--wait",
        "postgres-restore",
        timeout=600,
        overrides={
            "AISRE_RESTORE_BACKUP": backup_id,
            "AISRE_RESTORE_WAL": wal,
            "AISRE_RESTORE_TARGET": target,
        },
    )
    return time.monotonic() - t0


def created(engine: Engine, ids: list[str]) -> list[str]:
    with engine.connect() as conn:
        return [
            str(i)
            for i in conn.execute(
                text(
                    "SELECT incident_id FROM incidents WHERE incident_id = ANY(CAST(:i AS uuid[])) "
                    "ORDER BY created_at"
                ),
                {"i": ids},
            ).scalars()
        ]


@pytest.fixture
def drill():
    why = stack.available()
    if why:
        pytest.skip(why)
    try:
        yield
    finally:
        stack.compose("--profile", "drill", "rm", "-sfv", "postgres-restore")
        stack.restore()


def test_backup_point_in_time_and_restore_drill_through_the_cli(drill, app_engine):
    with app_engine.connect() as conn:
        if conn.execute(text("SHOW archive_mode")).scalar_one() != "on":
            pytest.skip("the stack's PostgreSQL isn't archiving WAL: `up -d postgres` first")
    org = uuid7()
    add_catalogue(app_engine, org, "drill-svc")
    env = cli_env(str(app_engine.url.render_as_string(hide_password=False)))

    # 1. Backup.
    bid = f"drill-{uuid.uuid4().hex[:8]}"
    code, out = cli(env, "backup", "take", str(BACKUPS), "--id", bid, "--command", BASEBACKUP)
    assert code == 0, out
    marker = out[0]
    assert marker["backup_id"] == bid and "base.tar" in marker["files"]
    assert cli(env, "fence", "show")[1][0]["writes_disabled"] is False  # quiesce lifted
    code, out = cli(env, "backup", "check", str(BACKUPS), "--days", "1")
    assert code == 0 and out[0]["missed_days"] == [], out  # today's backup is there (NFR-46)

    # 2. Writes after the backup, then the failure.
    made: list[tuple[str, datetime]] = []
    for n in range(WRITES):
        with app_engine.begin() as conn:
            iid = create_incident(
                conn,
                org_id=org,
                caller_id="dev:drill",
                body=IncidentCreate(
                    service="drill-svc",
                    environment="production",
                    started_at=datetime.now(UTC),
                    title=f"restore drill write {n}",
                    source="monitoring",
                    fingerprint=f"drill-{bid}-{n}",
                ),
                grouping_window=timedelta(0),
            ).incident_id
            conn.execute(  # withheld from the relay: nothing investigates these
                update(outbox).where(outbox.c.incident_id == iid).values(delivered_at=func.now())
            )
            at = conn.execute(
                text("SELECT created_at FROM incidents WHERE incident_id = :i"), {"i": iid}
            ).scalar_one()
        made.append((str(iid), at))
        time.sleep(WRITE_EVERY_S)
    lost_at = datetime.now(UTC)
    survived = f"drills/{bid}/wal"
    shutil.copytree(BACKUPS / "wal", BACKUPS / survived)
    ids = [i for i, _ in made]

    # 3. Point in time: halfway through the writes, between two of them.
    cut = made[WRITES // 2][1] + timedelta(seconds=WRITE_EVERY_S / 2)
    restore_into_drill_server(bid, survived, cut.isoformat())
    restored = create_engine(RESTORED)
    try:
        assert created(restored, ids) == [i for i, at in made if at <= cut]
    finally:
        restored.dispose()

    # 4. The drill: everything the archive holds.
    t0 = time.monotonic()
    data_s = restore_into_drill_server(bid, survived)
    restored = create_engine(RESTORED, pool_pre_ping=True)
    renv = cli_env(RESTORED)
    try:
        back = created(restored, ids)
        old_epoch = PgFence(restored).epoch
        code, out = cli(renv, "restore", "begin")
        assert code == 0, out
        new_epoch = out[0]["epoch"]
        assert new_epoch == old_epoch + 1
        fence = PgFence(restored)
        assert not fence.writes_allowed("orders-api-production", old_epoch)[0]  # NFR-23
        assert "restored_pending_reconciliation" in fence.writes_allowed("x", new_epoch)[1]

        async def ready() -> health.Report:
            return await health.Health(
                "drill",
                {
                    "database": health.database(restored),
                    "schema_version": health.schema_version(restored),
                },
            ).ready()

        report = asyncio.run(ready())
        assert report.ready, report.failing
        read_only_s = time.monotonic() - t0
        with restored.connect() as conn:
            broken = conn.execute(
                text(
                    "SELECT count(*) FROM incidents i, verify_timeline_chain(i.incident_id) "
                    "WHERE i.incident_id = ANY(CAST(:i AS uuid[]))"
                ),
                {"i": back},
            ).scalar_one()
            unvalidated = conn.execute(
                text("SELECT count(*) FROM pg_constraint WHERE NOT convalidated")
            ).scalar_one()
        assert broken == 0 and unvalidated == 0

        code, out = cli(renv, "restore", "report")
        assert code == 0, out
        recon = out[0]
        reconciled_s = time.monotonic() - t0
        if recon["unresolved"]:  # refused until a person decides each one (12.6 step 9)
            assert cli(renv, "restore", "complete")[0] == 1
        no_lead = renv | {"AISRE_SIGN_OFF_TOKEN": renv["AISRE_TOKEN"]}
        decide = [f"--decide={a}=not_applied" for a in recon["unresolved"]]
        assert cli(no_lead, "restore", "complete", *decide)[0] == 1  # no lead's sign-off
        code, out = cli(renv, "restore", "complete", *decide)
        assert code == 0, out
        assert PgFence(restored).writes_allowed("orders-api-production", new_epoch)[0]
        full_s = time.monotonic() - t0
    finally:
        restored.dispose()

    last_before = max(at for _, at in made)
    last_restored = max((at for i, at in made if i in back), default=None)
    assert last_restored is not None, "nothing written after the backup was restored"
    rpo = (last_before - last_restored).total_seconds()
    print(
        f"drill {bid}: base backup fenced writes {marker['fenced_seconds']} s; "
        f"{len(back)}/{WRITES} writes restored, RPO {rpo:.0f} s (failure {lost_at:%H:%M:%S}); "
        f"data restored {data_s:.0f} s, read-only {read_only_s:.0f} s, "
        f"reconciled {reconciled_s:.0f} s ({len(recon['resolved'])} resolved, "
        f"{len(recon['unresolved'])} decided by the drill), writes re-enabled {full_s:.0f} s"
    )
    assert rpo <= RPO_S and read_only_s <= RTO_READ_ONLY_S and full_s <= RTO_FULL_S
    shutil.rmtree(BACKUPS / "drills" / bid, ignore_errors=True)
    assert Path(BACKUPS / "base" / bid / "marker.json").exists()
