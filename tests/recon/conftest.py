"""Fixtures for the reconciliation tests: the whole system in this process, and a way to
run a reconciliation against its providers."""

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta

import pytest

from corridor import recon
from corridor.platform.clock import ManualClock
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.redis import RedisStore
from corridor.providers import SimBank, SimCustody
from corridor.recon import RunResult
from tests.support.stack import Stack, running_stack

Reconcile = Callable[[], Awaitable[RunResult]]

WINDOW = timedelta(hours=1)


@pytest.fixture
async def stack(
    settings: Settings, db: Database, redis: RedisStore, clock: ManualClock
) -> AsyncIterator[Stack]:
    """The API, the simulator, a dispatcher and the scheduled jobs, on this test's database.

    Its scheduler does not reconcile: a test here runs a reconciliation when it means to.
    ``redis`` is asked for only so that what the API wrote under this test's key prefix is
    deleted afterwards.
    """
    async with running_stack(settings, db, clock) as running:
        yield running


@pytest.fixture
def bank(stack: Stack) -> SimBank:
    return SimBank(stack.settings, client=stack.sim.http)


@pytest.fixture
def custody(stack: Stack) -> SimCustody:
    return SimCustody(stack.settings, client=stack.sim.http)


@pytest.fixture
def reconcile(stack: Stack, bank: SimBank, custody: SimCustody) -> Reconcile:
    """Run a reconciliation over the hour that ends now, against both providers."""

    async def run() -> RunResult:
        return await recon.run(
            stack.db,
            bank,
            custody,
            window_start=stack.clock.now() - WINDOW,
            window_end=stack.clock.now(),
        )

    return run
