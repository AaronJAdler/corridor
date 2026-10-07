"""What webhook ingestion refuses, and how."""

from corridor.platform.errors import DomainError


class InvalidSignature(DomainError):
    """The delivery did not prove it came from the provider.

    One error for every way that can fail, with no detail: an answer that told a missing
    header from a wrong digest from a stale timestamp would tell a forger which part of a
    forgery to work on next.
    """

    status = 401
    code = "invalid_signature"
    title = "Invalid signature"


class UnknownProvider(DomainError):
    """The path names a provider that sends Corridor no webhooks."""

    status = 404
    code = "not_found"
    title = "Not found"


class PayloadTooLarge(DomainError):
    """The body is longer than any event a provider sends."""

    status = 413
    code = "payload_too_large"
    title = "Payload too large"


class MalformedEvent(DomainError):
    """The body was signed by the provider and is still not an event.

    Raised with a fixed detail: nothing of the body is repeated in the answer.
    """

    status = 422
    code = "malformed_event"
    title = "Malformed event"


class EventNotFound(Exception):
    """There is no stored webhook event with that id. A bug, not something a provider can
    cause: the id comes from the outbox event that was written with the row."""
