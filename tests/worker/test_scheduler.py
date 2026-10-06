"""The scheduler: a job runs on one worker at a time, and once per interval across all.

Time is the test's to move: the scheduler reads the application clock, so an interval is
waited out by advancing the ``clock`` fixture, never by sleeping.
"""

import asyncio
from typing import Any

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from corridor.platform.clock import ManualClock
from corridor.platform.config import Settings
from corridor.platform.db import Database, lock_key
from corridor.platform.logging import REDACTED
from corridor.worker import Job, Scheduler, build_jobs
from corridor.worker import scheduler as scheduler_module
from tests.outbox.helpers import (
    TOPIC,
    Failing,
    LogReader,
    Recorder,
    dispatcher_for,
    enqueue_event,
    reap,
    status_counts,
    until,
)
from tests.support import postgres

# Shaped like a credential so the scrubber has something to find. It protects nothing.
NOT_A_TOKEN = "Bearer abcdefghijklmnop"  # pragma: allowlist secret

JOB = "test.job"
INTERVAL = 60.0


class Counting:
    """A job that succeeds and counts its runs."""

    def __init__(self) -> None:
        self.runs = 0

    async def __call__(self, _db: Database) -> None:
        self.runs += 1


class Breaking:
    """A job that fails every time, and counts how often it was asked."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error if error is not None else RuntimeError("the job broke")
        self.runs = 0

    async def __call__(self, _db: Database) -> None:
        self.runs += 1
        raise self.error


class Held:
    """A job that stops part-way until the test lets it go on."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.runs = 0
        self._entered = asyncio.Event()
        self._released = asyncio.Event()

    async def __call__(self, _db: Database) -> None:
        self.runs += 1
        self._entered.set()
        await self._released.wait()
        if self.error is not None:
            raise self.error

    async def entered(self) -> None:
        async with asyncio.timeout(5):
            await self._entered.wait()

    def release(self) -> None:
        self._released.set()


def scheduler_for(db: Database, run: Any, *, name: str = JOB) -> Scheduler:
    return Scheduler(db, [Job(name, INTERVAL, run)])


async def job_run(db: Database, name: str = JOB) -> Any:
    async with db.transaction() as session:
        rows = await session.execute(
            text(
                "SELECT last_started_at, last_finished_at, last_error FROM job_runs"
                " WHERE name = :name"
            ),
            {"name": name},
        )
        return rows.one_or_none()


async def locks_held(db: Database, name: str = JOB) -> int:
    """How many sessions hold the job's advisory lock right now."""
    key = lock_key("job", name)
    async with db.transaction() as session:
        held = await session.execute(
            text(
                # A 64-bit advisory key is shown as its two halves.
                "SELECT count(*) FROM pg_locks"
                " WHERE locktype = 'advisory' AND granted AND objsubid = 1"
                " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
                " AND ((classid::bigint << 32) | objid::bigint) = :key"
            ),
            {"key": key},
        )
        return int(held.scalar_one())


def job_runs_counted(job: str, outcome: str) -> float:
    return (
        REGISTRY.get_sample_value(
            "corridor_scheduled_job_runs_total", {"job": job, "outcome": outcome}
        )
        or 0.0
    )


# --- once per interval -----------------------------------------------------------------------


async def test_a_job_that_has_never_run_is_due(db: Database, clock: ManualClock) -> None:
    job = Counting()

    assert await scheduler_for(db, job).tick() == [JOB]

    assert job.runs == 1
    assert tuple(await job_run(db)) == (clock.now(), clock.now(), None)


async def test_a_job_does_not_run_again_within_its_interval(
    db: Database, clock: ManualClock
) -> None:
    job = Counting()
    scheduler = scheduler_for(db, job)
    assert await scheduler.tick() == [JOB]

    assert await scheduler.tick() == []
    clock.advance(seconds=INTERVAL - 0.001)
    assert await scheduler.tick() == []
    assert job.runs == 1

    clock.advance(seconds=0.001)
    assert await scheduler.tick() == [JOB]
    assert job.runs == 2


async def test_another_worker_does_not_repeat_a_job_that_has_run_this_interval(
    db: Database, clock: ManualClock
) -> None:
    job = Counting()
    assert await scheduler_for(db, job).tick() == [JOB]

    clock.advance(seconds=INTERVAL / 2)
    assert await scheduler_for(db, job).tick() == []
    assert job.runs == 1


