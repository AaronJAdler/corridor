"""ASGI middleware.

Written against the ASGI interface directly rather than ``BaseHTTPMiddleware``, so that
context variables bound here are visible to the handler and to everything it logs.
"""

import re
import time
from typing import Final

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from corridor.api.errors import REQUEST_ID_HEADER
from corridor.platform.ids import new_id
from corridor.platform.logging import bind_context, clear_context, get_logger

log = get_logger("corridor.access")

# A client-supplied id is echoed into logs and a response header, so it is accepted only if
# it cannot carry anything but an identifier.
_SAFE_REQUEST_ID: Final = re.compile(r"[A-Za-z0-9._-]{8,64}")


class RequestContextMiddleware:
    """Give every request an id, bind it to the log context, and write one access line."""

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

        async def send_with_request_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            log.info(
                "http.request",
                method=scope["method"],
                route=route_template(scope),
                status=status,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            clear_context()


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
