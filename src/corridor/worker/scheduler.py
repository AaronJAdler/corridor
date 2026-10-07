"""The scheduler: periodic jobs that run on one worker at a time, once per interval.

Every worker runs a scheduler, and every scheduler knows every job. Two mechanisms keep a
job from running twice, and each covers what the other cannot. A session-level advisory
lock, held for the whole run, means no two workers run the job at the same moment. The
job's row in ``job_runs`` means that a worker which comes to the job after another has
finished it finds that it is not due.
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, cast

from sqlalchemy import Table, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from corridor.platform.clock import utcnow
from corridor.platform.db import Database, lock_key
from corridor.platform.logging import get_logger, scrub
from corridor.platform.metrics import SCHEDULED_JOB_RUNS
from corridor.worker.models import JobRun

log = get_logger(__name__)

_job_runs = cast(Table, JobRun.__table__)

MAX_ERROR_LENGTH: Final = 500
# What is stored when the description of a failure is itself something PostgreSQL refuses.
UNRECORDABLE: Final = "the failure could not be recorded"
TICK_SECONDS: Final = 1.0


@dataclass(frozen=True, slots=True)
class Job:
    """Something to do every so often.

    ``run`` is an entry point: it opens its own transactions. A worker can die in the
    middle of it and the job then runs again, so it must be safe to repeat.
    """

    # Also the advisory lock's key and the key of the job's row in ``job_runs``.
    name: str
    interval_seconds: float
    run: Callable[[Database], Awaitable[None]]


class Scheduler:
    """Runs each job when it is due, unless another worker is running it or already has."""

    def __init__(self, db: Database, jobs: Sequence[Job]) -> None:
        names = [job.name for job in jobs]
        if len(set(names)) != len(names):
            raise ValueError(f"two scheduled jobs share a name: {sorted(names)}")
        self._db = db
        self._jobs = tuple(jobs)

    async def tick(self) -> list[str]:
        """Run every job that is due, one after another. Returns the names that ran.

        A job that fails is recorded and logged, and the next job still gets its turn.
        """
        ran: list[str] = []
        for job in self._jobs:
            try:
                if await self._run_if_due(job):
                    ran.append(job.name)
            except Exception:
                # Not the job failing, which is recorded on its row, but the bookkeeping
                # around it: the database went away, say. The other jobs are still tried.
                log.exception("scheduler.job_unrecorded", job=job.name)
        return ran

    async def run(self, stop: asyncio.Event) -> None:
        """Tick about once a second until ``stop`` is set. A job in progress is finished."""
        while not stop.is_set():
            await self.tick()
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(TICK_SECONDS):
                    await stop.wait()

    async def _run_if_due(self, job: Job) -> bool:
        key = lock_key("job", job.name)
        # A connection of the lock's own, in autocommit mode: the lock belongs to the
        # session, so it is held across the whole run with no transaction open, and if this
        # worker dies PostgreSQL releases it along with the connection. A crashed job can
        # therefore never block the next run.
        async with self._db.engine.connect() as pooled:
            connection = await pooled.execution_options(isolation_level="AUTOCOMMIT")
            try:
                holding = await _try_lock(connection, key)
            except BaseException:
                # Interrupted while asking, so the session may hold the lock without this
                # worker knowing. It is ended and not reused: the lock goes with it.
                await connection.invalidate()
                raise
            if not holding:
                # Another worker is running this job right now.
                return False
            try:
                return await self._run_holding_the_lock(connection, job)
            finally:
                await _unlock(connection, key)

    async def _run_holding_the_lock(self, connection: AsyncConnection, job: Job) -> bool:
        if not await _start_if_due(connection, job, now=utcnow()):
            return False

        error: str | None = None
        try:
            await job.run(self._db)
        except Exception as raised:
            # Cancellation is deliberately not caught: it means the worker is going away.
            # The row keeps saying the run never finished, and the job is due again after
            # its interval.
            error = _describe(raised)
            log.error("scheduler.job_failed", job=job.name, error=error, exc_info=raised)
        else:
            log.info("scheduler.job_ok", job=job.name)

        try:
            await _finish(connection, job, error)
        except Exception:
            if error is None:
                raise
            # The description is what could not be stored. The run is still recorded as
            # failed, with words that are certain to be storable.
            log.exception("scheduler.failure_unrecordable", job=job.name)
            await _finish(connection, job, UNRECORDABLE)
        SCHEDULED_JOB_RUNS.labels(job=job.name, outcome="ok" if error is None else "error").inc()
        return True


async def _finish(connection: AsyncConnection, job: Job, error: str | None) -> None:
    await connection.execute(
        update(_job_runs)
        .where(_job_runs.c.name == job.name)
        .values(last_finished_at=utcnow(), last_error=error)
    )


async def _try_lock(connection: AsyncConnection, key: int) -> bool:
    locked = await connection.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
    return bool(locked.scalar_one())


async def _unlock(connection: AsyncConnection, key: int) -> None:
    """Give the lock up before the connection goes back to the pool.

    A pooled connection that still held the lock would keep every worker off the job for
    as long as the pool kept the connection, so if the lock cannot be released the
    connection is thrown away instead: PostgreSQL releases the lock when the session ends.
    """
    try:
        await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
    except BaseException:
        await connection.invalidate()
        raise


async def _start_if_due(connection: AsyncConnection, job: Job, *, now: datetime) -> bool:
    """Record that a run starts now, unless the last one started less than an interval ago.

    Called only while holding the job's lock, so nothing else writes the row in between
    the read and the write.
    """
    last_started_at = (
        await connection.execute(
            select(_job_runs.c.last_started_at).where(_job_runs.c.name == job.name)
        )
    ).scalar_one_or_none()
    not_due = (
        last_started_at is not None
        and last_started_at + timedelta(seconds=job.interval_seconds) > now
    )
    if not_due:
        return False

    starting = {"last_started_at": now, "last_finished_at": None, "last_error": None}
    await connection.execute(
        pg_insert(_job_runs)
        .values(name=job.name, **starting)
        .on_conflict_do_update(index_elements=[_job_runs.c.name], set_=starting)
    )
    return True


def _describe(error: Exception) -> str:
    """An error as it is stored on its job: type and message, without secrets, cut to fit."""
    try:
        message = str(error)
    except Exception:
        message = "(no printable message)"
    described = f"{type(error).__name__}: {message}" if message else type(error).__name__
    # Scrubbed before it is cut: a secret cut in half no longer looks like one.
    # PostgreSQL refuses a NUL in text, so one in a message is dropped.
    return str(scrub(described)).replace("\x00", "")[:MAX_ERROR_LENGTH]
