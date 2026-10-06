"""The worker: the outbox loop in process, and the whole thing as a real process.

These tests wait for real asynchronous things (a notification, a poll, a process), always
by polling for the outcome with a time limit and never by sleeping for a fixed time.
"""

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
from collections.abc import AsyncIterator, Mapping
from datetime import timedelta
from typing import Any

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import text

from corridor.outbox import Handler, Registry
from corridor.platform.clock import ManualClock
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.metrics import OUTBOX_DEAD
from corridor.worker import Job, Worker, build_registry
from tests.outbox.helpers import (
    TOPIC,
    Gate,
    LogReader,
    Recorder,
    enqueue_event,
    enqueue_events,
    load,
    reap,
    status_counts,
    until,
)
from tests.support import postgres

# Longer than any test waits: with this poll, only a notification can wake the worker.
NEVER = 30.0
# A value no drain sets, to tell a gauge that has been set from one that has not.
UNSET = -1.0


def worker_for(
    db: Database,
    settings: Settings,
    handlers: Mapping[str, Handler],
    *,
    jobs: tuple[Job, ...] = (),
    **overrides: Any,
) -> Worker:
    registry = Registry()
    for topic, handler in handlers.items():
        registry.register(topic, handler)
    return Worker(db, registry, settings.model_copy(update=overrides), jobs=jobs)


@contextlib.asynccontextmanager
async def running(worker: Worker) -> AsyncIterator[asyncio.Task[None]]:
    """Run a worker for the length of the block and make sure it is over afterwards."""
    task = asyncio.create_task(worker.run())
    try:
        yield task
        worker.request_stop()
        async with asyncio.timeout(10):
            await task
    finally:
        await reap(task)


async def listeners(db: Database) -> list[int]:
    """The backends of this test's database that are listening for the outbox."""
    async with db.transaction() as session:
        pids = await session.execute(
            text(
                "SELECT pid FROM pg_stat_activity"
                " WHERE datname = current_database() AND query LIKE 'LISTEN %corridor_outbox%'"
            )
        )
        return list(pids.scalars())


async def waiting() -> None:
    """Wait until the worker has finished a drain and is waiting for something to do.

    The gauges are the last thing a drain sets, and nothing else happens between that and
    the wait.
    """
    await until(lambda: gauge("corridor_outbox_dead") != UNSET, what="the first drain")


def gauge(name: str) -> float | None:
    return REGISTRY.get_sample_value(name)


# --- the loop --------------------------------------------------------------------------------


async def test_a_worker_drains_the_events_enqueued_before_it_started(
    db: Database, settings: Settings
) -> None:
    ids = await enqueue_events(db, 45)
    recorder = Recorder()

    # More than two batches, and no notification will come: this is the first drain.
    async with running(worker_for(db, settings, {TOPIC: recorder}, outbox_poll_seconds=NEVER)):
        await until(lambda: len(recorder.events) == 45, what="the drain")

    assert sorted(recorder.ids) == ids
    assert await status_counts(db) == {"done": 45}


async def test_a_waiting_worker_wakes_on_notify(db: Database, settings: Settings) -> None:
    recorder = Recorder()
    OUTBOX_DEAD.set(UNSET)

    async with running(worker_for(db, settings, {TOPIC: recorder}, outbox_poll_seconds=NEVER)):
        await waiting()
        assert len(await listeners(db)) == 1

        event_id = await enqueue_event(db)

        # The poll is half a minute away.
        await until(lambda: recorder.ids == [event_id], within=2, what="the wake-up")

    assert (await load(db, event_id)).status == "done"


async def test_a_worker_drains_by_polling_when_no_notification_is_delivered(
    db: Database, owner_db: Database, settings: Settings
) -> None:
    async with owner_db.transaction() as session:
        await session.execute(
            text("ALTER TABLE outbox_events DISABLE TRIGGER outbox_events_notify")
        )
    recorder = Recorder()
    OUTBOX_DEAD.set(UNSET)

    async with running(worker_for(db, settings, {TOPIC: recorder}, outbox_poll_seconds=0.1)):
        await waiting()

        event_id = await enqueue_event(db)

        await until(lambda: recorder.ids == [event_id], what="the poll")


