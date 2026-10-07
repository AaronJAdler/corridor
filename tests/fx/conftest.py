"""Fixtures for FX tests."""

from collections.abc import AsyncIterator

import pytest

from corridor.identity import User
from corridor.platform.config import Settings
from corridor.platform.db import Database, create_engine
from tests.fx.support import add_person


@pytest.fixture
async def maria(db: Database) -> User:
    async with db.transaction() as session:
        return await add_person(session, "maria")


@pytest.fixture
async def joao(db: Database) -> User:
    async with db.transaction() as session:
        return await add_person(session, "joao")


@pytest.fixture
async def impatient_db(settings: Settings) -> AsyncIterator[Database]:
    """The same database through connections that give up on a lock after 100 ms: how a
    test shows that a conversion waits for a lock instead of going round it."""
    impatient = Database(
        create_engine(
            settings.model_copy(update={"db_lock_timeout_ms": 100}),
            application_name="corridor-test-impatient",
        )
    )
    try:
        yield impatient
    finally:
        await impatient.dispose()
