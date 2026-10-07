"""Fixtures for the operations tests: two administrators and a user with a wallet."""

from collections.abc import AsyncIterator

import pytest

from corridor.identity import Principal, User
from corridor.platform.config import Settings
from corridor.platform.db import Database, create_engine
from tests.identity.support import add_user
from tests.payments.support import acting_as, add_person

# The simulated providers and the adapters wired to them, for the tests of reviews.
# Imported so that pytest finds them as fixtures of this directory.
from tests.support.providers import bank, custody, provider_settings, sim  # noqa: F401


@pytest.fixture
async def ana(db: Database) -> Principal:
    """An administrator, in her own session."""
    async with db.transaction() as session:
        return acting_as(await add_user(session, "ana", role="admin"))


@pytest.fixture
async def bruno(db: Database) -> Principal:
    """Another administrator."""
    async with db.transaction() as session:
        return acting_as(await add_user(session, "bruno", role="admin"))


@pytest.fixture
async def impatient_db(settings: Settings) -> AsyncIterator[Database]:
    """The same database through connections that give up on a lock after 100 ms: how a
    test shows that something waits for a lock."""
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


@pytest.fixture
async def maria(db: Database) -> User:
    """An ordinary user with a wallet in every asset."""
    async with db.transaction() as session:
        return await add_person(session, "maria")
