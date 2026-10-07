"""What the webhook tests share: synthetic secrets, a signer written from the contract's
text, and readers of the two tables a delivery writes."""

import hashlib
import hmac
import json
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from pydantic import SecretStr
from sqlalchemy import text

from corridor.platform.clock import utcnow
from corridor.platform.config import Settings
from corridor.platform.db import Database

# Not secrets: they sign test deliveries and exist only here.
BANK_SECRET = "test-bank-webhook-signing-value-0001"  # pragma: allowlist secret
BANK_NEXT_SECRET = "test-bank-webhook-signing-value-0002"  # pragma: allowlist secret
CUSTODY_SECRET = "test-custody-webhook-signing-value-01"  # pragma: allowlist secret
UNKNOWN_SECRET = "test-nobody-webhook-signing-value-001"  # pragma: allowlist secret

TOPIC = "webhook.received"


def with_secrets(
    settings: Settings,
    *,
    bank: tuple[str, ...] = (BANK_SECRET,),
    custody: tuple[str, ...] = (CUSTODY_SECRET,),
) -> Settings:
    return settings.model_copy(
        update={
            "bank_rail_webhook_secrets": [SecretStr(secret) for secret in bank],
            "custody_webhook_secrets": [SecretStr(secret) for secret in custody],
        }
    )


def without_secrets(settings: Settings) -> Settings:
    return with_secrets(settings, bank=(), custody=())


def unix(moment: datetime | None = None) -> int:
    return int((moment if moment is not None else utcnow()).timestamp())


def sign(secret: str, body: bytes, timestamp: int | str | None = None) -> str:
    """The ``X-Signature`` header as the provider contract describes it: HMAC-SHA256, keyed
    with the shared secret, over ``<t>.<raw request body>``."""
    t = unix() if timestamp is None else timestamp
    digest = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={digest}"


def envelope(
    event_id: str = "evt_3n8b1c",
    event_type: str = "payout.completed",
    data: Mapping[str, Any] | None = None,
) -> bytes:
    document = {
        "id": event_id,
        "type": event_type,
        "created_at": "2026-01-15T12:00:00Z",
        "data": dict(data) if data is not None else {"payout_id": "po_2h5j8n", "amount": "100.00"},
    }
    return json.dumps(document, separators=(",", ":")).encode()


async def stored_events(db: Database) -> list[dict[str, Any]]:
    async with db.transaction() as session:
        rows = await session.execute(
            text(
                "SELECT id, provider, event_id, type, payload, received_at, processed_at, outcome"
                " FROM webhook_events ORDER BY id"
            )
        )
        return [dict(row) for row in rows.mappings()]


async def enqueued(db: Database) -> list[uuid.UUID]:
    """The webhook event each ``webhook.received`` outbox event names, oldest first."""
    async with db.transaction() as session:
        rows = await session.execute(
            text("SELECT payload FROM outbox_events WHERE topic = :topic ORDER BY id"),
            {"topic": TOPIC},
        )
        return [uuid.UUID(payload["webhook_event_id"]) for payload in rows.scalars()]
