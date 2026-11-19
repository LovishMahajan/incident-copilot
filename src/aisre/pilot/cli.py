"""Artifact 13.5 T5.1 to T5.4 (ADR-75): `aisre pilot ...`. aisre.cli parses; this runs.

    pilot check --service S [--service S ...] [--alertmanager FILE]
        The go-live check (T5.1) on this process's environment: one PASS or FAIL line each,
        exit 1 on any FAIL. Never prints a setting's value.
    pilot report --from T --to T [--out DIR]
        The pilot measures (T5.3) for incidents whose alert arrived in [from, to).
    pilot baseline --from T --to T [--first N] [--earlier FILE] [--out DIR]
        Artifact 1.6's baselines (T5.2): from FILE, earlier incident records as JSON, or else
        from the first N (default 30) reviewed change-caused incidents in the range.
    pilot approvals --from T --to T [--out DIR]
        The approval review (T5.4, Demo D3).

The reports read PostgreSQL as aisre_reporting (AISRE_REPORTING_DATABASE_URL) and refuse any
other role. Without --out they print Markdown; with it they write <name>.md and <name>.json
there. They say "synthetic" unless AISRE_ENV is production (aisre.pilot.report.is_pilot).
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

DEFAULT_ALERTMANAGER = "ops/compose/alertmanager.yml"


def _time(value: str) -> datetime:
    t = datetime.fromisoformat(value)
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def parsers(top: Any) -> None:
    pilot = top.add_parser("pilot", help="Pilot readiness and measures (M5)")
    ps = pilot.add_subparsers(dest="command", required=True)
    c = ps.add_parser("check", help="Refuse unless Release A holds in these settings (T5.1)")
    c.add_argument("--service", action="append", default=[], help="A pilot service; repeat")
    c.add_argument("--alertmanager", default=DEFAULT_ALERTMANAGER, help="Alertmanager config")
    for name, what in (
        ("report", "The pilot measures (T5.3)"),
        ("baseline", "Artifact 1.6's baselines (T5.2)"),
        ("approvals", "The approval review (T5.4, D3)"),
    ):
        r = ps.add_parser(name, help=what)
        r.add_argument("--from", dest="start", type=_time, required=True)
        r.add_argument("--to", dest="end", type=_time, required=True)
        r.add_argument("--out", help="Write <name>.md and <name>.json here")
        if name == "baseline":
            r.add_argument("--first", type=int, default=30)
            r.add_argument("--earlier", help="Earlier incident records, a JSON list")


def _write(out: IO[str], where: str | None, name: str, markdown: str, as_json: str) -> None:
    if where is None:
        print(markdown, file=out)
        return
    folder = Path(where)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.md").write_text(markdown, encoding="utf-8")
    (folder / f"{name}.json").write_text(as_json + "\n", encoding="utf-8")
    print(f"wrote {folder / name}.md and .json", file=out)


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

    import sys

    from sqlalchemy.exc import SQLAlchemyError

    from aisre.config import Settings
    from aisre.pilot import approval_review, report
    from aisre.record import make_engine

    if args.end <= args.start:
        print("aisre: --to must be after --from", file=sys.stderr)
        return 2
    url = Settings.from_env(env).reporting_database_url
    if url is None:
        print("aisre: set AISRE_REPORTING_DATABASE_URL (the aisre_reporting role)", file=sys.stderr)
        return 1
    engine = make_engine(url)
    try:
        with engine.connect() as conn:
            if args.command == "approvals":
                rows = approval_review.read(conn, args.start, args.end)
                dev = any(d.development_key for r in rows for d in r.decisions)
                label = "pilot" if env.get("AISRE_ENV") == "production" and not dev else "synthetic"
                rep = approval_review.report(rows, label=label, start=args.start, end=args.end)
                _write(
                    out,
                    args.out,
                    "approval-review",
                    approval_review.to_markdown(rep),
                    approval_review.to_json(rep),
                )
                return 0
            records = report.read(conn, args.start, args.end)
    except report.NotReadOnly as e:
        print(f"aisre: {e}", file=sys.stderr)
        return 1
    except SQLAlchemyError as e:
        print(f"aisre: database: {type(e).__name__}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    label = "pilot" if report.is_pilot(env, records) else "synthetic"
    if args.command == "report":
        rep = report.pilot_report(records, label=label, start=args.start, end=args.end)
        _write(out, args.out, "pilot-report", report.to_markdown(rep), report.to_json(rep))
        return 0
    if args.earlier:
        rows_in = json.loads(Path(args.earlier).read_text(encoding="utf-8"))
        earlier = report.earlier_from_json(rows_in)
        source, wanted = f"earlier incident records ({args.earlier})", None
    else:
        earlier = report.from_pilot(records, args.first)
        source = f"the first {args.first} reviewed change-caused incidents in the range"
        wanted = args.first
    rep = report.baseline_report(earlier, label=label, source=source, wanted=wanted)
    _write(out, args.out, "baseline", report.to_markdown(rep), report.to_json(rep))
    return 0
