"""Everything the simulator remembers, in one object.

The application holds exactly one ``SimState``. Forgetting everything is replacing it.
"""

import asyncio

from corridor_sim.bank import BankBooks
from corridor_sim.chaos import FaultTable
from corridor_sim.clock import SimClock
from corridor_sim.custody import CustodyBooks
from corridor_sim.fx import RateWalk
from corridor_sim.ids import IdFactory
from corridor_sim.settings import SimSettings
from corridor_sim.webhooks import Delivery, WebhookQueue, WebhookSender


class SimState:
    def __init__(self, settings: SimSettings, sender: WebhookSender) -> None:
        self.settings = settings
        self.sender = sender
        self.clock = SimClock(settings.clock_mode, settings.start_time)
        self.ids = IdFactory(settings.seed)
        self.webhooks = WebhookQueue(settings, self.clock, self.ids, sender)
        self.bank = BankBooks(settings, self.clock, self.ids, self.webhooks)
        self.custody = CustodyBooks(settings, self.clock, self.ids, self.webhooks)
        self.fx = RateWalk(settings.seed, self.clock.now())
        self.faults = FaultTable()
        # One tick at a time, so that no event is sent by two of them at once.
        self._ticking = asyncio.Lock()

    def fresh(self) -> SimState:
        """A state that remembers nothing, with the same settings and the same client."""
        return SimState(self.settings, self.sender)

    async def tick(self) -> None:
        """Do everything that the passing of time causes, up to the clock's present."""
        async with self._ticking:
            now = self.clock.now()
            self.bank.settle_due(now)
            self.custody.advance_chain(now)
            self.fx.advance_to(now)
            await self.webhooks.deliver_due()

    async def deliver(self) -> list[Delivery]:
        """Attempt every webhook delivery that is due, without anything else time does."""
        async with self._ticking:
            return await self.webhooks.deliver_due()
