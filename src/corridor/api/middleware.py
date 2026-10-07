"""ASGI middleware.

Written against the ASGI interface directly rather than ``BaseHTTPMiddleware``, so that
context variables bound here are visible to the handler and to everything it logs.
"""

import re
import time
from typing import Final

from fastapi import Request
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from corridor.api.errors import REQUEST_ID_HEADER, problem
from corridor.api.headers import apply_security_headers
from corridor.platform.ids import new_id
from corridor.platform.logging import bind_context, clear_context, get_logger
from corridor.platform.metrics import HTTP_REQUEST_SECONDS, HTTP_REQUESTS

log = get_logger("corridor.access")

# A client-supplied id is echoed into logs and a response header, so it is accepted only if
# it cannot carry anything but an identifier.
_SAFE_REQUEST_ID: Final = re.compile(r"[A-Za-z0-9._-]{8,64}")

# The methods a metric is labelled with. A client can send any word as a method, and each
# new label value is a time series that is kept for as long as the process lives.
_KNOWN_METHODS: Final = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
_OTHER_METHOD: Final = "OTHER"


class RequestContextMiddleware:
    """Give every request an id, bind it to the log context, write one access line, count
    it, and put the security headers on whatever is answered."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _request_id(scope)
        scope.setdefault("state", {})["request_id"] = request_id
        clear_context()
        bind_context(request_id=request_id)

        status = 500
        started = time.perf_counter()

        async def send_with_headers(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = MutableHeaders(scope=message)
                headers[REQUEST_ID_HEADER] = request_id
                apply_security_headers(headers, path=scope["path"])
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        finally:
            elapsed = time.perf_counter() - started
            method: str = scope["method"]
            route = route_template(scope)
            log.info(
                "http.request",
                method=method,
                route=route,
                status=status,
                duration_ms=round(elapsed * 1000, 2),
            )
            counted_method = method if method in _KNOWN_METHODS else _OTHER_METHOD
            HTTP_REQUESTS.labels(method=counted_method, route=route, status=str(status)).inc()
            HTTP_REQUEST_SECONDS.labels(method=counted_method, route=route).observe(elapsed)
            clear_context()


class _BodyTooLarge(Exception):
    """Raised into whatever is reading a request body when it passes the limit."""


class BodyLimitMiddleware:
    """Refuse a request whose body is longer than ``max_request_body_bytes``.

    Nothing has parsed the body at this point. A request that declares its length is
    refused on the declaration, with none of it read. One that does not, or that
    understates it, is refused as soon as the bytes that arrive pass the limit, so what
    the server ever holds of a body is bounded by the limit and one chunk.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        limit: int = request.app.state.container.settings.max_request_body_bytes
        declared = _declared_length(scope)
        if declared is not None and declared > limit:
            await _too_large(request, limit)(scope, receive, send)
            return

        received = 0
        refused = False
        answering = False

        async def counted_receive() -> Message:
            nonlocal received, refused
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    refused = True
                    raise _BodyTooLarge
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal answering
            if refused:
                # Whatever the application makes of a body it was not allowed to finish
                # reading, such as "could not parse the body", is not what is answered.
                return
            answering = True
            await send(message)

        try:
            await self.app(scope, counted_receive, guarded_send)
        except Exception:
            # Raised by the limit, perhaps wrapped by whatever was reading. Anything else
            # is not this middleware's to handle.
            if not refused:
                raise
        if refused and not answering:
            await _too_large(request, limit)(scope, receive, send)


def _declared_length(scope: Scope) -> int | None:
    for name, value in scope["headers"]:
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                # Not a length. The body is counted as it arrives instead.
                return None
    return None


def _too_large(request: Request, limit: int) -> ASGIApp:
    # The API's error handlers sit inside this middleware, so the problem document is
    # built directly.
    return problem(
        request,
        status=413,
        code="payload_too_large",
        title="Payload too large",
        detail=f"A request body can be at most {limit} bytes.",
    )


def _request_id(scope: Scope) -> str:
    for name, value in scope["headers"]:
        if name == b"x-request-id":
            candidate: str = value.decode("latin-1")
            if _SAFE_REQUEST_ID.fullmatch(candidate):
                return candidate
            break
    return str(new_id())


def route_template(scope: Scope) -> str:
    """The matched route's path template, e.g. ``/v1/transfers/{transfer_id}``.

    Logs and metrics are labelled with the template, never the raw path: the raw path has
    unbounded cardinality and can contain identifiers.
    """
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "unmatched"
