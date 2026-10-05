"""Fixtures shared by the whole suite.

Every data-touching test runs against a real PostgreSQL and a real Redis. See
``tests/support/postgres.py`` for how each test gets a database of its own.
"""

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI

from corridor.api.app import create_app
from corridor.platform.clock import ManualClock, use_clock
from corridor.platform.config import Settings
from corridor.platform.db import Database, create_engine
from corridor.platform.redis import RedisStore, create_redis
from tests.support import postgres
from tests.support import redis as redis_support


@pytest.fixture(scope="session")
def database_template() -> str:
    """The name of the migrated template database, built once per session."""
    return postgres.ensure_template()


@pytest.fixture
def database(database_template: str) -> Iterator[postgres.TestDatabase]:
    """A freshly migrated database that belongs to this test alone."""
    created = postgres.create_database(database_template)
    try:
        yield created
    finally:
        postgres.drop_database(created)


@pytest.fixture
def settings(database: postgres.TestDatabase) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        log_level="INFO",
        database_url=database.app_url,
        database_owner_url=database.owner_url,
        database_app_role=postgres.APP_ROLE,
        db_pool_size=20,
        db_max_overflow=40,
        redis_url=redis_support.redis_url(),
        redis_key_prefix=redis_support.unique_prefix(),
    )


@pytest.fixture
async def db(settings: Settings) -> AsyncIterator[Database]:
    """The database as the application sees it: connected as the application role."""
    database = Database(create_engine(settings, application_name="corridor-test"))
    try:
        yield database
    finally:
        await database.dispose()


@pytest.fixture
async def owner_db(settings: Settings, database: postgres.TestDatabase) -> AsyncIterator[Database]:
    """Connected as the role that owns the schema. For tests about the schema itself."""
    owner_settings = settings.model_copy(update={"database_url": settings.database_owner_url})
    owner = Database(create_engine(owner_settings, application_name="corridor-test-owner"))
    try:
        yield owner
    finally:
        await owner.dispose()


@pytest.fixture
async def redis(settings: Settings) -> AsyncIterator[RedisStore]:
    store = RedisStore(create_redis(settings), settings.redis_key_prefix)
    try:
        yield store
    finally:
        await redis_support.delete_prefix(store.client, settings.redis_key_prefix)
        await store.close()


@pytest.fixture
def clock() -> Iterator[ManualClock]:
    """Freeze the application clock at a fixed instant; the test moves it explicitly."""
    with use_clock(ManualClock(datetime(2026, 1, 15, 12, 0, tzinfo=UTC))) as manual:
        assert isinstance(manual, ManualClock)
        yield manual


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    """The API, started: its lifespan has run, so its connections are open."""
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client wired straight to the app, with no network in between."""
    # An unhandled error is returned as the 500 response a real client would see.
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://corridor.test") as http:
        yield http
