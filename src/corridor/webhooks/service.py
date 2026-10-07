"""Webhook ingestion: believe a delivery, store it once, and later process it.

Receiving and processing are separate on purpose. The request stores the event and its
outbox row in one transaction and answers; the worker processes it afterwards. A slow
handler therefore never makes a provider retry, and a stored event can be processed again.
See section 7.3 of the architecture.
"""

import json
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Annotated, Any, Final, cast

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import RowMapping, Table, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import outbox
from corridor.platform.clock import utcnow
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.ids import new_id
from corridor.platform.logging import get_logger
from corridor.webhooks import signature
from corridor.webhooks.errors import (
    EventNotFound,
    InvalidSignature,
    MalformedEvent,
    UnknownProvider,
)
from corridor.webhooks.models import WebhookEventRow
from corridor.webhooks.types import (
    Envelope,
    EventOutcome,
    Provider,
    Recorded,
    WebhookEvent,
)

log = get_logger(__name__)

# The outbox topic an accepted delivery is enqueued under. The worker registers the handler
# that passes the event's id to ``process``.
RECEIVED_TOPIC: Final = "webhook.received"

# A Core table: every statement against it is written out here.
_events = cast(Table, WebhookEventRow.__table__)

# What a provider's event id and event type are made of. Both end up in a text column, a
# log line and a registry key, so they are held to plain identifiers of a bounded length.
_IDENTIFIER: Final = r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"
_MAX_EVENT_ID_LENGTH: Final = 255
_MAX_TYPE_LENGTH: Final = 100

_MALFORMED: Final = "The body is not an event as the provider contract describes one."

# A handler is an entry point: it opens its own transactions, as an outbox handler does.
WebhookHandler = Callable[[Database, WebhookEvent], Awaitable[None]]


class WebhookRegistry:
    """Which handler runs for which provider and event type. A pair has one handler.

    The worker fills it. That is how an event reaches the payment state machines without
    this module calling them.
    """

    def __init__(self) -> None:
        self._handlers: dict[tuple[Provider, str], WebhookHandler] = {}

    def register(self, provider: Provider, event_type: str, handler: WebhookHandler) -> None:
        key = (Provider(provider), event_type)
        if key in self._handlers:
            raise ValueError(f"{provider} event type {event_type!r} already has a handler")
        self._handlers[key] = handler

    def handler_for(self, provider: Provider, event_type: str) -> WebhookHandler | None:
        return self._handlers.get((provider, event_type))


# --- believing a delivery --------------------------------------------------------------------


def provider_named(name: str) -> Provider:
    """The provider a delivery path names. Anything outside the allow list is not found."""
    try:
        return Provider(name)
    except ValueError:
        raise UnknownProvider from None


def _secrets_for(settings: Settings, provider: Provider) -> list[bytes]:
    configured = {
        Provider.SIMBANK: settings.bank_rail_webhook_secrets,
        Provider.SIMCUSTODY: settings.custody_webhook_secrets,
    }[provider]
    return [secret.get_secret_value().encode() for secret in configured]


def verify_delivery(
    settings: Settings, provider: Provider, signature_headers: Sequence[str], body: bytes
) -> None:
    """Refuse a delivery that does not prove it came from ``provider``.

    ``signature_headers`` is every ``X-Signature`` header of the request and ``body`` is
    the request body exactly as it arrived. The reason for a refusal goes to the log and
    nowhere else.
    """
    refusal = signature.check(
        secrets=_secrets_for(settings, provider),
        headers=signature_headers,
        body=body,
        now=utcnow(),
        tolerance_seconds=settings.webhook_tolerance_seconds,
    )
    if refusal is not None:
        log.warning("webhook.signature_refused", provider=provider.value, reason=refusal.value)
        raise InvalidSignature


def validate_secrets(settings: Settings) -> None:
    """Refuse a configuration in which the two providers share a webhook secret.

    The provider's name is not among the bytes that are signed. With a shared secret, a
    delivery one provider signed would verify on the other provider's path, and an event
    could be replayed as though the other had sent it.
    """
    bank = {secret.get_secret_value() for secret in settings.bank_rail_webhook_secrets}
    custody = {secret.get_secret_value() for secret in settings.custody_webhook_secrets}
    if bank & custody:
        # Which secret is shared is not said: an error message is not a place for one.
        raise ValueError(
            "bank_rail_webhook_secrets and custody_webhook_secrets share a secret; "
            "each provider needs secrets of its own"
        )


# --- the envelope ----------------------------------------------------------------------------


class _EnvelopeModel(BaseModel):
    # Strict, and an unknown field is refused: the body is stored and later handed to the
    # code that moves money, so it is held to the contract and not coerced towards it.
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: Annotated[str, Field(pattern=_IDENTIFIER, max_length=_MAX_EVENT_ID_LENGTH)]
    type: Annotated[str, Field(pattern=_IDENTIFIER, max_length=_MAX_TYPE_LENGTH)]
    created_at: AwareDatetime
    data: dict[str, Any]


def _refuse_constant(_name: str) -> None:
    # NaN and the infinities are not JSON, and PostgreSQL refuses them in jsonb.
    raise ValueError("not a JSON number")


