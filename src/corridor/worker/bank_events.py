"""Reading a bank rail's events from its API, for a Corridor that no webhook reaches.

A run in GitHub Actions has no address the provider could deliver to. The events are listed
instead, and stored exactly as a delivered one is: under the provider's name and the event's
id, so that a listed event and a delivered one are the same row, and processed by the same
handler, which reads what happened from the object the event names.

The listing is authenticated by the API key, as every other call to the provider is, so an
event read this way needs no signature: it comes from the provider's own API, and what it
says is checked against the object anyway.
"""

from datetime import datetime, timedelta
from typing import Final

from corridor import webhooks
from corridor.platform.clock import utcnow
from corridor.platform.db import Database
from corridor.platform.logging import get_logger
from corridor.providers import BankEventSource
from corridor.webhooks import Envelope

log = get_logger(__name__)

BANK_EVENT_POLL_JOB: Final = "bank.poll_events"
BANK_EVENT_POLL_INTERVAL_SECONDS: Final = 10.0
# How far back a worker that has just started looks. Further than any outage a deposit
# should wait out; what is older is reconciliation's to find.
LOOKBACK: Final = timedelta(hours=24)
# How far behind the newest event the next listing starts. An event is listed by when it
# was created, and one created a moment before another can appear in the listing after it.
# What is read twice is stored once.
OVERLAP: Final = timedelta(minutes=5)


class BankEventPoller:
    """Lists a bank rail's events and stores the new ones, from where it last got to."""

    def __init__(self, source: BankEventSource) -> None:
        self._source = source
        self._since: datetime | None = None

    async def __call__(self, db: Database) -> None:
        since = self._since if self._since is not None else utcnow() - LOOKBACK
        # The listing is a network call, made before any transaction is opened.
        listed = await self._source.list_events(since=since)
        provider = webhooks.provider_named(self._source.name)
        stored = 0
        if listed:
            async with db.transaction() as session:
                for event in listed:
                    envelope = Envelope(event.id, event.category, dict(event.payload))
                    recorded = await webhooks.record(session, provider, envelope)
                    stored += recorded.created
            newest = max(event.created_at for event in listed)
            self._since = max(since, newest - OVERLAP)
        else:
            self._since = since
        log.info("bank_events.polled", provider=provider.value, listed=len(listed), stored=stored)
