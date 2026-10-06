"""Fixtures for identity tests."""

import pytest
from pydantic import SecretStr

from corridor.identity import KeySet, PasswordHasher, User, generate_private_key_pem, load_keyset
from corridor.platform.config import Settings
from corridor.platform.db import Database
from tests.identity.support import add_user


@pytest.fixture
def hasher(settings: Settings) -> PasswordHasher:
    """A hasher with the cheap parameters of the test settings."""
    return PasswordHasher(settings)


@pytest.fixture
def auth_settings(settings: Settings) -> Settings:
    """The test settings plus a signing key, generated for this test and never written down."""
    return settings.model_copy(update={"jwt_signing_key": SecretStr(generate_private_key_pem())})


@pytest.fixture
def keys(auth_settings: Settings) -> KeySet:
    return load_keyset(auth_settings)


@pytest.fixture
async def maria(db: Database) -> User:
    async with db.transaction() as session:
        return await add_user(session, "maria")