async def test_the_interval_is_counted_from_when_a_run_started(
    db: Database, clock: ManualClock
) -> None:
    job = Held()
    scheduler = scheduler_for(db, job)
    started_at = clock.now()
    running = asyncio.create_task(scheduler.tick())
    try:
        await job.entered()
        in_progress = await job_run(db)
        clock.advance(seconds=INTERVAL - 1)
        job.release()
        assert await running == [JOB]
    finally:
        await reap(running)

    # While it ran, the row said so.
    assert tuple(in_progress) == (started_at, None, None)
    assert tuple(await job_run(db)) == (started_at, clock.now(), None)

    clock.advance(seconds=1)
    assert await scheduler.tick() == [JOB]


async def test_each_job_keeps_its_own_time(db: Database, clock: ManualClock) -> None:
    often, seldom = Counting(), Counting()
    scheduler = Scheduler(db, [Job("test.often", 10, often), Job("test.seldom", 100, seldom)])

    assert await scheduler.tick() == ["test.often", "test.seldom"]
    clock.advance(seconds=10)
    assert await scheduler.tick() == ["test.often"]
    clock.advance(seconds=90)
    assert await scheduler.tick() == ["test.often", "test.seldom"]

    assert (often.runs, seldom.runs) == (3, 2)


def test_two_jobs_cannot_share_a_name(db: Database) -> None:
    # They would share a lock and a row, and each would keep the other from running.
    with pytest.raises(ValueError, match="share a name"):
        Scheduler(db, [Job(JOB, 10, Counting()), Job(JOB, 20, Counting())])


# --- one at a time ---------------------------------------------------------------------------


async def test_two_schedulers_ticking_at_once_run_a_due_job_once(
    db: Database, clock: ManualClock
) -> None:
    job = Counting()
    first, second = scheduler_for(db, job), scheduler_for(db, job)

    for round_number in range(1, 21):
        ran = await asyncio.gather(first.tick(), second.tick())

        assert sorted(ran) == [[], [JOB]]
        assert job.runs == round_number
        clock.advance(seconds=INTERVAL)


async def test_a_job_still_running_is_not_started_again_even_after_its_interval(
    db: Database, clock: ManualClock
) -> None:
    job = Held()
    first, second = scheduler_for(db, job), scheduler_for(db, job)
    running = asyncio.create_task(first.tick())
    try:
        await job.entered()

        # By the row alone the job is due again. Only the lock says it is still running.
        clock.advance(seconds=INTERVAL + 1)
        assert await second.tick() == []
        assert job.runs == 1

        job.release()
        assert await running == [JOB]
    finally:
        await reap(running)

    # The first run is over and an interval has passed since it started.
    assert await second.tick() == [JOB]
    assert job.runs == 2


async def test_the_lock_is_held_while_the_job_runs_and_released_when_it_succeeds(
    db: Database,
) -> None:
    job = Held()
    running = asyncio.create_task(scheduler_for(db, job).tick())
    try:
        await job.entered()
        assert await locks_held(db) == 1
        job.release()
        await running
    finally:
        await reap(running)

    assert await locks_held(db) == 0


async def test_the_lock_is_released_when_the_job_raises(db: Database) -> None:
    await scheduler_for(db, Breaking()).tick()

    assert await locks_held(db) == 0


async def test_the_lock_is_released_when_the_job_is_cancelled(
    db: Database, clock: ManualClock
) -> None:
    job = Held()
    running = asyncio.create_task(scheduler_for(db, job).tick())
    try:
        await job.entered()
        assert await locks_held(db) == 1
    finally:
        await reap(running)

    assert await locks_held(db) == 0
    # Like a worker that died: the run never finished, and the job is due after its interval.
    assert tuple(await job_run(db)) == (clock.now(), None, None)
    again = Counting()
    assert await scheduler_for(db, again).tick() == []
    clock.advance(seconds=INTERVAL)
    assert await scheduler_for(db, again).tick() == [JOB]


