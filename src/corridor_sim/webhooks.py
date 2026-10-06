"""Webhooks: one queue of events for both providers, and their delivery.

An event is due as soon as it exists, but nothing is sent when it is made: sending happens
when the simulator ticks. Delivery is at least once, unordered and not guaranteed, exactly
as the contract warns a receiver, and each of those can be provoked on demand.
"""

import asyncio
import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final, Literal

import httpx

from corridor_sim.clock import SimClock, format_time
from corridor_sim.ids import IdFactory
from corridor_sim.settings import SimSettings

Provider = Literal["bank", "custody"]
EventStatus = Literal["pending", "delivered", "abandoned", "dropped", "undeliverable"]

# How long after each failed attempt the next one is due, in simulator seconds. A sixth
# failure has no entry here: the event is abandoned.
RETRY_DELAYS: Final = (1, 2, 4, 8, 16)
MAX_ATTEMPTS: Final = len(RETRY_DELAYS) + 1


class WebhookSender:
    """The HTTP client deliveries go out on.

    It belongs to the application rather than to the state, so that forgetting everything
    does not close a connection pool.
    """

    def __init__(self, client: httpx.AsyncClient | None, timeout_seconds: float) -> None:
        self.client = client
        self.timeout_seconds = timeout_seconds


@dataclass(frozen=True, slots=True)
class Attempt:
    """One request sent for an event, and what came of it."""

    at: datetime
    # The receiver's answer, or None when there was none: then ``error`` says why.
    status_code: int | None
    error: str | None
    # An extra copy sent after the event had already been delivered.
    duplicate: bool = False

    @property
    def succeeded(self) -> bool:
        return self.status_code is not None and 200 <= self.status_code < 300


@dataclass(slots=True)
class Event:
    id: str
    type: str
    provider: Provider
    created_at: datetime
    data: Mapping[str, object]
    # The request body, serialised once: every attempt signs and sends these same bytes.
    body: bytes
    status: EventStatus
    next_attempt_at: datetime | None
    attempts: list[Attempt] = field(default_factory=list)


@dataclass(slots=True)
class Behaviour:
    """How deliveries misbehave. The default is a well-behaved provider."""

    duplicates: int = 0
    drop_types: frozenset[str] = frozenset()
    hold: bool = False
    reverse: bool = False


@dataclass(frozen=True, slots=True)
class Delivery:
    """The outcome of one attempt, as ``/_control/webhooks/deliver`` reports it."""

    event: Event
    attempt: Attempt


class WebhookQueue:
    def __init__(
        self, settings: SimSettings, clock: SimClock, ids: IdFactory, sender: WebhookSender
    ) -> None:
        self._clock = clock
        self._ids = ids
        self._sender = sender
        self._urls: dict[Provider, str | None] = {
            "bank": settings.bank_webhook_url,
            "custody": settings.custody_webhook_url,
        }
        self._secrets: dict[Provider, bytes] = {
            "bank": settings.bank_webhook_secret.get_secret_value().encode(),
            "custody": settings.custody_webhook_secret.get_secret_value().encode(),
        }
        self.behaviour = Behaviour()
        self.events: list[Event] = []

    def emit(self, provider: Provider, event_type: str, data: Mapping[str, object]) -> Event:
        """Record an event. It is due at once, and sent at the next tick."""
        now = self._clock.now()
        event_id = self._ids.new("evt_")
        status: EventStatus = "pending"
        if event_type in self.behaviour.drop_types:
            status = "dropped"
        elif self._urls[provider] is None:
            status = "undeliverable"
        envelope = {
            "id": event_id,
            "type": event_type,
            "created_at": format_time(now),
            "data": dict(data),
        }
        event = Event(
            id=event_id,
            type=event_type,
            provider=provider,
            created_at=now,
            data=dict(data),
            body=json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode(),
            status=status,
            next_attempt_at=now if status == "pending" else None,
        )
        self.events.append(event)
        return event

    async def deliver_due(self) -> list[Delivery]:
        """Attempt every delivery that is due. A failure is recorded and never raised."""
        if self.behaviour.hold:
            return []
        now = self._clock.now()
        due = [
            event
            for event in self.events
            if event.status == "pending"
            and event.next_attempt_at is not None
            and event.next_attempt_at <= now
        ]
        if self.behaviour.reverse:
            due.reverse()

        deliveries: list[Delivery] = []
        for event in due:
            attempt = await self._send(event)
            event.attempts.append(attempt)
            deliveries.append(Delivery(event, attempt))
            if attempt.succeeded:
                event.status = "delivered"
                event.next_attempt_at = None
                for _ in range(self.behaviour.duplicates):
                    extra = await self._send(event, duplicate=True)
                    event.attempts.append(extra)
                    deliveries.append(Delivery(event, extra))
            elif len(event.attempts) >= MAX_ATTEMPTS:
                event.status = "abandoned"
                event.next_attempt_at = None
            else:
                delay = RETRY_DELAYS[len(event.attempts) - 1]
                event.next_attempt_at = attempt.at + timedelta(seconds=delay)
        return deliveries

    async def _send(self, event: Event, *, duplicate: bool = False) -> Attempt:
        at = self._clock.now()
        timestamp = int(at.timestamp())
        signature = hmac.new(
            self._secrets[event.provider], f"{timestamp}.".encode() + event.body, hashlib.sha256
        ).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "X-Signature": f"t={timestamp},v1={signature}",
        }
        url = self._urls[event.provider]
        client = self._sender.client
        try:
            if url is None or client is None:
                raise RuntimeError("there is nowhere to send this event")
            # The client's own timeout is not enforced by every transport, so the deadline
            # is kept here as well.
            async with asyncio.timeout(self._sender.timeout_seconds):
                response = await client.post(url, content=event.body, headers=headers)
        except TimeoutError, httpx.TimeoutException:
            return Attempt(at, None, "timeout", duplicate)
        except Exception as error:
            # Whatever went wrong on the way is the receiver's failure to answer, not the
            # simulator's failure to run.
            return Attempt(at, None, type(error).__name__, duplicate)
        return Attempt(at, response.status_code, None, duplicate)


def event_document(event: Event) -> dict[str, object]:
    return {
        "id": event.id,
        "type": event.type,
        "provider": event.provider,
        "created_at": format_time(event.created_at),
        "data": dict(event.data),
        "status": event.status,
        "next_attempt_at": format_time(event.next_attempt_at)
        if event.next_attempt_at is not None
        else None,
        "attempts": [attempt_document(attempt) for attempt in event.attempts],
    }


def attempt_document(attempt: Attempt) -> dict[str, object]:
    return {
        "at": format_time(attempt.at),
        "status_code": attempt.status_code,
        "error": attempt.error,
        "duplicate": attempt.duplicate,
    }


def delivery_document(delivery: Delivery) -> dict[str, object]:
    return {
        "event_id": delivery.event.id,
        "type": delivery.event.type,
        "provider": delivery.event.provider,
        "event_status": delivery.event.status,
        **attempt_document(delivery.attempt),
    }
