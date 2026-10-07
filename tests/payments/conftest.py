"""Fixtures for payment tests."""

import pytest

from corridor.identity import User
from corridor.platform.config import Settings
from corridor.platform.db import Database
from tests.payments.support import add_person


@pytest.fixture
async def maria(db: Database) -> User:
    async with db.transaction() as session:
        return await add_person(session, "maria")


@pytest.fixture
async def joao(db: Database) -> User:
    async with db.transaction() as session:
        return await add_person(session, "joao")


@pytest.fixture
def with_fee(settings: Settings) -> Settings:
    """The test settings with a 1% transfer fee."""
    return settings.model_copy(update={"transfer_fee_bps": 100})
