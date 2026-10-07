"""Fixtures for the operations tests: two administrators and a user with a wallet."""

import pytest

from corridor.identity import Principal, User
from corridor.platform.db import Database
from tests.identity.support import add_user
from tests.payments.support import acting_as, add_person


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
async def maria(db: Database) -> User:
    """An ordinary user with a wallet in every asset."""
    async with db.transaction() as session:
        return await add_person(session, "maria")
