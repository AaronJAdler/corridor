"""Fixtures for the failure-injection suite: the whole system, running in this process."""

from collections.abc import AsyncIterator

import pytest

from corridor.platform.clock import ManualClock
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.redis import RedisStore
from tests.support.stack import Stack, running_stack


@pytest.fixture
async def stack(
    settings: Settings, db: Database, redis: RedisStore, clock: ManualClock
) -> AsyncIterator[Stack]:
    """The API, the simulator, a dispatcher and the scheduled jobs, on this test's database.

    It is stopped before the ``db`` fixture runs the ledger verifier, so every test here
    ends with the verifier looking at what the failures left behind. ``redis`` is asked for
    only so that what the API wrote under this test's key prefix is deleted afterwards.
    """
    async with running_stack(settings, db, clock) as running:
        yield running
