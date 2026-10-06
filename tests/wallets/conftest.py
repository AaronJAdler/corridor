"""Fixtures for wallet tests."""

import uuid

import pytest

from corridor import wallets
from corridor.platform.db import Database
from corridor.platform.ids import new_id


@pytest.fixture
async def user_id(db: Database) -> uuid.UUID:
    """A user with a wallet in every asset. Wallets know a user only by id."""
    user = new_id()
    async with db.transaction() as session:
        await wallets.provision(session, user)
    return user
