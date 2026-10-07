"""The dispatcher: claims due events, runs their handlers and records what happened.

Delivery is at least once. An event is claimed for a limited time; a worker that dies
leaves its claim to run out and another worker picks the event up. A handler can therefore
run more than once, and making that harmless is the handler's job. See section 7.1 of the
architecture.
"""

import asyncio
import random
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Final

from corridor.outbox.retry import next_delay
from corridor.outbox.service import claim, mark_dead, mark_done, mark_for_retry
from corridor.outbox.types import OutboxEvent
from corridor.platform.clock import utcnow
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.logging import bind_context, clear_context, get_logger, scrub
from corridor.platform.metrics import OUTBOX_PROCESSED

log = get_logger(__name__)

# A handler is an entry point: it opens its own transactions and may call a provider.
Handler = Callable[[OutboxEvent], Awaitable[None]]

MAX_ERROR_LENGTH: Final = 500
# How much of its claim a handler may use. The rest is for recording what happened.
HANDLER_SHARE_OF_CLAIM: Final = 0.9
# What is stored when the description of a failure is itself something PostgreSQL refuses.
UNRECORDABLE: Final = "the failure could not be recorded"


class Registry:
    """Which handler runs for which topic. A topic has one handler."""

    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}

    def register(self, topic: str, handler: Handler) -> None:
        if topic in self._handlers:
            raise ValueError(f"topic {topic!r} already has a handler")
        self._handlers[topic] = handler

    def handler_for(self, topic: str) -> Handler | None:
        return self._handlers.get(topic)


class Dispatcher:
    """Claims batches of due events and sees each one through its handler."""

    def __init__(
        self,
        db: Database,
        registry: Registry,
        settings: Settings,
        *,
        rng: random.Random | None = None,
    ) -> None:
        self._db = db
        self._registry = registry
        self._batch_size = settings.outbox_batch_size
        self._concurrency = settings.outbox_concurrency
        self._claim_seconds = settings.outbox_claim_seconds
        self._max_attempts = settings.outbox_max_attempts
        self._rng = rng if rng is not None else random.Random()  # noqa: S311 - retry jitter, not a secret

    async def run_once(self) -> int:
        """Claim one batch of due events and process it. Returns how many were claimed.

        A handler's failure is recorded on its event and is never raised from here. What
        can be raised is a failure to claim at all: the database could not be reached.
        """
        async with self._db.transaction() as session:
            events = await claim(
                session,
                now=utcnow(),
                limit=self._batch_size,
                claim_seconds=self._claim_seconds,
                max_attempts=self._max_attempts,
            )
        # The claim is committed. From here on the dispatcher holds no transaction open
        # while a handler runs, however long a provider takes to answer.

        slots = asyncio.Semaphore(self._concurrency)

        async def process(event: OutboxEvent) -> None:
            async with slots:
                await self._process(event)

        # A task for each event, so each has a log context of its own and a handler that
        # is cancelled takes no other event with it.
        async with asyncio.TaskGroup() as group:
            for event in events:
                group.create_task(process(event))
        return len(events)

    async def drain(self) -> int:
        """Run batch after batch until there is nothing left to claim. Returns the total."""
        total = 0
        while claimed := await self.run_once():
            total += claimed
        return total

    async def _process(self, event: OutboxEvent) -> None:
        # The request id that enqueued the event goes into the log context while the event
        # is handled, so a request can be followed across the queue. This task's context
        # is its own: nothing bound here outlives the event or leaks into another.
        clear_context()
        request_id = event.context.get("request_id")
        if request_id is not None:
            bind_context(request_id=request_id)

        try:
            await self._handle(event)
        except Exception:
            # The outcome could not be recorded: the database went away, say. Nothing is
            # lost. The event stays claimed, the claim runs out, and it is processed again.
            log.exception("outbox.event_unrecorded", **_fields(event))

    async def _handle(self, event: OutboxEvent) -> None:
        handler = self._registry.handler_for(event.topic)
        if handler is None:
            # Not retried: nothing will change until a deploy brings a handler.
            await self._bury(event, f"no handler for topic {event.topic}")
            return

        try:
            # Stopped before the claim runs out, so that the failure is recorded under this
            # claim and the event is not picked up by a second worker while this one is
            # still at it.
            async with asyncio.timeout(self._claim_seconds * HANDLER_SHARE_OF_CLAIM):
                await handler(event)
        except Exception as error:
            # Cancellation is deliberately not caught. It means the worker is shutting
            # down, not that the handler failed: the event is left claimed and is picked
            # up again when the claim runs out.
            await self._record_failure(event, error)
        else:
            await self._record_success(event)

    async def _record_success(self, event: OutboxEvent) -> None:
        async with self._db.transaction() as session:
            recorded = await mark_done(session, event, now=utcnow())
        if not recorded:
            _log_lost_claim(event)
            return
        OUTBOX_PROCESSED.labels(topic=event.topic, outcome="done").inc()
        log.info("outbox.event_done", **_fields(event))

    async def _record_failure(self, event: OutboxEvent, error: Exception) -> None:
        try:
            await self._record_described(event, _describe(error), error)
        except Exception:
            # The failure could not be written down as described. Left at that, the event
            # would stay claimed, come round when the claim ran out, fail the same way and
            # never end. It is ended here with words that are certain to be storable. If
            # the database is simply away this raises too, and the claim runs out as usual.
            log.exception("outbox.failure_unrecordable", **_fields(event))
            await self._bury(event, UNRECORDABLE)

    async def _record_described(
        self, event: OutboxEvent, description: str, error: Exception
    ) -> None:
        if event.attempts >= self._max_attempts:
            await self._bury(event, description, error)
            return

        delay = next_delay(event.attempts, rng=self._rng)
        async with self._db.transaction() as session:
            recorded = await mark_for_retry(
                session,
                event,
                available_at=utcnow() + timedelta(seconds=delay),
                error=description,
            )
        if not recorded:
            _log_lost_claim(event)
            return
        OUTBOX_PROCESSED.labels(topic=event.topic, outcome="retry").inc()
        log.warning(
            "outbox.event_retry",
            **_fields(event),
            retry_in_seconds=round(delay, 3),
            error=description,
            exc_info=error,
        )

    async def _bury(
        self, event: OutboxEvent, description: str, error: Exception | None = None
    ) -> None:
        async with self._db.transaction() as session:
            recorded = await mark_dead(session, event, now=utcnow(), error=description)
        if not recorded:
            _log_lost_claim(event)
            return
        OUTBOX_PROCESSED.labels(topic=event.topic, outcome="dead").inc()
        log.error("outbox.event_dead", **_fields(event), error=description, exc_info=error)


def _fields(event: OutboxEvent) -> dict[str, object]:
    """What every log line about an event carries. Never the payload."""
    return {"event_id": str(event.id), "topic": event.topic, "attempt": event.attempts}


def _log_lost_claim(event: OutboxEvent) -> None:
    # This worker's claim ran out before it finished and another worker has the event now.
    # That worker's result is the one that counts, so this one's was not written.
    log.warning("outbox.claim_lost", **_fields(event))


def _describe(error: Exception) -> str:
    """An error as it is stored on its event: type and message, without secrets, cut to fit."""
    try:
        message = str(error)
    except Exception:
        message = "(no printable message)"
    described = f"{type(error).__name__}: {message}" if message else type(error).__name__
    # Scrubbed before it is cut: a secret cut in half no longer looks like one.
    # PostgreSQL refuses a NUL in text, so one in a message is dropped.
    return str(scrub(described)).replace("\x00", "")[:MAX_ERROR_LENGTH]
