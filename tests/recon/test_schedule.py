"""The scheduled run: every five minutes, over the trailing hour or back to where the last
completed run ended, on one worker only."""

import asyncio
from datetime import datetime, timedelta

from prometheus_client import REGISTRY

from corridor import recon
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.providers import SimBank, SimCustody
from corridor.worker import Scheduler, build_jobs
from tests.payments.support import rows
from tests.recon.support import breaks, funded, let_pass
from tests.recon.test_schema import add_run
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


def test_how_often_reconciliation_runs_is_a_setting(
    settings: Settings, stack: Stack, bank: SimBank
) -> None:
    often = settings.model_copy(update={"reconciliation_interval_seconds": 2.0})
    by_name = {job.name: job for job in build_jobs(often, bank=bank, reconcile=True)}

    assert by_name[JOB].interval_seconds == 2.0


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
    await let_pass(stack, 180)

    ran = await scheduler(stack, bank, custody).tick()

    assert JOB in ran
    (run,) = await rows(stack.db, "SELECT * FROM recon_runs")
    assert run["window_end"] == stack.clock.now()
    assert run["window_end"] - run["window_start"] == timedelta(hours=1)
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


async def test_the_scheduled_run_leaves_a_deposit_alone_until_its_webhook_has_had_time(
    stack: Stack, bank: SimBank, custody: SimCustody
) -> None:
    user = await stack.person()
    await stack.webhooks_behave(drop_types=["deposit.received"])
    await stack.bank_deposit(user, "250.00")
    await let_pass(stack, 119)

    assert JOB in await scheduler(stack, bank, custody).tick()

    # Two minutes have not passed since the bank received it: nothing is said of it yet.
    assert (await stack.wallet(user), await breaks(stack)) == ((0, 0), [])

    await let_pass(stack, 300)
    assert JOB in await scheduler(stack, bank, custody).tick()

    assert await stack.wallet(user) == (250_00, 0)
    assert [row["resolved_by"] for row in await breaks(stack)] == ["system"]


async def test_how_long_a_deposit_is_left_alone_is_a_setting(
    stack: Stack, bank: SimBank, custody: SimCustody
) -> None:
    user = await stack.person()
    await stack.webhooks_behave(drop_types=["deposit.received"])
    await stack.bank_deposit(user, "250.00")
    await let_pass(stack, 10)
    impatient = stack.settings.model_copy(update={"reconciliation_grace_seconds": 5})

    ran = await Scheduler(
        stack.db, build_jobs(impatient, bank=bank, custody=custody, reconcile=True)
    ).tick()

    assert JOB in ran
    assert await stack.wallet(user) == (250_00, 0)


# --- after an outage -----------------------------------------------------------------------------


async def windows(stack: Stack) -> list[tuple[datetime, datetime]]:
    found = await rows(stack.db, "SELECT window_start, window_end FROM recon_runs ORDER BY id")
    return [(row["window_start"], row["window_end"]) for row in found]


async def test_a_run_after_an_outage_starts_where_the_last_completed_run_ended(
    stack: Stack, bank: SimBank, custody: SimCustody
) -> None:
    assert JOB in await scheduler(stack, bank, custody).tick()
    ((_, covered_until),) = await windows(stack)
    # No worker for five hours. The deposit arrives, unheard of, in the first of them: an
    # hour's window would end three hours short of it.
    user = await stack.person()
    await stack.webhooks_behave(drop_types=["deposit.received"])
    await let_pass(stack, 600)
    await stack.bank_deposit(user, "250.00")
    await let_pass(stack, 5 * 3600)

    assert JOB in await scheduler(stack, bank, custody).tick()

    _, (start, end) = await windows(stack)
    assert (start, end) == (covered_until, stack.clock.now())
    assert await stack.wallet(user) == (250_00, 0)


async def test_a_run_soon_after_the_last_one_still_covers_the_whole_trailing_hour(
    stack: Stack, bank: SimBank, custody: SimCustody
) -> None:
    assert JOB in await scheduler(stack, bank, custody).tick()
    await let_pass(stack, 300)

    assert JOB in await scheduler(stack, bank, custody).tick()

    _, (start, end) = await windows(stack)
    assert end - start == timedelta(hours=1)


async def test_catching_up_looks_only_at_completed_runs(
    db: Database,
) -> None:
    now = datetime.fromisoformat("2026-03-01T12:00:00+00:00")
    hour = timedelta(hours=1)

    async def start() -> datetime:
        async with db.transaction() as session:
            return await recon.catch_up_start(session, window_end=now, window=hour)

    # No run has ever completed: the trailing hour.
    assert await start() == now - hour
    # One that could not read a provider compared nothing in full, however recent it is.
    await add_run(db, window_start=now - 3 * hour, window_end=now - 2 * hour, status="incomplete")
    assert await start() == now - hour
    await add_run(db, window_start=now - 6 * hour, window_end=now - 5 * hour)
    assert await start() == now - 5 * hour
    # The latest completed run is what counts, not the latest one written.
    await add_run(db, window_start=now - 9 * hour, window_end=now - 8 * hour)
    assert await start() == now - 5 * hour


async def test_catching_up_reaches_back_a_week_at_most(db: Database) -> None:
    now = datetime.fromisoformat("2026-03-01T12:00:00+00:00")
    await add_run(db, window_start=now - timedelta(days=31), window_end=now - timedelta(days=30))

    async with db.transaction() as session:
        start = await recon.catch_up_start(session, window_end=now, window=timedelta(hours=1))

    assert start == now - recon.MAX_CATCH_UP == now - timedelta(days=7)


# --- the gauge ---------------------------------------------------------------------------------


async def forget(stack: Stack) -> None:
    """The providers lose their books: everything Corridor recorded is now unknown to them."""
    await stack.sim.control("POST", "/reset")


def open_breaks_gauge() -> float | None:
    return REGISTRY.get_sample_value("corridor_recon_open_breaks")


async def test_the_scheduled_run_reports_how_many_breaks_are_open(
    stack: Stack, bank: SimBank, custody: SimCustody
) -> None:
    # A clean run first, so that what the gauge says next is this test's and nobody else's.
    assert JOB in await scheduler(stack, bank, custody).tick()
    assert open_breaks_gauge() == 0
    # A deposit the bank no longer knows of: a break only a person can settle.
    await funded(stack)
    await forget(stack)
    await let_pass(stack, 300)

    assert JOB in await scheduler(stack, bank, custody).tick()

    still_open = [row for row in await breaks(stack) if row["status"] == "open"]
    assert len(still_open) >= 1
    assert open_breaks_gauge() == len(still_open)
    async with stack.db.transaction() as session:
        assert await recon.count_open_breaks(session) == len(still_open)
