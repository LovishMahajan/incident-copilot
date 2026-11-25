"""Artifact 11.9, 13.5 T3.1 (ADR-44, ADR-73): the release process adapter, the only write interface.

`FakeReleaseClient` speaks to Compose's fake release process (ops/fakes/release.py), which stands
in for the real release process until one is confirmed (T3.1 stays partial). It implements
dispatcher.ReleaseProcess: `current_state`, then `apply`, `lookup` by the execution key (the
action ID, ADR-47). A fix is an apply carrying the patch's canonical bytes, which the client reads
through `patch_content` (the dispatcher's own reader, so the bytes sent are the bytes it hashed,
ST-13); a rollback has no patch and is the release process's undo to the revision it names.

`github_actions` (aisre.remediation.github, ADR-75) is the real release process's adapter, for
T5.5's scenario revert in a test environment. There is no production adapter: no credential for
a production write exists until HG-10 (ADR-44). `Settings` refuses every release adapter in
production, `release_from_settings` refuses there too and without an adapter or its credential,
so the dispatcher process refuses to start (aisre.remediation.dispatcher_process).

HTTP is urllib through `asyncio.to_thread`, as the review client does. No answer, or a 5xx, is a
ConnectionError: the dispatcher records the outcome as unknown and reconciles, never re-sends.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from pydantic import SecretStr

from aisre.config import Settings
from aisre.remediation.dispatcher import ReleaseChange, TargetState

if TYPE_CHECKING:
    from aisre.remediation.github import GitHubActionsRelease

TIMEOUT_S = 10.0
PRODUCTION_REFUSED = (
    "no production write credential exists until HG-10 (ADR-44): release adapters run in "
    "development and test only"
)


class ReleaseRefused(RuntimeError):
    """No release adapter may be built here."""


class FakeReleaseClient:
    """dispatcher.ReleaseProcess over the fake release process's HTTP API, with its token."""

    def __init__(
        self,
        url: str,
        token: SecretStr,
        patch_content: Callable[[str], bytes],
        timeout_s: float = TIMEOUT_S,
    ) -> None:
        self.url, self.token, self.timeout_s = url.rstrip("/"), token, timeout_s
        self.patch_content = patch_content

    def _call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        req = urllib.request.Request(
            self.url + path,
            data=None if body is None else json.dumps(body).encode(),
            method=method,
            headers={
                "Authorization": f"Bearer {self.token.get_secret_value()}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return int(resp.status), json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            if e.code >= 500:
                raise ConnectionError(f"release process: HTTP {e.code}") from None
            try:
                return e.code, json.loads(e.read() or b"null")
            except ValueError:
                return e.code, None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise ConnectionError(f"release process: {type(e).__name__}") from None

    @staticmethod
    def _dep(deployment: str) -> str:
        return f"/v1/deployments/{quote(deployment, safe='')}"

    async def current_state(self, deployment: str) -> TargetState:
        status, body = await asyncio.to_thread(self._call, "GET", self._dep(deployment))
        if status != 200 or not isinstance(body, dict):
            raise ConnectionError(f"release process: HTTP {status} reading {deployment}")
        return TargetState(str(body["revision"]), body.get("in_progress_key"))

    async def apply(
        self,
        deployment: str,
        execution_key: str,
        base_revision: str,
        patch_sha256: str | None,
        to_revision: str,
    ) -> str:
        """ "applied", "rejected" or "in_progress". A rollback (no patch) is the undo."""
        body: dict[str, Any] = {
            "execution_key": execution_key,
            "base_revision": base_revision,
            "to_revision": to_revision,
        }
        if patch_sha256 is not None:
            content = await asyncio.to_thread(self.patch_content, patch_sha256)
            body |= {"patch_sha256": patch_sha256, "patch": content.decode()}
        op = "apply" if patch_sha256 is not None else "undo"
        status, answer = await asyncio.to_thread(
            self._call, "POST", f"{self._dep(deployment)}/{op}", body
        )
        if status != 200 or not isinstance(answer, dict):
            return "rejected"  # refused before anything ran: 4xx (5xx raised above)
        return str(answer.get("result", "rejected"))

    async def history(self, deployment: str) -> list[ReleaseChange]:
        """The change history (T3.11): what ran when, and the key each platform change carried.
        A deployment the release process doesn't know has none."""
        status, body = await asyncio.to_thread(self._call, "GET", self._dep(deployment))
        if status == 404:
            return []
        if status != 200 or not isinstance(body, dict):
            raise ConnectionError(f"release process: HTTP {status} reading {deployment}")
        return [
            ReleaseChange(
                deployment,
                datetime.fromisoformat(str(h["at"]).replace("Z", "+00:00")),
                str(h.get("kind") or "unknown"),
                str(h["revision"]),
                str(h["execution_key"]) if h.get("execution_key") else None,
            )
            for h in body.get("history") or []
        ]

    async def lookup(self, deployment: str, execution_key: str) -> str | None:
        path = f"{self._dep(deployment)}/changes/{quote(execution_key, safe='')}"
        status, body = await asyncio.to_thread(self._call, "GET", path)
        if status == 404:
            return None
        if status != 200 or not isinstance(body, dict):
            raise ConnectionError(f"release process: HTTP {status} on lookup")
        return str(body["status"])


def release_from_settings(
    settings: Settings, patch_content: Callable[[str], bytes]
) -> FakeReleaseClient | GitHubActionsRelease:
    """The configured adapter. Raises ReleaseRefused in production, when none is set, or with no
    write credential: nothing is built that could write without one."""
    if settings.env == "production":
        raise ReleaseRefused(PRODUCTION_REFUSED)
    if settings.release_adapter is None or not settings.release_url:
        raise ReleaseRefused("AISRE_RELEASE_ADAPTER and AISRE_RELEASE_URL are required")
    if settings.release_token is None or not settings.release_token.get_secret_value():
        raise ReleaseRefused("AISRE_RELEASE_TOKEN, the write credential, is required")
    if settings.release_adapter == "github_actions":
        from aisre.remediation.github import GitHubActionsRelease

        gh: dict[str, Any] = dict(settings.release_github or {})
        try:
            return GitHubActionsRelease(
                settings.release_url, settings.release_token, patch_content, **gh
            )
        except (TypeError, ValueError) as e:
            raise ReleaseRefused(f"AISRE_RELEASE_GITHUB: {e}") from None
    return FakeReleaseClient(settings.release_url, settings.release_token, patch_content)


def remediation_enabled(settings: Settings) -> bool:
    """ADR-73: the workflow asks for approval instead of handing over a tested fix only in
    development or test, with a release adapter configured. Else Release A (ADR-44)."""
    return settings.env in ("development", "test") and settings.release_adapter is not None
