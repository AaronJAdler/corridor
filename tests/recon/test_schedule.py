"""The scheduled run: every five minutes, over the trailing hour, on one worker only."""

import asyncio

from corridor.platform.config import Settings
from corridor.providers import SimBank, SimCustody
from corridor.worker import Scheduler, build_jobs
from corridor.worker import jobs as worker_jobs
from tests.payments.support import rows
from tests.recon.support import breaks, let_pass
from tests.support.stack import Stack

JOB = "recon.run"


def scheduler(stack: Stack, bank: SimBank, custody: SimCustody) -> Scheduler:
    return Scheduler(
        stack.db, build_jobs(stack.settings, bank=bank, custody=custody, reconcile=True)
    )


def test_reconciliation_is_scheduled_every_five_minutes_when_it_is_asked_for(
    settings: Settings, stack: Stack, bank: SimBank
) -> None:
    by_name = {job.name: job for job in build_jobs(settings, bank=bank, reconcile=True)}

    assert by_name[JOB].interval_seconds == 300


def test_reconciliation_is_not_scheduled_unless_it_is_asked_for(
    settings: Settings, stack: Stack, bank: SimBank
) -> None:
    assert JOB not in {job.name for job in build_jobs(settings, bank=bank)}


def test_a_worker_with_no_provider_has_nothing_to_reconcile(settings: Settings) -> None:
    assert JOB not in {job.name for job in build_jobs(settings, reconcile=True)}


async def test_the_scheduled_run_covers_the_trailing_hour_and_repairs(
    stack: Stack, bank: SimBank, custody: SimCustody
) -> None:
    user = await stack.person()
    await stack.webhooks_behave(drop_types=["deposit.received"])
    await stack.bank_deposit(user, "250.00")
    await let_pass(stack)

    ran = await scheduler(stack, bank, custody).tick()

    assert JOB in ran
    (run,) = await rows(stack.db, "SELECT * FROM recon_runs")
    assert run["window_end"] == stack.clock.now()
    assert run["window_end"] - run["window_start"] == worker_jobs.RECONCILIATION_WINDOW
    assert await stack.wallet(user) == (250_00, 0)
    assert [row["resolved_by"] for row in await breaks(stack)] == ["system"]
    (recorded,) = await rows(stack.db, "SELECT * FROM job_runs WHERE name = :name", name=JOB)
    assert recorded["last_error"] is None


async def test_two_schedulers_run_a_due_reconciliation_once(
    stack: Stack, bank: SimBank, custody: SimCustody
) -> None:
    first, second = scheduler(stack, bank, custody), scheduler(stack, bank, custody)

    ran = await asyncio.gather(first.tick(), second.tick())

    assert sorted(JOB in names for names in ran) == [False, True]
    assert len(await rows(stack.db, "SELECT 1 FROM recon_runs")) == 1


async def test_the_scheduled_run_is_due_again_after_five_minutes_and_not_before(
    stack: Stack, bank: SimBank, custody: SimCustody
) -> None:
    one, other = scheduler(stack, bank, custody), scheduler(stack, bank, custody)
    assert JOB in await one.tick()

    stack.clock.advance(seconds=299)
    early = await asyncio.gather(one.tick(), other.tick())
    stack.clock.advance(seconds=1)
    due = await asyncio.gather(one.tick(), other.tick())

    assert [JOB in names for names in early] == [False, False]
    assert sorted(JOB in names for names in due) == [False, True]
    assert len(await rows(stack.db, "SELECT 1 FROM recon_runs")) == 2