def _contains_nul(value: object) -> bool:
    """Whether a string anywhere in a JSON value holds U+0000, which jsonb cannot store."""
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(_contains_nul(key) or _contains_nul(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_nul(item) for item in value)
    return False


def parse_envelope(body: bytes) -> Envelope:
    """Read a verified body as an event, or refuse it. Call this only after ``verify_delivery``.

    What is kept is the document as it was sent, not the validated model written out
    again, so that the stored event is the provider's own.
    """
    try:
        model = _EnvelopeModel.model_validate_json(body)
        payload = json.loads(body, parse_constant=_refuse_constant)
    except ValidationError, ValueError, RecursionError:
        # Without the cause: a validation error repeats the input it refused.
        raise MalformedEvent(_MALFORMED) from None
    # What PostgreSQL would refuse to store is refused here, where it is the sender's
    # mistake and not a failure of ours that the provider would retry six times.
    if not isinstance(payload, dict) or _contains_nul(payload):
        raise MalformedEvent(_MALFORMED)
    return Envelope(event_id=model.id, type=model.type, payload=payload)


# --- recording -------------------------------------------------------------------------------


async def record(session: AsyncSession, provider: Provider, envelope: Envelope) -> Recorded:
    """Store a verified event and enqueue its processing, in the caller's transaction.

    A provider delivers at least once. The second delivery of an event id conflicts with
    the first, writes nothing and enqueues nothing, and the caller acknowledges it all the
    same. Two deliveries racing each other are settled by the unique constraint: the later
    insert waits for the earlier transaction and then finds the row.
    """
    inserted = await session.execute(
        pg_insert(_events)
        .values(
            id=new_id(),
            provider=provider.value,
            event_id=envelope.event_id,
            type=envelope.type,
            payload=dict(envelope.payload),
            received_at=utcnow(),
            processed_at=None,
            outcome=None,
        )
        .on_conflict_do_nothing(index_elements=[_events.c.provider, _events.c.event_id])
        .returning(_events.c.id)
    )
    new: uuid.UUID | None = inserted.scalar_one_or_none()
    if new is None:
        existing: uuid.UUID = (
            await session.execute(
                select(_events.c.id).where(
                    _events.c.provider == provider.value, _events.c.event_id == envelope.event_id
                )
            )
        ).scalar_one()
        return Recorded(id=existing, created=False)

    # In the same transaction as the row: an event is stored if and only if its processing
    # is queued, so an acknowledged delivery is never one that nothing will process.
    await outbox.enqueue(session, RECEIVED_TOPIC, {"webhook_event_id": str(new)})
    return Recorded(id=new, created=True)


def _event(row: RowMapping) -> WebhookEvent:
    outcome: str | None = row["outcome"]
    return WebhookEvent(
        id=row["id"],
        provider=Provider(row["provider"]),
        event_id=row["event_id"],
        type=row["type"],
        payload=row["payload"],
        received_at=row["received_at"],
        processed_at=row["processed_at"],
        outcome=EventOutcome(outcome) if outcome is not None else None,
    )


async def find_event(session: AsyncSession, webhook_event_id: uuid.UUID) -> WebhookEvent | None:
    row = (
        (await session.execute(select(_events).where(_events.c.id == webhook_event_id)))
        .mappings()
        .one_or_none()
    )
    return _event(row) if row is not None else None


async def get_event(session: AsyncSession, webhook_event_id: uuid.UUID) -> WebhookEvent:
    event = await find_event(session, webhook_event_id)
    if event is None:
        raise EventNotFound(f"there is no webhook event {webhook_event_id}")
    return event


# --- processing ------------------------------------------------------------------------------


async def _finish(
    session: AsyncSession, webhook_event_id: uuid.UUID, outcome: EventOutcome, now: datetime
) -> None:
    # Only where it is still unprocessed: of two workers that both ran the handler, the
    # first to finish records when, and the second changes nothing.
    await session.execute(
        update(_events)
        .where(_events.c.id == webhook_event_id, _events.c.processed_at.is_(None))
        .values(processed_at=now, outcome=outcome.value)
    )


async def process(db: Database, webhook_event_id: uuid.UUID, registry: WebhookRegistry) -> None:
    """Apply one stored event: the body of the ``webhook.received`` outbox handler.

    An entry point. It holds no transaction open while the handler runs, because a handler
    owns its own. Delivery from the outbox is at least once, so an event that is already
    processed is left alone, and a handler may still run twice if a worker dies between
    the handler and the mark; handlers are idempotent for that reason.

    A handler's exception is not caught. The event stays unprocessed and the outbox
    retries it.
    """
    async with db.transaction() as session:
        event = await get_event(session, webhook_event_id)
    if event.processed_at is not None:
        return

    handler = registry.handler_for(event.provider, event.type)
    if handler is None:
        # Stored and ignored: a type this version does not know is not an error, and the
        # row is there if a later version wants it.
        async with db.transaction() as session:
            await _finish(session, event.id, EventOutcome.IGNORED, utcnow())
        log.info("webhook.ignored", provider=event.provider.value, event_type=event.type)
        return

    await handler(db, event)

    async with db.transaction() as session:
        await _finish(session, event.id, EventOutcome.PROCESSED, utcnow())
    log.info("webhook.processed", provider=event.provider.value, event_type=event.type)
