"""The worker process: drains the outbox and runs the scheduled jobs.

The loop polls. A notification from the outbox's insert trigger only cuts the wait short:
PostgreSQL does not keep notifications for a session that is not connected, so one sent
while the listener is reconnecting is lost, and the poll is what finds its event.
"""

import asyncio
import contextlib
import signal
from collections.abc import Sequence
from types import FrameType

import asyncpg  # type: ignore[import-untyped]  # asyncpg ships no type information
from prometheus_client import start_http_server
from sqlalchemy.ext.asyncio import AsyncConnection

from corridor import outbox
from corridor.outbox import Dispatcher, OutboxEvent, Registry
from corridor.platform.config import Settings
from corridor.platform.db import Database, create_engine
from corridor.platform.logging import configure_logging, get_logger
from corridor.platform.metrics import OUTBOX_DEAD, OUTBOX_OLDEST_PENDING_SECONDS, OUTBOX_PENDING
from corridor.worker.jobs import build_jobs
from corridor.worker.scheduler import Job, Scheduler

log = get_logger(__name__)

PING_TOPIC = "worker.ping"
TRANSFER_COMPLETED_TOPIC = "transfer.completed"


class Worker:
    """One worker: the outbox loop, with the scheduler running beside it."""

    def __init__(
        self, db: Database, registry: Registry, settings: Settings, *, jobs: Sequence[Job] = ()
    ) -> None:
        self._db = db
        self._dispatcher = Dispatcher(db, registry, settings)
        self._scheduler = Scheduler(db, jobs)
        self._poll_seconds = settings.outbox_poll_seconds
        self._stop = asyncio.Event()
        # Set by a notification and by a request to stop: either ends the wait at once.
        self._wake = asyncio.Event()
        self._listener = _Listener(db, self._wake)

    def request_stop(self) -> None:
        """Ask the loop to end. The batch in hand is finished; nothing new is claimed."""
        self._stop.set()
        self._wake.set()

    async def run(self) -> None:
        """Drain the outbox, wait for a notification or the poll interval, and repeat."""
        log.info("worker.started")
        scheduling = asyncio.create_task(self._scheduler.run(self._stop))
        try:
            while not self._stop.is_set():
                await self._listener.ensure()
                # Cleared before the drain, not after: a notification that arrives while
                # the drain is running must cut the next wait short, not be forgotten.
                self._wake.clear()
                await self._drain()
                if self._stop.is_set():
                    break
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(self._poll_seconds):
                        await self._wake.wait()
        finally:
            # Whatever ended the loop, the scheduler is not left running without it.
            self._stop.set()
            try:
                await scheduling
            finally:
                await self._listener.close()
                log.info("worker.stopped")

    async def _drain(self) -> None:
        try:
            while not self._stop.is_set() and await self._dispatcher.run_once():
                pass
            async with self._db.transaction() as session:
                queue = await outbox.stats(session)
        except Exception:
            # The database could not be reached. Nothing is lost by waiting: the events are
            # still there at the next poll, and a worker that died here would help nobody.
            log.exception("worker.drain_failed")
            return
        OUTBOX_PENDING.set(queue.pending)
        OUTBOX_OLDEST_PENDING_SECONDS.set(queue.oldest_pending_seconds)
        OUTBOX_DEAD.set(queue.dead)


class _Listener:
    """A connection that listens for the outbox's notifications and sets an event on each.

    It is a convenience and is allowed to fail: whatever goes wrong here is logged and
    tried again at the worker's next cycle, and the worker polls in the meantime.
    """

    def __init__(self, db: Database, wake: asyncio.Event) -> None:
        self._db = db
        self._wake = wake
        self._connection: AsyncConnection | None = None
        self._driver: asyncpg.Connection | None = None

    async def ensure(self) -> None:
        """Listen, if not listening already. Never raises."""
        if self._driver is not None and not self._driver.is_closed():
            return
        try:
            if self._connection is not None:
                log.warning("worker.listen_lost")
                await self._discard()
            await self._listen()
        except Exception:
            log.warning("worker.listen_failed", exc_info=True)
            with contextlib.suppress(Exception):
                await self._discard()

    async def _listen(self) -> None:
        # Autocommit, so the connection sits idle between notifications and not idle in a
        # transaction, which the server would end after a few seconds.
        self._connection = await self._db.engine.connect()
        await self._connection.execution_options(isolation_level="AUTOCOMMIT")
        pooled = await self._connection.get_raw_connection()
        driver: asyncpg.Connection = pooled.driver_connection
        await driver.add_listener(outbox.NOTIFY_CHANNEL, self._on_notify)
        self._driver = driver
        log.info("worker.listening", channel=outbox.NOTIFY_CHANNEL)

    def _on_notify(
        self, _connection: asyncpg.Connection, _pid: int, _channel: str, _payload: str
    ) -> None:
        self._wake.set()

    async def close(self) -> None:
        """Stop listening and give the connection back. Never raises."""
        try:
            if self._driver is not None and not self._driver.is_closed():
                await self._driver.remove_listener(outbox.NOTIFY_CHANNEL, self._on_notify)
                self._driver = None
        except Exception:
            log.warning("worker.unlisten_failed", exc_info=True)
        with contextlib.suppress(Exception):
            await self._discard()

    async def _discard(self) -> None:
        connection, driver = self._connection, self._driver
        self._connection = self._driver = None
        if connection is None:
            return
        try:
            if driver is not None or connection.closed:
                # Still listening, as far as anyone knows, or broken: either way it must
                # not go back into the pool for someone else to be handed.
                await connection.invalidate()
        finally:
            await connection.close()


async def _ping(_event: OutboxEvent) -> None:
    """Does nothing. Enqueue a ``worker.ping`` to see that a deployed worker is working."""


async def _transfer_completed(_event: OutboxEvent) -> None:
    """Does nothing yet: nothing consumes a completed transfer, and an event with no handler
    would go dead instead of done."""


def build_registry() -> Registry:
    """The handlers that exist today, by topic."""
    registry = Registry()
    registry.register(PING_TOPIC, _ping)
    registry.register(TRANSFER_COMPLETED_TOPIC, _transfer_completed)
    return registry


def run(settings: Settings) -> None:
    """Run a worker until it is told to stop with SIGINT or SIGTERM. Blocks."""
    configure_logging(settings.log_level, settings.log_format)
    if settings.worker_metrics_port != 0:
        start_http_server(settings.worker_metrics_port, addr=settings.worker_metrics_host)
    asyncio.run(_serve(settings))


async def _serve(settings: Settings) -> None:
    db = Database(create_engine(settings, application_name="corridor-worker"))
    worker = Worker(db, build_registry(), settings, jobs=build_jobs(settings))
    loop = asyncio.get_running_loop()

    def on_signal(_signal: int, _frame: FrameType | None) -> None:
        # A signal handler interrupts whatever the main thread was doing, so all it does
        # is hand the request to the loop.
        loop.call_soon_threadsafe(worker.request_stop)

    # `signal.signal` and not `loop.add_signal_handler`, which does not exist on Windows.
    previous = {
        number: signal.signal(number, on_signal) for number in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        await worker.run()
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
        await db.dispose()
