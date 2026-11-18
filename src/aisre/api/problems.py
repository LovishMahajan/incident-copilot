"""Artifact 7.4 (ADR-23): errors as RFC 9457 problem details, the `Problem` contract."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from aisre.contracts import Problem

PROBLEM_TYPE = "https://errors.aisre.internal/"
MEDIA_TYPE = "application/problem+json"


class ApiError(Exception):
    """Raised by a route; the app turns it into a Problem response."""

    def __init__(
        self,
        status: int,
        code: str,
        title: str,
        detail: str | None = None,
        *,
        errors: list[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(f"{status} {code}")
        self.status, self.code, self.title, self.detail = status, code, title, detail
        self.errors, self.headers = errors, headers


def problem_response(request: Request, err: ApiError) -> JSONResponse:
    body = Problem(
        type=PROBLEM_TYPE + err.code,
        title=err.title,
        status=err.status,
        detail=err.detail,
        instance=request.url.path,
        code=err.code,
        request_id=getattr(request.state, "request_id", ""),
        errors=err.errors,
    )
    return JSONResponse(
        body.model_dump(mode="json", exclude_none=True),
        status_code=err.status,
        media_type=MEDIA_TYPE,
        headers=err.headers,
    )


def field_errors(errors: Any) -> list[dict[str, Any]]:
    """Pydantic's errors, without the rejected input (it may hold anything)."""
    return [
        {"loc": [str(p) for p in e.get("loc", ())], "msg": e.get("msg", ""), "type": e.get("type")}
        for e in errors
    ]