async def test_a_worker_whose_listener_is_cut_off_keeps_polling_and_listens_again(
    db: Database, superuser_db: Database, settings: Settings, logs: LogReader
) -> None:
    recorder = Recorder()
    OUTBOX_DEAD.set(UNSET)

    async with running(
        worker_for(db, settings, {TOPIC: recorder}, outbox_poll_seconds=0.1)
    ) as task:
        await waiting()
        (listener,) = await listeners(db)

        async with superuser_db.transaction() as session:
            await session.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": listener})
        event_id = await enqueue_event(db)

        await until(lambda: recorder.ids == [event_id], what="the poll")

        async def listening_again() -> bool:
            now_listening = await listeners(db)
            return len(now_listening) == 1 and now_listening != [listener]

        await until(listening_again, what="listening again")
        assert not task.done()

    assert "worker.listen_lost" in {line["event"] for line in logs()}


async def test_a_worker_that_cannot_listen_at_all_still_drains(
    db: Database, settings: Settings, logs: LogReader, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(*_args: object, **_kwargs: object) -> None:
        raise ConnectionError("no connection for the listener")

    recorder = Recorder()
    worker = worker_for(db, settings, {TOPIC: recorder}, outbox_poll_seconds=0.1)
    monkeypatch.setattr("corridor.worker.main._Listener._listen", refuse)
    OUTBOX_DEAD.set(UNSET)

    async with running(worker) as task:
        await waiting()
        event_id = await enqueue_event(db)

        await until(lambda: recorder.ids == [event_id], what="the poll")
        assert not task.done()

    assert await listeners(db) == []
    assert "worker.listen_failed" in {line["event"] for line in logs()}


async def test_a_stopped_worker_is_no_longer_listening(db: Database, settings: Settings) -> None:
    OUTBOX_DEAD.set(UNSET)

    async with running(worker_for(db, settings, {}, outbox_poll_seconds=NEVER)):
        await waiting()
        (listener,) = await listeners(db)

    # Its connection went back to the pool, and whoever is handed it next hears nothing.
    async with db.transaction() as session:
        still = await session.execute(
            text(
                "SELECT count(*) FROM pg_stat_activity WHERE pid = :pid AND query LIKE 'LISTEN %'"
            ),
            {"pid": listener},
        )
        assert still.scalar_one() == 0


# --- stopping --------------------------------------------------------------------------------


async def test_request_stop_lets_the_handler_in_flight_finish_and_claims_nothing_new(
    db: Database, settings: Settings
) -> None:
    first, second = await enqueue_events(db, 2)
    gate = Gate()
    worker = worker_for(db, settings, {TOPIC: gate}, outbox_batch_size=1, outbox_poll_seconds=NEVER)
    task = asyncio.create_task(worker.run())
    try:
        await gate.entered()

        worker.request_stop()
        # The stop is not a cancellation: the worker is still there, waiting on its handler.
        done, _pending = await asyncio.wait({task}, timeout=0.2)
        assert done == set()
        assert (await load(db, first)).status == "processing"

        gate.open()
        async with asyncio.timeout(5):
            await task
    finally:
        await reap(task)

    assert [event.id for event in gate.events] == [first]
    assert (await load(db, first)).status == "done"
    assert (await load(db, second)).status == "pending"
    assert await status_counts(db) == {"done": 1, "pending": 1}


async def test_a_waiting_worker_stops_at_once(db: Database, settings: Settings) -> None:
    OUTBOX_DEAD.set(UNSET)
    worker = worker_for(db, settings, {}, outbox_poll_seconds=NEVER)
    task = asyncio.create_task(worker.run())
    try:
        await waiting()
        worker.request_stop()
        async with asyncio.timeout(5):
            await task
    finally:
        await reap(task)


# --- alongside the loop ----------------------------------------------------------------------


async def test_the_scheduler_runs_alongside_the_outbox_loop(
    db: Database, settings: Settings
) -> None:
    ran = asyncio.Event()

    async def job(_db: Database) -> None:
        ran.set()

    worker = worker_for(
        db, settings, {}, jobs=(Job("test.job", 3600, job),), outbox_poll_seconds=NEVER
    )
    async with running(worker):
        async with asyncio.timeout(5):
            await ran.wait()

    async with db.transaction() as session:
        finished = await session.execute(
            text("SELECT last_finished_at IS NOT NULL FROM job_runs WHERE name = 'test.job'")
        )
        assert finished.scalar_one() is True


async def test_the_gauges_show_the_queue_after_a_drain(
    db: Database, settings: Settings, clock: ManualClock
) -> None:
    await enqueue_event(db, "test.unhandled")
    await enqueue_event(db, available_at=clock.now() + timedelta(minutes=10))
    await enqueue_event(db, available_at=clock.now() + timedelta(minutes=20))
    OUTBOX_DEAD.set(UNSET)

    async with running(worker_for(db, settings, {}, outbox_poll_seconds=NEVER)):
        await waiting()

    assert gauge("corridor_outbox_dead") == 1
    assert gauge("corridor_outbox_pending") == 2
    assert gauge("corridor_outbox_oldest_pending_seconds") == 0


# --- what a worker is built from -------------------------------------------------------------


async def test_the_registry_handles_a_ping_by_doing_nothing(db: Database) -> None:
    registry = build_registry()
    event_id = await enqueue_event(db, "worker.ping")

    handler = registry.handler_for("worker.ping")
    assert handler is not None
    assert await handler(await load(db, event_id)) is None  # type: ignore[func-returns-value]
    assert registry.handler_for("test.created") is None


# --- as a real process -----------------------------------------------------------------------


def start_worker_process(
    database: postgres.TestDatabase, settings: Settings
) -> subprocess.Popen[str]:
    environment = {
        **os.environ,
        "CORRIDOR_DATABASE_URL": database.app_url,
        "CORRIDOR_REDIS_URL": settings.redis_url.get_secret_value(),
    }
    return subprocess.Popen(
        [sys.executable, "-m", "corridor.worker"],
        cwd=postgres.REPO_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


@pytest.mark.parametrize("stop_signal", [signal.SIGTERM, signal.SIGINT])
async def test_the_worker_process_handles_pings_and_exits_cleanly_when_signalled(
    database: postgres.TestDatabase, settings: Settings, db: Database, stop_signal: signal.Signals
) -> None:
    # The process reads the system clock, so this test leaves the application clock alone.
    await enqueue_events(db, 3, "worker.ping")
    process = start_worker_process(database, settings)
    try:
        await until(
            lambda: _is(status_counts(db), {"done": 3}), within=30, what="the first three pings"
        )
        # Enqueued while the process is waiting.
        await enqueue_events(db, 2, "worker.ping")
        await until(lambda: _is(status_counts(db), {"done": 5}), within=10, what="the next two")

        process.send_signal(stop_signal)
        # In a thread, because waiting for a process blocks.
        returncode = await asyncio.to_thread(process.wait, 10)
    finally:
        if process.poll() is None:
            process.kill()
        output, errors = await asyncio.to_thread(process.communicate)

    assert returncode == 0, errors
    assert await status_counts(db) == {"done": 5}
    events = [line for line in output.splitlines() if line.startswith("{")]
    assert any('"worker.started"' in line for line in events)
    assert any('"worker.stopped"' in line for line in events)
    assert "Traceback" not in output + errors
    # The hourly purge ran at start-up, through the real jobs.
    async with db.transaction() as session:
        jobs = await session.execute(text("SELECT name, last_error FROM job_runs"))
        assert [tuple(row) for row in jobs] == [("outbox.purge_finished", None)]


async def _is(actual: Any, expected: Any) -> bool:
    return bool(await actual == expected)
