"""The vocabulary of webhook ingestion: who sends events, and an event as it is stored."""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

# The longest body a delivery may have. The largest event in the provider contract is a
# few hundred bytes; the cap bounds what an unauthenticated request can make the API read.
MAX_BODY_BYTES: Final = 64 * 1024


class Provider(StrEnum):
    """The providers that deliver webhooks, by the name in the delivery path."""

    SIMBANK = "simbank"
    SIMCUSTODY = "simcustody"


class EventOutcome(StrEnum):
    # A handler was registered for the event's type and it finished.
    PROCESSED = "processed"
    # No handler was registered for the type: the event is kept and nothing was done.
    IGNORED = "ignored"


@dataclass(frozen=True, slots=True)
class Envelope:
    """A delivery's body, once it is known to be an event."""

    event_id: str
    type: str
    # The whole envelope as the provider sent it, so that the stored event can be replayed.
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Recorded:
    """What recording a delivery came to."""

    id: uuid.UUID
    # False when the provider had already delivered this event id and nothing was written.
    created: bool


@dataclass(frozen=True, slots=True)
class WebhookEvent:
    """One stored event, as its row was when it was read."""

    id: uuid.UUID
    provider: Provider
    # The provider's id for the event, unique for that provider.
    event_id: str
    type: str
    payload: Mapping[str, Any]
    received_at: datetime
    processed_at: datetime | None
    outcome: EventOutcome | None

    @property
    def data(self) -> Mapping[str, Any]:
        """The part of the envelope that depends on the event's type."""
        data: Mapping[str, Any] = self.payload["data"]
        return data
