"""Artifact 13.5 T5.1 to T5.4 (ADR-75): `aisre pilot ...`. aisre.cli parses; this runs.

pilot check --service S [--service S ...] [--alertmanager FILE]
    The go-live check (T5.1) on this process's environment: one PASS or FAIL line each,
    exit 1 on any FAIL. Never prints a setting's value.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from pathlib import Path
from typing import IO, Any

DEFAULT_ALERTMANAGER = "ops/compose/alertmanager.yml"


def parsers(top: Any) -> None:
    pilot = top.add_parser("pilot", help="Pilot readiness and measures (M5)")
    ps = pilot.add_subparsers(dest="command", required=True)
    c = ps.add_parser("check", help="Refuse unless Release A holds in these settings (T5.1)")
    c.add_argument("--service", action="append", default=[], help="A pilot service; repeat")
    c.add_argument("--alertmanager", default=DEFAULT_ALERTMANAGER, help="Alertmanager config")


def main(args: argparse.Namespace, env: Mapping[str, str], out: IO[str]) -> int:
    if args.command == "check":
        from aisre.pilot import check  # settings, gateway and release modules: only here

        results = check.run(env, args.service, Path(args.alertmanager))
        for r in results:
            print(r.line(), file=out)
        failed = sum(not r.ok for r in results)
        print(
            f"{len(results) - failed} passed, {failed} failed: "
            + ("Release A holds" if not failed else "refused"),
            file=out,
        )
        return 1 if failed else 0
    return 2
