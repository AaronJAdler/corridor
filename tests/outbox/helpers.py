"""Helpers shared by the outbox tests and the worker tests."""

import asyncio
import contextlib
import inspect
import json
import logging
import random
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from typing import Any

import asyncpg
import pytest
from sqlalchemy import text

from corridor import outbox
from corridor.outbox import Dispatcher, Handler, OutboxEvent, Registry
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.logging import configure_logging

TOPIC = "test.created"


async def until(
    condition: Callable[[], Any | Awaitable[Any]], *, within: float = 5.0, what: str = "it"
) -> None:
    """Wait for something asynchronous to happen: poll ``condition`` until it holds.

    This is the only real waiting these tests do, and only for things that are really
    asynchronous (a notification arriving, a process exiting). It returns as soon as the
    condition holds and fails the test when the time runs out.
    """
    try:
        async with asyncio.timeout(within):
            # The condition is a database row or another process, never an asyncio.Event.
            while not await _holds(condition):  # noqa: ASYNC110
                await asyncio.sleep(0.01)
    except TimeoutError:
        raise AssertionError(f"{what} did not happen within {within} seconds") from None


async def _holds(condition: Callable[[], Any | Awaitable[Any]]) -> bool:
    result = condition()
    if inspect.isawaitable(result):
        result = await result
    return bool(result)


async def reap(task: asyncio.Task[Any]) -> None:
    """Make sure a task a test started is over before the test ends, whatever happened."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


LogReader = Callable[[], list[dict[str, Any]]]


@contextlib.contextmanager
def json_logs(capsys: pytest.CaptureFixture[str]) -> Iterator[LogReader]:
    """Send logging through the real pipeline and yield a reader of what was written.

    Each call to the reader returns the lines logged since the last one, parsed.
    """
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    configure_logging("INFO", "json")
    capsys.readouterr()

    def read() -> list[dict[str, Any]]:
        written = capsys.readouterr().out.splitlines()
        return [json.loads(line) for line in written if line.startswith("{")]

    try:
        yield read
    finally:
        root.handlers, root.level = saved_handlers, saved_level


class NotifyProbe:
    """A session of its own that listens on a channel and remembers what it was sent."""

    def __init__(self, connection: asyncpg.Connection, channel: str) -> None:
        self._connection = connection
        self._channel = channel
        self._payloads: list[str] = []

    async def start(self) -> None:
        await self._connection.add_listener(self._channel, self._on_notify)

    def _on_notify(
        self, _connection: asyncpg.Connection, _pid: int, _channel: str, payload: str
    ) -> None:
        self._payloads.append(payload)

    async def received(self) -> list[str]:
        """Every notification PostgreSQL has sent this session so far.

        A round trip settles the question without waiting: PostgreSQL hands a session the
        notifications it is holding for it before it reports a command complete, and the
        driver runs their callbacks before it resumes whoever awaited the command.
        """
        await self._connection.execute("SELECT 1")
        return list(self._payloads)


# --- events ----------------------------------------------------------------------------------


async def enqueue_event(
    db: Database, topic: str = TOPIC, payload: Mapping[str, Any] | None = None, **options: Any
) -> uuid.UUID:
    """Enqueue one event in a transaction of its own, as a request would."""
    async with db.transaction() as session:
        event_id = await outbox.enqueue(session, topic, payload or {}, **options)
    assert event_id is not None
    return event_id


async def enqueue_events(db: Database, how_many: int, topic: str = TOPIC) -> list[uuid.UUID]:
    """Enqueue several events in one transaction. Their ids ascend in the order returned."""
    ids: list[uuid.UUID] = []
    async with db.transaction() as session:
        for number in range(how_many):
            event_id = await outbox.enqueue(session, topic, {"number": number})
            assert event_id is not None
            ids.append(event_id)
    return ids


async def load(db: Database, event_id: uuid.UUID) -> OutboxEvent:
    async with db.transaction() as session:
        event = await outbox.get_event(session, event_id)
    assert event is not None, f"event {event_id} does not exist"
    return event


async def status_counts(db: Database) -> dict[str, int]:
    async with db.transaction() as session:
        rows = await session.execute(
            text("SELECT status, count(*) AS events FROM outbox_events GROUP BY status")
        )
        return {row.status: row.events for row in rows}


# --- dispatchers and handlers ----------------------------------------------------------------


def dispatcher_for(
    db: Database,
    settings: Settings,
    handlers: Mapping[str, Handler],
    *,
    rng: random.Random | None = None,
    **overrides: Any,
) -> Dispatcher:
    """A dispatcher with these handlers, on the test's settings unless overridden."""
    registry = Registry()
    for topic, handler in handlers.items():
        registry.register(topic, handler)
    return Dispatcher(db, registry, settings.model_copy(update=overrides), rng=rng)


class Recorder:
    """A handler that succeeds and remembers the events it was given."""

    def __init__(self) -> None:
        self.events: list[OutboxEvent] = []

    async def __call__(self, event: OutboxEvent) -> None:
        self.events.append(event)

    @property
    def ids(self) -> list[uuid.UUID]:
        return [event.id for event in self.events]


class Failing:
    """A handler that fails every time, and counts how often it was asked."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error if error is not None else RuntimeError("the provider is down")
        self.calls = 0

    async def __call__(self, event: OutboxEvent) -> None:
        self.calls += 1
        raise self.error


class Gate:
    """A handler that stops part-way until the test lets it go on.

    It is how a test holds an event in flight: ``entered`` says a handler has started,
    ``open()`` lets every handler finish, by returning or by raising ``error``.
    """

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.events: list[OutboxEvent] = []
        self._entered = asyncio.Event()
        self._opened = asyncio.Event()

    async def __call__(self, event: OutboxEvent) -> None:
        self.events.append(event)
        self._entered.set()
        await self._opened.wait()
        if self.error is not None:
            raise self.error

    async def entered(self) -> None:
        async with asyncio.timeout(5):
            await self._entered.wait()

    def open(self) -> None:
        self._opened.set()


class Longest(random.Random):
    """A generator that always draws the top of the range: the longest backoff allowed."""

    def uniform(self, a: float, b: float) -> float:
        return b


class Shortest(random.Random):
    """A generator that always draws the bottom of the range."""

    def uniform(self, a: float, b: float) -> float:
        return a


async def abandon(db: Database, settings: Settings, topic: str = TOPIC) -> Dispatcher:
    """Claim what is due as a worker would, then die: the events are left ``processing``.

    The worker is stopped by cancellation while its handlers are running, which records
    nothing, exactly as a killed process records nothing.
    """
    gate = Gate()
    dispatcher = dispatcher_for(db, settings, {topic: gate})
    working = asyncio.create_task(dispatcher.run_once())
    try:
        await gate.entered()
    finally:
        await reap(working)
    return dispatcher
