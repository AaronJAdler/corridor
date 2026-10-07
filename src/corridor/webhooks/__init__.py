"""Inbound webhooks: verifying a provider's delivery, storing it once, and processing it.

Other modules use the names exported here and nothing else from this package.
"""

from corridor.webhooks.errors import (
    EventNotFound,
    InvalidSignature,
    MalformedEvent,
    PayloadTooLarge,
    UnknownProvider,
)
from corridor.webhooks.service import (
    RECEIVED_TOPIC,
    WebhookHandler,
    WebhookRegistry,
    find_event,
    get_event,
    parse_envelope,
    process,
    provider_named,
    record,
    validate_secrets,
    verify_delivery,
)
from corridor.webhooks.types import (
    MAX_BODY_BYTES,
    Envelope,
    EventOutcome,
    Provider,
    Recorded,
    WebhookEvent,
)

__all__ = [
    "MAX_BODY_BYTES",
    "RECEIVED_TOPIC",
    "Envelope",
    "EventNotFound",
    "EventOutcome",
    "InvalidSignature",
    "MalformedEvent",
    "PayloadTooLarge",
    "Provider",
    "Recorded",
    "UnknownProvider",
    "WebhookEvent",
    "WebhookHandler",
    "WebhookRegistry",
    "find_event",
    "get_event",
    "parse_envelope",
    "process",
    "provider_named",
    "record",
    "validate_secrets",
    "verify_delivery",
]
