"""The application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from corridor import __version__
from corridor.api import health
from corridor.api.container import Container
from corridor.api.errors import install_error_handlers
from corridor.api.middleware import RequestContextMiddleware
from corridor.api.ratelimit import RateLimitMiddleware
from corridor.platform.config import Settings, load_settings
from corridor.platform.db import Database, create_engine
from corridor.platform.logging import configure_logging
from corridor.platform.redis import RedisStore, create_redis


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the API. Connections are opened when the app starts and closed when it stops."""
    resolved = settings if settings is not None else load_settings()
    # Here rather than only in the command line, so the redacting pipeline is in place
    # however the app is started.
    configure_logging(resolved.log_level, resolved.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = Database(create_engine(resolved, application_name="corridor-api"))
        redis = RedisStore(create_redis(resolved), resolved.redis_key_prefix)
        app.state.container = Container(settings=resolved, db=db, redis=redis)
        try:
            yield
        finally:
            await redis.close()
            await db.dispose()

    interactive_docs = resolved.environment != "production"
    app = FastAPI(
        title="Corridor",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if interactive_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if interactive_docs else None,
    )
    # The last middleware added is the outermost. The request context goes on last so that
    # a request refused by the rate limiter still has an id and an access-log line.
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)
    app.include_router(health.router)
    return app
