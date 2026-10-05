"""Rate limiting for the API: a middleware for every request and a dependency for single routes."""

from starlette.types import ASGIApp, Receive, Scope, Send


class RateLimitMiddleware:
    """Limit every request by client address. Placeholder: passes everything through."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.app(scope, receive, send)
