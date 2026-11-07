"""Which payment function each provider event reaches.

The webhooks module stores an event and knows nothing of what it means; the payments module
applies one and knows nothing of how it arrived. This table is where the two meet, and it is
here because the worker is the only module above both.
"""

from collections.abc import Awaitable, Callable, Mapping
from functools import partial
from typing import Any

from corridor import payments
from corridor.platform.db import Database
from corridor.webhooks import Provider, WebhookEvent, WebhookHandler, WebhookRegistry

# A payment function that applies one provider event: an entry point, given the event's data.
Apply = Callable[[Database, Mapping[str, Any]], Awaitable[None]]


def build_webhook_registry() -> WebhookRegistry:
    """The handler of every provider event this version acts on. An event of any other type
    is stored and ignored."""
    # The bank handlers are told which bank rail spoke: the name on the path it delivered to.
    # A rail whose events these are routes its own, once its adapter reads them.
    bank = Provider.SIMBANK
    named = bank.value
    routes: tuple[tuple[Provider, str, Apply], ...] = (
        (bank, "deposit.received", partial(payments.apply_bank_deposit_received, provider=named)),
        (bank, "deposit.returned", partial(payments.apply_bank_deposit_returned, provider=named)),
        (bank, "payout.completed", partial(payments.apply_payout_completed, provider=named)),
        (bank, "payout.failed", partial(payments.apply_payout_failed, provider=named)),
        (Provider.SIMCUSTODY, "deposit.detected", payments.apply_chain_deposit_detected),
        (Provider.SIMCUSTODY, "deposit.confirmed", payments.apply_chain_deposit_confirmed),
        (Provider.SIMCUSTODY, "deposit.failed", payments.apply_chain_deposit_failed),
        (Provider.SIMCUSTODY, "withdrawal.completed", payments.apply_withdrawal_completed),
        (Provider.SIMCUSTODY, "withdrawal.failed", payments.apply_withdrawal_failed),
    )
    registry = WebhookRegistry()
    for provider, event_type, apply in routes:
        registry.register(provider, event_type, _handing_over_the_data(apply))
    return registry


def _handing_over_the_data(apply: Apply) -> WebhookHandler:
    async def handle(db: Database, event: WebhookEvent) -> None:
        # Only the part that depends on the event's type: the envelope is the webhooks
        # module's business, and a payment function is the same whoever calls it.
        await apply(db, event.data)

    return handle
