"""The application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from corridor import __version__, identity, webhooks
from corridor.api import health
from corridor.api.container import Container
from corridor.api.errors import install_error_handlers
from corridor.api.middleware import RequestContextMiddleware
from corridor.api.ratelimit import RateLimitMiddleware
from corridor.api.routers import ROUTERS
from corridor.platform.config import Settings, load_settings
from corridor.platform.db import Database, create_engine
from corridor.platform.logging import configure_logging
from corridor.platform.redis import RedisStore, create_redis
from corridor.providers import SimBank, SimCustody


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the API. Connections are opened when the app starts and closed when it stops."""
    resolved = settings if settings is not None else load_settings()
    # Here rather than only in the command line, so the redacting pipeline is in place
    # however the app is started.
    configure_logging(resolved.log_level, resolved.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # First, and before any connection is opened: a process that cannot sign or verify
        # a token must not start, and the error says which setting is wrong.
        keys = identity.load_keyset(resolved)
        hasher = identity.PasswordHasher(resolved)
        # With one secret for both providers, an event signed by one would verify on the
        # other's path. That is refused here, before a single delivery can be accepted.
        webhooks.validate_secrets(resolved)
        # A provider with an address and no key refuses to be built, as a missing signing
        # key does. One with no address is left out, and the API runs without it.
        bank = SimBank(resolved) if resolved.bank_rail_url else None
        custody = SimCustody(resolved) if resolved.custody_url else None
        db = Database(create_engine(resolved, application_name="corridor-api"))
        redis = RedisStore(create_redis(resolved), resolved.redis_key_prefix)
        app.state.container = Container(
            settings=resolved,
            db=db,
            redis=redis,
            keys=keys,
            hasher=hasher,
            bank=bank,
            custody=custody,
        )
        try:
            yield
        finally:
            await redis.close()
            await db.dispose()
            if bank is not None:
                await bank.aclose()
            if custody is not None:
                await custody.aclose()

    interactive_docs = resolved.environment != "production"
    app = FastAPI(
        title="Corridor",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if interactive_docs else None,
        redoc_url=None,
        # No OAuth2 flow is offered, so the page that would receive its redirect is not served.
        swagger_ui_oauth2_redirect_url=None,
        openapi_url="/openapi.json" if interactive_docs else None,
    )
    # The last middleware added is the outermost. The request context goes on last so that
    # a request refused by the rate limiter still has an id and an access-log line.
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)
    app.include_router(health.router)
    for router in ROUTERS:
        app.include_router(router)
    return app
