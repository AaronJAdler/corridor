"""Which payment function each provider event reaches.

The webhooks module stores an event and knows nothing of what it means; the payments module
applies one and knows nothing of how it arrived. This table is where the two meet, and it is
here because the worker is the only module above both.
"""

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from corridor import payments
from corridor.platform.db import Database
from corridor.webhooks import Provider, WebhookEvent, WebhookHandler, WebhookRegistry

# A payment function that applies one provider event: an entry point, given the event's data.
Apply = Callable[[Database, Mapping[str, Any]], Awaitable[None]]


def build_webhook_registry() -> WebhookRegistry:
    """The handler of every provider event this version acts on. An event of any other type
    is stored and ignored."""
    routes: tuple[tuple[Provider, str, Apply], ...] = (
        (Provider.SIMBANK, "deposit.received", payments.apply_bank_deposit_received),
        (Provider.SIMBANK, "deposit.returned", payments.apply_bank_deposit_returned),
        (Provider.SIMBANK, "payout.completed", payments.apply_payout_completed),
        (Provider.SIMBANK, "payout.failed", payments.apply_payout_failed),
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
