"""Fixtures for risk tests."""

import pytest

from corridor.identity import User
from corridor.platform.db import Database
from tests.payments.support import add_person

# The simulated providers and the adapters wired to them, for the tests that take money in
# or out. Imported so that pytest finds them as fixtures of this directory.
from tests.support.providers import bank, custody, provider_settings, sim  # noqa: F401


@pytest.fixture
async def maria(db: Database) -> User:
    async with db.transaction() as session:
        return await add_person(session, "maria")


@pytest.fixture
async def joao(db: Database) -> User:
    async with db.transaction() as session:
        return await add_person(session, "joao")
