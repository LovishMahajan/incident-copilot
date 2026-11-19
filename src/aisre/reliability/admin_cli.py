"""Artifact 13.5 T4.4 (ADR-69): `aisre fence|backup|restore`, the Platform admin's commands.

aisre.cli parses; this names the caller and runs aisre.reliability.admin. The caller is
AISRE_TOKEN and a lead's sign-off AISRE_SIGN_OFF_TOKEN, each verified against AISRE_OIDC_*
(the fake issuer locally, ADR-74). A role counts only when held on every service
(`aisre:platform_admin`, `aisre:incident_lead`): a fence and a restore are not one service's.
Output is JSON on standard output; a refusal goes to standard error with exit status 1.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shlex
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from sqlalchemy.exc import SQLAlchemyError

from aisre.api.tokens import Claims, InvalidToken, TokenVerifier, VerifierUnavailable
from aisre.config import Settings
from aisre.record import make_engine
from aisre.reliability import admin
from aisre.reliability.fence import Actor
from aisre.remediation.action import EVERY_SERVICE, Role, roles_from_groups

Verify = Callable[[str], Claims]
OUTCOMES = ("applied", "not_applied")


def actor(token: str | None, verify: Verify, wanted: tuple[Role, ...], what: str) -> Actor:
    """The token's subject, in the first wanted role it holds on every service. A token
    holding none of them gives role "none", which every admin rule refuses."""
    if not token:
        raise admin.Refused(f"{what}: no token given")
    try:
        claims = verify(token)
    except (InvalidToken, VerifierUnavailable) as e:
        raise admin.Refused(f"{what}: token refused ({type(e).__name__})") from None
    held = roles_from_groups(claims.groups).get(EVERY_SERVICE, frozenset())
    role = next((r.value for r in wanted if r in held), "none")
    return Actor(f"user:{claims.subject}", role)


def verifier(settings: Settings) -> Verify:
    if not settings.token_sign_in:
        raise admin.Refused("set AISRE_OIDC_ISSUER, AISRE_OIDC_AUDIENCE and AISRE_OIDC_JWKS_URL")
    assert settings.oidc_issuer and settings.oidc_audience and settings.oidc_jwks_url
    return TokenVerifier(
        settings.oidc_issuer, settings.oidc_audience, settings.oidc_jwks_url
    ).verify


def release_lookups(settings: Settings) -> tuple[Callable[..., Any], Callable[..., Any]]:
    """Lookups only, by key and current revision: nothing here can write (12.6 step 7)."""
    from aisre.remediation.release import release_from_settings

    client = release_from_settings(settings, lambda sha: b"")

    def lookup(deployment: str, key: str) -> str | None:
        return asyncio.run(client.lookup(deployment, key))

    def revision(deployment: str) -> str:
        return asyncio.run(client.current_state(deployment)).revision

    return lookup, revision


def decisions(given: list[str]) -> dict[str, str]:
    out = {}
    for d in given:
        action, _, outcome = d.partition("=")
        if outcome not in OUTCOMES:
            raise admin.Refused(f"--decide {d!r}: the outcome is one of {', '.join(OUTCOMES)}")
        out[action] = outcome
    return out


def run(
    args: argparse.Namespace,
    env: Mapping[str, str],
    out: IO[str],
    verify: Verify | None = None,
    lookups: tuple[Callable[..., Any], Callable[..., Any]] | None = None,
) -> None:
    settings = Settings.from_env(env)
    who = (args.noun, args.command)
    if who == ("backup", "check"):
        report = admin.check_backups(
            Path(args.dest), datetime.now(UTC).date(), args.days, prune=args.prune
        )
        print(json.dumps(report), file=out)
        return
    engine = make_engine(settings.database_url)
    try:
        if who == ("fence", "show"):
            print(json.dumps(admin.show(engine)), file=out)
            return
        v = verify or verifier(settings)
        me = env.get("AISRE_TOKEN")
        lead = env.get("AISRE_SIGN_OFF_TOKEN")
        admin_role = (Role.PLATFORM_ADMIN,)
        if who == ("fence", "set"):
            by = actor(me, v, (Role.INCIDENT_LEAD, Role.PLATFORM_ADMIN), "AISRE_TOKEN")
            admin.set_manual(engine, by, args.why, args.deployment)
        elif who == ("fence", "lift"):
            by = actor(me, v, admin_role, "AISRE_TOKEN")
            sign = actor(lead, v, (Role.INCIDENT_LEAD,), "AISRE_SIGN_OFF_TOKEN")
            admin.lift_manual(engine, by, sign, args.deployment)
        elif who == ("backup", "take"):
            by = actor(me, v, admin_role, "AISRE_TOKEN")
            command = args.backup_command or env.get("AISRE_BACKUP_COMMAND")
            if not command:
                raise admin.Refused("give --command or AISRE_BACKUP_COMMAND")
            m = admin.take_backup(engine, Path(args.dest), shlex.split(command), by, args.id)
            print(json.dumps(m.__dict__), file=out)
            return
        elif who == ("restore", "begin"):
            by = actor(me, v, admin_role, "AISRE_TOKEN")
            print(json.dumps({"epoch": admin.begin_restore(engine, by)}), file=out)
            return
        elif args.noun == "restore":  # report, complete
            look, rev = lookups or release_lookups(settings)
            recon = admin.restore_report(engine, look, rev)
            print(json.dumps(recon.__dict__), file=out)
            if args.command == "complete":
                by = actor(me, v, admin_role, "AISRE_TOKEN")
                sign = actor(lead, v, (Role.INCIDENT_LEAD,), "AISRE_SIGN_OFF_TOKEN")
                admin.complete_restore(engine, recon, sign, by, decisions(args.decide))
        print(json.dumps(admin.show(engine)), file=out)
    finally:
        engine.dispose()


def main(args: argparse.Namespace, env: Mapping[str, str], out: IO[str]) -> int:
    import sys

    try:
        run(args, env, out)
    except admin.Refused as e:
        print(f"aisre: refused: {e}", file=sys.stderr)
        return 1
    except (OSError, SQLAlchemyError) as e:
        print(f"aisre: {type(e).__name__}: {str(e)[:300]}", file=sys.stderr)
        return 1
    return 0
