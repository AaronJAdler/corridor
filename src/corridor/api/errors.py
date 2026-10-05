"""Error rendering: every error leaves the API as an RFC 9457 problem document.

The body always carries a stable machine-readable ``code`` and the request id. It never
carries an exception message, a stack trace or anything the client sent.
"""

from collections.abc import Mapping
from http import HTTPStatus
from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from corridor.platform.errors import DomainError
from corridor.platform.logging import get_logger

PROBLEM_CONTENT_TYPE = "application/problem+json"
PROBLEM_TYPE_BASE = "https://corridor.example/problems/"
REQUEST_ID_HEADER = "X-Request-ID"

log = get_logger(__name__)

_STATUS_CODES = {
    400: "bad_request",
    401: "unauthenticated",
    403: "permission_denied",
    404: "not_found",
    405: "method_not_allowed",
    413: "payload_too_large",
    415: "unsupported_media_type",
}


def request_id_of(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else None


def problem(
    request: Request,
    *,
    status: int,
    code: str,
    title: str,
    detail: str | None = None,
    headers: Mapping[str, str] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": PROBLEM_TYPE_BASE + code.replace("_", "-"),
        "title": title,
        "status": status,
        "code": code,
    }
    if detail is not None:
        body["detail"] = detail
    request_id = request_id_of(request)
    if request_id is not None:
        body["request_id"] = request_id
    # Extension members never replace the standard ones.
    body.update({key: value for key, value in (extra or {}).items() if key not in body})

    response_headers = dict(headers or {})
    if request_id is not None:
        # The outermost error handler runs outside the request middleware, so the header is
        # set here as well as there.
        response_headers[REQUEST_ID_HEADER] = request_id
    return JSONResponse(
        body, status_code=status, headers=response_headers, media_type=PROBLEM_CONTENT_TYPE
    )


async def _domain_error(request: Request, exc: Exception) -> JSONResponse:
    error = cast(DomainError, exc)
    return problem(
        request,
        status=error.status,
        code=error.code,
        title=error.title,
        detail=error.detail,
        headers=error.headers,
        extra=error.extra,
    )


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    error = cast(RequestValidationError, exc)
    # Location and message only. The rejected input is left out: it may be a password.
    errors = [
        {
            "field": ".".join(str(part) for part in item["loc"]),
            "message": item["msg"],
            "type": item["type"],
        }
        for item in error.errors()
    ]
    return problem(
        request,
        status=422,
        code="invalid_request",
        title="Invalid request",
        detail="The request did not match the schema for this endpoint.",
        extra={"errors": errors},
    )


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    error = cast(HTTPException, exc)
    status = error.status_code
    title = HTTPStatus(status).phrase if status in HTTPStatus._value2member_map_ else "Error"
    return problem(
        request,
        status=status,
        code=_STATUS_CODES.get(status, "http_error"),
        title=title,
        headers=dict(error.headers) if error.headers else None,
    )


async def _unexpected_error(request: Request, error: Exception) -> JSONResponse:
    # This handler runs outside the request middleware, after the log context is cleared,
    # so the request id is passed explicitly.
    log.error("request.unhandled_error", request_id=request_id_of(request), exc_info=error)
    return problem(
        request,
        status=500,
        code="internal_error",
        title="Internal server error",
        detail="The request could not be completed. Quote the request id when reporting this.",
    )


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DomainError, _domain_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(HTTPException, _http_error)
    app.add_exception_handler(Exception, _unexpected_error)
