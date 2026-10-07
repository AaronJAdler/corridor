"""The purge of idempotency keys: the worker's one statement against a table of the API's."""

from datetime import timedelta

from sqlalchemy import text

from corridor.platform.clock import ManualClock
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.ids import new_id
from corridor.worker import Scheduler, build_jobs
from corridor.worker.purge import purge_idempotency_keys

JOB = "idempotency.purge_expired"


async def add_key(db: Database, clock: ManualClock, key: str, *, completed: bool = True) -> None:
    """A key as the API leaves one: created now, and answered unless the request is running."""
    async with db.transaction() as session:
        await session.execute(
            text(
                "INSERT INTO idempotency_keys (actor_id, key, fingerprint, status_code,"
                " response_body, response_headers, created_at, completed_at)"
                " VALUES (:actor, :key, :fingerprint, :status, NULL, NULL, :now, :completed_at)"
            ),
            {
                "actor": new_id(),
                "key": key,
                "fingerprint": "0" * 64,
                "status": 201 if completed else None,
                "now": clock.now(),
                "completed_at": clock.now() if completed else None,
            },
        )


async def keys(db: Database) -> set[str]:
    async with db.transaction() as session:
        return set((await session.execute(text("SELECT key FROM idempotency_keys"))).scalars())


async def test_the_purge_deletes_keys_older_than_the_cutoff_and_only_those(
    db: Database, clock: ManualClock
) -> None:
    await add_key(db, clock, "old")
    await add_key(db, clock, "old-and-never-answered", completed=False)
    clock.advance(hours=25)
    await add_key(db, clock, "recent")

    async with db.transaction() as session:
        deleted = await purge_idempotency_keys(
            session, older_than=clock.now() - timedelta(hours=24)
        )

    assert deleted == 2
    assert await keys(db) == {"recent"}


async def test_a_key_created_exactly_at_the_cutoff_is_kept(
    db: Database, clock: ManualClock
) -> None:
    await add_key(db, clock, "on-the-line")

    async with db.transaction() as session:
        assert await purge_idempotency_keys(session, older_than=clock.now()) == 0

    assert await keys(db) == {"on-the-line"}


async def test_the_purge_job_deletes_keys_older_than_a_day(
    db: Database, settings: Settings, clock: ManualClock
) -> None:
    await add_key(db, clock, "a-day-and-a-second-old")
    clock.advance(seconds=1)
    await add_key(db, clock, "a-day-old")
    clock.advance(hours=24)

    assert JOB in await Scheduler(db, build_jobs(settings)).tick()

    assert await keys(db) == {"a-day-old"}
    async with db.transaction() as session:
        error = await session.execute(
            text("SELECT last_error FROM job_runs WHERE name = :name"), {"name": JOB}
        )
        assert error.scalar_one() is None


def test_the_purge_job_runs_hourly(settings: Settings) -> None:
    by_name = {job.name: job for job in build_jobs(settings)}

    assert by_name[JOB].interval_seconds == 3600
