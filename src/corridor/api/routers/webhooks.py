"""Inbound webhooks: where a provider tells Corridor what happened to a payout or a deposit.

The one route that money-moving events arrive on without a bearer credential. What stands
in for it is the provider's signature over the raw body, so the order here is fixed: read
the bytes, verify them, and only then look at what they say. The answer is sent after the
event and its outbox row have committed, and processing happens later in the worker.
"""

from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import webhooks
from corridor.api.deps import Db, SettingsDep
from corridor.api.ratelimit import rate_limit
from corridor.platform.logging import get_logger
from corridor.platform.metrics import WEBHOOK_DELIVERIES

log = get_logger(__name__)

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])

_SIGNATURE_HEADER = "x-signature"


class ReceivedResponse(BaseModel):
    received: Literal[True] = True


async def _read_capped(request: Request) -> bytes:
    """The request body exactly as it arrived, refused once it is longer than the cap.

    Counted as it is read and not taken from ``Content-Length``, which a sender can leave
    out or get wrong. The bytes are kept as they are: the signature is over them.
    """
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > webhooks.MAX_BODY_BYTES:
            raise webhooks.PayloadTooLarge
    return bytes(body)


@router.post(
    "/{provider}",
    response_model=ReceivedResponse,
    summary="Receive a provider's webhook",
    # A group of its own, so that a provider's burst is not counted against the bucket its
    # address shares with ordinary API calls beyond the global limit every route has.
    dependencies=[Depends(rate_limit("webhooks", per_minute=lambda s: s.rate_limit_per_minute))],
)
async def receive_webhook(
    provider: str, request: Request, db: Db, settings: SettingsDep
) -> ReceivedResponse:
    sender = webhooks.provider_named(provider)
    body = await _read_capped(request)
    try:
        webhooks.verify_delivery(settings, sender, request.headers.getlist(_SIGNATURE_HEADER), body)
    except webhooks.InvalidSignature:
        _count(sender, "bad_signature")
        raise
    # Only now is the body read as anything but bytes.
    try:
        envelope = webhooks.parse_envelope(body)
    except webhooks.MalformedEvent:
        _count(sender, "malformed")
        raise

    async def work(session: AsyncSession) -> webhooks.Recorded:
        return await webhooks.record(session, sender, envelope)

    recorded = await db.run(work)
    # After the commit: a delivery is counted as accepted once its event is stored.
    _count(sender, "accepted" if recorded.created else "duplicate")
    # After the commit. Neither the body nor the signature is logged, here or anywhere.
    log.info(
        "webhook.received",
        provider=sender.value,
        event_type=envelope.type,
        webhook_event_id=str(recorded.id),
        duplicate=not recorded.created,
    )
    return ReceivedResponse()


def _count(sender: webhooks.Provider, outcome: str) -> None:
    # The provider is one of the names on the allow list, never the text of the path.
    WEBHOOK_DELIVERIES.labels(provider=sender.value, outcome=outcome).inc()
