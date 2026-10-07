"""Fixtures for the agent tests."""

import httpx
import pytest
from pydantic import SecretStr

from corridor.platform.config import Settings
from tests.agents.support import HASH_KEY
from tests.support.auth import RegisteredUser, register_user


@pytest.fixture
def settings(settings: Settings) -> Settings:
    """The test settings with agent keys configured, which the shared ones leave out."""
    return settings.model_copy(update={"api_key_hash_key": SecretStr(HASH_KEY)})


@pytest.fixture
async def maria(client: httpx.AsyncClient) -> RegisteredUser:
    return await register_user(client, handle="maria")


@pytest.fixture
async def joao(client: httpx.AsyncClient) -> RegisteredUser:
    return await register_user(client, handle="joao")
