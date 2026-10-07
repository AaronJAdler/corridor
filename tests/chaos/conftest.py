"""Fixtures for the failure-injection suite: the whole system, running in this process."""

from collections.abc import AsyncIterator

import pytest

from corridor.platform.clock import ManualClock
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.redis import RedisStore
from corridor.providers import SimBank, SimCustody
from corridor.worker import Job, Scheduler, build_jobs
from corridor.worker.jobs import RECONCILIATION_JOB
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


class _AndReconciliation(Scheduler):
    """The stack's own scheduler, and after it the reconciliation job, as a deployed
    worker runs it beside the others."""

    def __init__(self, first: Scheduler, db: Database, reconciliation: Job) -> None:
        super().__init__(db, [reconciliation])
        self._first = first

    async def tick(self) -> list[str]:
        return await self._first.tick() + await super().tick()


@pytest.fixture
def reconciling(stack: Stack) -> Stack:
    """The same system with a worker that also reconciles, as ``corridor worker`` does.

    The job is the worker's own, built as the worker builds it, and it runs when the
    scheduler finds it due as time is turned. Its lines to the providers are its own, so a
    fault a test queues for another job is not met by this one first.
    """
    (reconciliation,) = (
        job
        for job in build_jobs(
            stack.settings,
            bank=SimBank(stack.settings, client=stack.sim.http),
            custody=SimCustody(stack.settings, client=stack.sim.http),
            reconcile=True,
        )
        if job.name == RECONCILIATION_JOB
    )
    stack.scheduler = _AndReconciliation(stack.scheduler, stack.db, reconciliation)
    return stack