async def test_the_lock_is_released_when_taking_it_is_interrupted(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    taking = scheduler_module._try_lock

    async def taken_then_cancelled(connection: AsyncConnection, key: int) -> bool:
        # The worker is cancelled as the answer comes back: the session has the lock and
        # the scheduler never learns it.
        assert await taking(connection, key) is True
        raise asyncio.CancelledError

    monkeypatch.setattr(scheduler_module, "_try_lock", taken_then_cancelled)
    job = Counting()

    with pytest.raises(asyncio.CancelledError):
        await scheduler_for(db, job).tick()

    assert job.runs == 0
    # The session is ended, not handed back to the pool, and its lock goes with it.
    await until(lambda: _free(db), what="the release of the lock")


async def test_a_session_whose_lock_cannot_be_released_is_ended_and_not_reused(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    execute = AsyncConnection.execute

    async def failing_to_unlock(self: AsyncConnection, statement: Any, *args: Any) -> Any:
        if "pg_advisory_unlock" in str(statement):
            raise ConnectionError("the unlock did not get through")
        return await execute(self, statement, *args)

    monkeypatch.setattr(AsyncConnection, "execute", failing_to_unlock)
    job = Counting()

    # The failure is the scheduler's own and does not escape the tick.
    await scheduler_for(db, job).tick()

    assert job.runs == 1
    # Back in the pool, the session would keep every worker off the job.
    await until(lambda: _free(db), what="the release of the lock")


async def _free(db: Database) -> bool:
    return await locks_held(db) == 0


async def test_the_lock_is_released_when_the_job_is_not_due(db: Database) -> None:
    scheduler = scheduler_for(db, Counting())
    await scheduler.tick()

    assert await scheduler.tick() == []

    assert await locks_held(db) == 0


async def test_no_transaction_is_open_on_the_lock_connection_while_the_job_runs(
    db: Database,
) -> None:
    job = Held()
    running = asyncio.create_task(scheduler_for(db, job).tick())
    try:
        await job.entered()
        async with db.transaction() as session:
            states = (
                await session.execute(
                    text(
                        "SELECT state FROM pg_stat_activity"
                        " WHERE datname = current_database() AND pid <> pg_backend_pid()"
                    )
                )
            ).scalars()
            # A job may take minutes, and the server ends a session that idles in a
            # transaction after seconds.
            assert set(states) == {"idle"}
        job.release()
        await running
    finally:
        await reap(running)


# --- failure ---------------------------------------------------------------------------------


async def test_a_failing_job_is_recorded_with_its_error_and_runs_again_at_the_next_interval(
    db: Database, clock: ManualClock
) -> None:
    job = Breaking(RuntimeError("the disk is full"))
    scheduler = scheduler_for(db, job)

    assert await scheduler.tick() == [JOB]
    assert tuple(await job_run(db)) == (clock.now(), clock.now(), "RuntimeError: the disk is full")

    # A failure is not retried early: the job waits for its interval like any other run.
    assert await scheduler.tick() == []
    clock.advance(seconds=INTERVAL)
    assert await scheduler.tick() == [JOB]
    assert job.runs == 2


async def test_a_run_that_succeeds_clears_the_error_of_the_one_before(
    db: Database, clock: ManualClock
) -> None:
    await scheduler_for(db, Breaking()).tick()
    clock.advance(seconds=INTERVAL)

    await scheduler_for(db, Counting()).tick()

    assert tuple(await job_run(db)) == (clock.now(), clock.now(), None)


async def test_a_failing_job_does_not_stop_the_other_jobs_in_the_same_tick(db: Database) -> None:
    after = Counting()
    scheduler = Scheduler(db, [Job("test.broken", INTERVAL, Breaking()), Job(JOB, INTERVAL, after)])

    assert await scheduler.tick() == ["test.broken", JOB]

    assert after.runs == 1
    assert (await job_run(db, "test.broken")).last_error == "RuntimeError: the job broke"
    assert (await job_run(db)).last_error is None


async def test_a_recorded_error_has_its_secrets_removed(db: Database) -> None:
    await scheduler_for(db, Breaking(RuntimeError(f"refused: {NOT_A_TOKEN}"))).tick()

    assert (await job_run(db)).last_error == f"RuntimeError: refused: {REDACTED}"


async def test_a_recorded_error_is_cut_to_500_characters(db: Database) -> None:
    await scheduler_for(db, Breaking(RuntimeError("x" * 2_000))).tick()

    assert (await job_run(db)).last_error == ("RuntimeError: " + "x" * 2_000)[:500]


async def test_each_run_is_counted_by_job_and_outcome(db: Database) -> None:
    before = (job_runs_counted("test.fine", "ok"), job_runs_counted("test.broken", "error"))
    scheduler = Scheduler(
        db, [Job("test.fine", INTERVAL, Counting()), Job("test.broken", INTERVAL, Breaking())]
    )

    await scheduler.tick()
    # Not due: nothing ran, so nothing is counted.
    await scheduler.tick()

    assert job_runs_counted("test.fine", "ok") == before[0] + 1
    assert job_runs_counted("test.broken", "error") == before[1] + 1


async def test_each_run_is_logged_by_job(db: Database, logs: LogReader) -> None:
    scheduler = Scheduler(
        db, [Job("test.fine", INTERVAL, Counting()), Job("test.broken", INTERVAL, Breaking())]
    )

    await scheduler.tick()

    written = {line["event"]: line for line in logs() if line["event"].startswith("scheduler.")}
    assert set(written) == {"scheduler.job_ok", "scheduler.job_failed"}
    assert written["scheduler.job_ok"]["job"] == "test.fine"
    assert written["scheduler.job_failed"]["job"] == "test.broken"
    assert written["scheduler.job_failed"]["error"] == "RuntimeError: the job broke"


# --- running until stopped -------------------------------------------------------------------


async def test_run_ticks_until_it_is_stopped(db: Database) -> None:
    job = Held()
    stop = asyncio.Event()
    running = asyncio.create_task(scheduler_for(db, job).run(stop))
    try:
        await job.entered()
        stop.set()
        job.release()
        async with asyncio.timeout(5):
            await running
    finally:
        await reap(running)

    # The run that was in progress when the stop came was allowed to finish.
    assert (await job_run(db)).last_finished_at is not None
    assert job.runs == 1


async def test_run_goes_on_ticking_after_a_job_fails(db: Database, clock: ManualClock) -> None:
    job = Breaking()
    stop = asyncio.Event()
    running = asyncio.create_task(scheduler_for(db, job).run(stop))
    try:
        async with asyncio.timeout(10):
            while job.runs < 2:
                # Each tick finds the job due again, a second of real time apart.
                clock.advance(seconds=INTERVAL)
                await asyncio.sleep(0.05)
    finally:
        stop.set()
        async with asyncio.timeout(5):
            await running

    assert job.runs >= 2


# --- the jobs that exist ---------------------------------------------------------------------


async def test_the_purge_job_deletes_finished_events_older_than_the_retention(
    db: Database, settings: Settings, clock: ManualClock
) -> None:
    recorder = Recorder()
    old_done = await enqueue_event(db)
    await dispatcher_for(db, settings, {TOPIC: recorder}).run_once()
    old_dead = await enqueue_event(db, "test.unhandled")
    await dispatcher_for(db, settings, {}).run_once()

    clock.advance(hours=24 * settings.outbox_retention_days, seconds=1)
    recent_done = await enqueue_event(db)
    await dispatcher_for(db, settings, {TOPIC: recorder}).run_once()
    failed = await enqueue_event(db)
    await dispatcher_for(db, settings, {TOPIC: Failing()}).run_once()
    assert await status_counts(db) == {"done": 2, "dead": 1, "pending": 1}

    scheduler = Scheduler(db, build_jobs(settings))
    assert await scheduler.tick() == ["outbox.purge_finished"]

    async with db.transaction() as session:
        left = set((await session.execute(text("SELECT id FROM outbox_events"))).scalars())
    assert left == {old_dead, recent_done, failed}
    assert old_done not in left
    assert (await job_run(db, "outbox.purge_finished")).last_error is None


async def test_the_purge_job_runs_hourly(db: Database, settings: Settings) -> None:
    (job,) = build_jobs(settings)

    assert (job.name, job.interval_seconds) == ("outbox.purge_finished", 3600)


# --- the table -------------------------------------------------------------------------------


async def test_no_column_of_job_runs_has_a_default(db: Database) -> None:
    async with db.transaction() as session:
        rows = await session.execute(
            text(
                "SELECT column_name, column_default FROM information_schema.columns"
                " WHERE table_schema = 'public' AND table_name = 'job_runs'"
            )
        )
        defaults = {row.column_name: row.column_default for row in rows}

    assert defaults == dict.fromkeys(["name", "last_started_at", "last_finished_at", "last_error"])


async def test_the_application_role_has_full_row_access_to_job_runs(db: Database) -> None:
    async with db.transaction() as session:
        granted = (
            await session.execute(
                text(
                    "SELECT string_agg(privilege_type, ',' ORDER BY privilege_type)"
                    " FROM information_schema.role_table_grants"
                    " WHERE grantee = :role AND table_schema = 'public'"
                    " AND table_name = 'job_runs'"
                ),
                {"role": postgres.APP_ROLE},
            )
        ).scalar_one()

    assert granted == "DELETE,INSERT,SELECT,UPDATE"
