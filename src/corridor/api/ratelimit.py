"""Rate limiting for the API: a middleware for every request and dependencies for single routes.

The middleware and ``rate_limit`` count by client address, as the server reports it.
Uvicorn has already applied ``X-Forwarded-For`` from the proxies it trusts by the time a
request reaches the app. Nothing here reads that header, because any client can send it.
``money_rate_limit`` counts by who is acting instead: a user, or one of their agents.
"""

import ipaddress
from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Annotated, Final

from fastapi import Depends, Request
from starlette.types import ASGIApp, Receive, Scope, Send

from corridor.api.deps import get_container, get_principal
from corridor.api.errors import problem
from corridor.identity import Principal
from corridor.platform.config import Settings
from corridor.platform.errors import RateLimited, ServiceUnavailable
from corridor.platform.ratelimit import Limit, RateLimiter
from corridor.platform.redis import RedisStore

_GLOBAL_GROUP: Final = "global"
_MONEY_WRITE_GROUP: Final = "money_write"
_MONEY_READ_GROUP: Final = "money_read"

# A load balancer's health check must never be throttled: a refused check takes a healthy
# instance out of service. The metrics scrape is exempt so that it still answers while the
# limiter is refusing everything else from that address.
_EXEMPT_PATHS: Final = frozenset({"/healthz", "/readyz", "/metrics"})

# The subject for a connection whose address the server does not report, such as one over a
# Unix socket. Those requests share one bucket; they do not go uncounted.
_UNKNOWN_CLIENT: Final = "unknown"

# What one IPv6 customer is given at the least. A limit per address would be a limit that
# anyone with a /64 could step around 2^64 times.
_IPV6_CLIENT_PREFIX: Final = 64

# The methods that change nothing. A money route asked with any other is a write.
_READ_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})


class RateLimiterUnavailable(ServiceUnavailable):
    """A request that moves money could not be counted, because Redis could not be
    reached, and was refused rather than let through uncounted. Nothing was done."""

    code = "rate_limiter_unavailable"
    title = "Service unavailable"

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(
            "This cannot be done at the moment. Try again shortly.",
            headers={"Retry-After": str(max(1, retry_after_seconds))},
        )


def client_of(request: Request) -> str:
    """What a request's client is counted as: its IPv4 address, or its IPv6 /64.

    An IPv4 address written as an IPv6 one is the IPv4 address. Anything that is not an
    address at all is counted as it is given, and no address as ``unknown``.
    """
    client = request.client
    if client is None:
        return _UNKNOWN_CLIENT
    try:
        address = ipaddress.ip_address(client.host)
    except ValueError:
        return client.host
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        network = ipaddress.ip_network((address, _IPV6_CLIENT_PREFIX), strict=False)
        return str(network)
    return str(address)


class RateLimitMiddleware:
    """Limit every request by client, under the group ``global``."""

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
    """A dependency that gives one route a limit of its own, by client.

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


async def money_rate_limit(
    request: Request, principal: Annotated[Principal, Depends(get_principal)]
) -> None:
    """The limit on the routes that move money or prepare to, by who is acting.

    A user and each of their agents has a bucket of its own, so an agent that runs away
    uses up its own allowance and not its owner's. Reads and writes are counted apart.
    While Redis cannot be reached a read is served and a write is refused: what goes
    uncounted could be repeated without limit, and a write here moves money or calls a
    provider.
    """
    container = get_container(request)
    settings = container.settings
    if not settings.rate_limit_enabled:
        return
    writing = request.method not in _READ_METHODS
    decision = await _limiter_for(container.redis).check(
        _MONEY_WRITE_GROUP if writing else _MONEY_READ_GROUP,
        f"{principal.actor_type}:{principal.actor_id}",
        Limit.per_minute(
            settings.rate_limit_money_write_per_minute
            if writing
            else settings.rate_limit_money_read_per_minute
        ),
        fail_open=not writing,
    )
    if decision.unavailable:
        raise RateLimiterUnavailable(decision.retry_after_seconds)
    if not decision.allowed:
        raise RateLimited(decision.retry_after_seconds)


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
    decision = await _limiter_for(container.redis).check(
        group, client_of(request), Limit.per_minute(per_minute(settings))
    )
    return None if decision.allowed else RateLimited(decision.retry_after_seconds)


@lru_cache(maxsize=1)
def _limiter_for(store: RedisStore) -> RateLimiter:
    """The limiter for a Redis store, kept so that its script is prepared once and not for
    every request. A process has one store, so one is remembered."""
    return RateLimiter(store)
