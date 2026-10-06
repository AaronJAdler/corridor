"""Rate limiting for the API: a middleware for every request and a dependency for single routes.

Both count by client address, as the server reports it. Uvicorn has already applied
``X-Forwarded-For`` from the proxies it trusts by the time a request reaches the app.
Nothing here reads that header, because any client can send it.
"""

from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Final

from fastapi import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from corridor.api.deps import get_container
from corridor.api.errors import problem
from corridor.platform.config import Settings
from corridor.platform.errors import RateLimited
from corridor.platform.ratelimit import Limit, RateLimiter
from corridor.platform.redis import RedisStore

_GLOBAL_GROUP: Final = "global"

# A load balancer's health check must never be throttled: a refused check takes a healthy
# instance out of service. The metrics scrape is exempt so that it still answers while the
# limiter is refusing everything else from that address.
_EXEMPT_PATHS: Final = frozenset({"/healthz", "/readyz", "/metrics"})

# The subject for a connection whose address the server does not report, such as one over a
# Unix socket. Those requests share one bucket; they do not go uncounted.
_UNKNOWN_CLIENT: Final = "unknown"


class RateLimitMiddleware:
    """Limit every request by client address, under the group ``global``."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in _EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        refusal = await _refusal(request, _GLOBAL_GROUP, _global_limit)
        if refusal is None:
            await self.app(scope, receive, send)
            return

        # The API's error handlers sit inside this middleware, so an exception raised here
        # would not reach them. The problem document is built directly instead.
        response = problem(
            request,
            status=refusal.status,
            code=refusal.code,
            title=refusal.title,
            detail=refusal.detail,
            headers=refusal.headers,
            extra=refusal.extra,
        )
        await response(scope, receive, send)


def rate_limit(
    group: str, *, per_minute: Callable[[Settings], int]
) -> Callable[[Request], Awaitable[None]]:
    """A dependency that gives one route a limit of its own, by client address.

    The limit is read from the settings on each request, so nothing is needed when the
    route is declared::

        Depends(rate_limit("auth", per_minute=lambda s: s.rate_limit_auth_per_minute))

    The global limit still applies to the route. This one is counted separately, under
    ``group``.
    """

    async def enforce(request: Request) -> None:
        refusal = await _refusal(request, group, per_minute)
        if refusal is not None:
            raise refusal

    return enforce


def _global_limit(settings: Settings) -> int:
    return settings.rate_limit_per_minute


async def _refusal(
    request: Request, group: str, per_minute: Callable[[Settings], int]
) -> RateLimited | None:
    """Count this request against its client's bucket in ``group``.

    Returns the refusal if the bucket cannot pay for the request, and None if the request
    may go on. The error is returned and not raised because the two callers differ: the
    dependency raises it, the middleware has to render it.
    """
    container = get_container(request)
    settings = container.settings
    if not settings.rate_limit_enabled:
        return None
    client = request.client
    decision = await _limiter_for(container.redis).check(
        group,
        client.host if client is not None else _UNKNOWN_CLIENT,
        Limit.per_minute(per_minute(settings)),
    )
    return None if decision.allowed else RateLimited(decision.retry_after_seconds)


@lru_cache(maxsize=1)
def _limiter_for(store: RedisStore) -> RateLimiter:
    """The limiter for a Redis store, kept so that its script is prepared once and not for
    every request. A process has one store, so one is remembered."""
    return RateLimiter(store)
