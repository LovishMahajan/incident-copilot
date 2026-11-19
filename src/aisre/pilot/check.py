"""Artifact 13.5 T5.1 (ADR-44, ADR-75): the go-live check, `aisre pilot check`.

Given the production settings (the process environment the API, worker and Test Runner will
run with) and the pilot's services, it refuses unless Release A holds: advisory only, with
nothing local, fake or development-only configured. Each check is one line, PASS or FAIL, and
any FAIL makes the command exit 1.

It never prints a value: a check names environment variables and what is wrong with them,
nothing they hold. Settings are read twice: once as given, which must load in production,
and once with `AISRE_ENV=test`, so the remaining checks still read typed values when the
production refusals in `Settings` fire; each refusal is then also its own FAIL line.

What it can't see: whether a credential that is set is valid, whether a source answers, or
whether a page reaches a person. Those are the pilot's first-day checks (docs/onboarding.md).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import ValidationError

from aisre.config import ENV_VARS, Settings
from aisre.evidence.sources import SOURCE_TOOLS, SourceSet
from aisre.gateway.adapters import CLAUDE_MODEL_IDS
from aisre.gateway.gateway_contracts import TASK_MODEL_ALIAS
from aisre.remediation.release import remediation_enabled

PILOT_ENVIRONMENT = "production"
# Artifact 12.3's three paging roles; each needs a receiver (ops/compose/alertmanager.yml).
PAGING_ROLES = ("platform_owner", "incident_lead", "security_lead")
FAKE_PAGER = "fake-pager"
_FIELD_VAR = {field: var for field, var in ENV_VARS.items()}


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""

    def line(self) -> str:
        return f"{'PASS' if self.ok else 'FAIL'}  {self.name}" + (
            f": {self.detail}" if self.detail else ""
        )


def _errors(e: ValidationError) -> str:
    """The validation messages, by variable, without the input (which may be a secret)."""
    parts = []
    for err in e.errors(include_input=False, include_url=False):
        var = ", ".join(_FIELD_VAR.get(str(loc), str(loc)) for loc in err["loc"])
        parts.append(f"{var}: {err['msg']}" if var else str(err["msg"]))
    return "; ".join(parts)


def _set(environ: Mapping[str, str], var: str) -> bool:
    return bool(environ.get(var))


def _fake_host(url: str | None) -> bool:
    host = urlparse(url or "").hostname or ""
    return "fake" in host or host in ("localhost", "127.0.0.1", "::1")


def check_settings(environ: Mapping[str, str], services: Sequence[str]) -> list[Check]:
    out = [
        Check(
            "AISRE_ENV is production",
            environ.get("AISRE_ENV") == PILOT_ENVIRONMENT,
            "" if environ.get("AISRE_ENV") == PILOT_ENVIRONMENT else "set AISRE_ENV=production",
        )
    ]
    try:
        Settings.from_env(environ)
        out.append(Check("settings load as given", True))
    except ValidationError as e:
        out.append(Check("settings load as given", False, _errors(e)))

    # Read from the variables themselves, so they are checked even when settings don't load.
    def unset(name: str, wanted: Sequence[str]) -> Check:
        found = [v for v in wanted if _set(environ, v)]
        return Check(name, not found, f"unset {', '.join(found)}" if found else "")

    release_vars = sorted(v for v in environ if v.startswith("AISRE_RELEASE_"))
    out.append(unset("no release or production write credential (ADR-44)", release_vars))
    out.append(unset("no fake release process", ["AISRE_RELEASE_ADAPTER"]))
    out.append(
        unset(
            "no development signing key (ADR-73)",
            ["AISRE_APPROVAL_SIGNING_KEY", "AISRE_APPROVAL_VERIFY_KEY"],
        )
    )
    fakes = ["AISRE_IDENTITY_DIRECTORY_URL", "AISRE_VERIFIER_MONITORING_URL"]
    if environ.get("AISRE_REVIEW_ADAPTER") == "fake_review":
        fakes.insert(0, "AISRE_REVIEW_ADAPTER")
    out.append(unset("no fake review system, identity directory or monitoring source", fakes))
    raw_release: dict[str, Any] = {
        "env": environ.get("AISRE_ENV") or "development",
        "release_adapter": environ.get("AISRE_RELEASE_ADAPTER") or None,
    }
    out.append(
        Check(
            "remediation off: a tested fix is handed over, never sent for approval",
            not remediation_enabled(  # the workflow's own rule, on the raw variables
                Settings.model_construct(**raw_release)
            ),
        )
    )

    try:
        s = Settings.from_env({**environ, "AISRE_ENV": "test"})
    except ValidationError as e:
        out.append(Check("settings readable", False, _errors(e)))
        return out
    out.append(
        Check(
            "no scripted skills",
            not s.scripted_skills,
            "unset AISRE_SCRIPTED_SKILLS" if s.scripted_skills else "",
        )
    )
    out.append(
        Check(
            "sandbox runtime is runsc (gVisor), not runc (ADR-71)",
            s.sandbox_runtime == "runsc",
            "" if s.sandbox_runtime == "runsc" else "set AISRE_SANDBOX_RUNTIME=runsc",
        )
    )
    out.append(_model(s))
    out += _evidence(s, services, environ)
    return out


def _model(s: Settings) -> Check:
    name = "real model: provider key set, provider's own URL, every task's alias pinned"
    problems = []
    key = s.provider_api_key.get_secret_value() if s.provider_api_key else ""
    if not key:
        problems.append("set AISRE_PROVIDER_API_KEY")
    elif key.startswith("fake"):
        problems.append("AISRE_PROVIDER_API_KEY is a fake key")
    if s.provider_url is not None and (
        _fake_host(s.provider_url) or not s.provider_url.startswith("https://")
    ):
        problems.append("AISRE_PROVIDER_URL is not the provider's (unset it, or https)")
    unpinned = sorted({a for a in TASK_MODEL_ALIAS.values() if a not in CLAUDE_MODEL_IDS})
    if unpinned:
        problems.append(f"aliases without a pinned model: {', '.join(unpinned)}")
    return Check(name, not problems, "; ".join(problems))


def _evidence(s: Settings, services: Sequence[str], environ: Mapping[str, str]) -> list[Check]:
    if s.evidence_sources is None:
        return [Check("evidence sources configured", False, "set AISRE_EVIDENCE_SOURCES")]
    try:
        sources = SourceSet.from_config(s.evidence_sources, production=True)
    except (ValueError, TypeError) as e:
        return [Check("evidence sources load in production", False, str(e))]
    out = [Check("evidence sources load in production", True)]
    adapters = [
        *sources.sources.values(),
        *(a for m in sources.services.values() for a in m.values()),
    ]
    refs = {r.describe(): r for a in adapters for r in getattr(a, "credentials", ())}
    absent = sorted(d for d, r in refs.items() if not r.present(environ))
    out.append(
        Check(
            "evidence credentials referenced are present (not that they work)",
            not absent,
            f"missing: {'; '.join(absent)}" if absent else "",
        )
    )
    if not services:
        out.append(Check("pilot services named", False, "name each with --service"))
    for service in services:
        missing = [
            t for t in SOURCE_TOOLS if sources.for_tool(t, service, PILOT_ENVIRONMENT) is None
        ]
        out.append(
            Check(
                f"evidence sources for {service} in {PILOT_ENVIRONMENT}",
                not missing,
                f"no source for {', '.join(missing)}" if missing else "",
            )
        )
    return out


_RECEIVER = re.compile(r"^\s*-\s*name:\s*['\"]?([\w-]+)['\"]?\s*$")
# A receiver's destination keys in Alertmanager's integrations (webhook, PagerDuty, Opsgenie,
# Slack and the like), each with its `_file` form.
_KEYS = ("url", "routing_key", "service_key", "api_key", "api_url", "webhook_url")
_TARGET = re.compile(r"^\s*-?\s*(" + "|".join(f"{k}|{k}_file" for k in _KEYS) + r"):")


def check_paging(path: Path) -> list[Check]:
    """The Alertmanager configuration routes to a receiver for each paging role, and none of
    them is the fake pager (ADR-74). A line scan, not a YAML parser: it reads the `receivers:`
    list in the shape ops/compose/alertmanager.yml has. It can't tell that a receiver works."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return [Check("paging configured", False, f"cannot read {path}: {type(e).__name__}")]
    lines = text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.startswith("receivers:"))
    except StopIteration:
        return [Check("paging configured", False, f"{path} has no receivers")]
    targets: dict[str, int] = {}
    current = None
    for ln in lines[start + 1 :]:
        if ln and not ln[0].isspace():
            break
        if m := _RECEIVER.match(ln):
            current = m.group(1)
            targets.setdefault(current, 0)
        elif current and _TARGET.match(ln):
            targets[current] += 1
    missing = [r for r in PAGING_ROLES if not targets.get(r)]
    out = [
        Check(
            "paging configured: a receiver for each role",
            not missing,
            f"no receiver target for {', '.join(missing)}" if missing else "",
        )
    ]
    out.append(
        Check(
            "no fake pager (ADR-74)",
            FAKE_PAGER not in text,
            f"{path} still sends to the fake pager" if FAKE_PAGER in text else "",
        )
    )
    return out


def run(environ: Mapping[str, str], services: Sequence[str], alertmanager: Path) -> list[Check]:
    return check_settings(environ, services) + check_paging(alertmanager)
