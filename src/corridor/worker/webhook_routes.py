"""Which payment function each provider event reaches.

The webhooks module stores an event and knows nothing of what it means; the payments module
applies one and knows nothing of how it arrived. This table is where the two meet, and it is
here because the worker is the only module above both.

The simulators' events carry what happened in their ``data``. Increase's say only which
object changed: what happened is read from that object, through the bank rail this worker
was given, and handed on in the same shape.
"""

from collections.abc import Awaitable, Callable, Mapping
from functools import partial
from typing import Any

from corridor import payments
from corridor.platform.db import Database
from corridor.platform.logging import get_logger
from corridor.providers import (
    INCREASE_EVENT_CATEGORIES,
    BankEventSource,
    BankEventType,
    BankRail,
    ProviderMisconfigured,
)
from corridor.webhooks import Provider, WebhookEvent, WebhookHandler, WebhookRegistry

log = get_logger(__name__)

# A payment function that applies one provider event: an entry point, given the event's data.
Apply = Callable[[Database, Mapping[str, Any]], Awaitable[None]]
# A bank rail's payment function, which is also told which rail spoke.
BankApply = Callable[..., Awaitable[None]]


def _bank_functions() -> Mapping[BankEventType, BankApply]:
    """The payment function each of the contract's bank events reaches."""
    return {
        "deposit.received": payments.apply_bank_deposit_received,
        "deposit.returned": payments.apply_bank_deposit_returned,
        "payout.completed": payments.apply_payout_completed,
        "payout.failed": payments.apply_payout_failed,
    }


def build_webhook_registry(bank: BankRail | None = None) -> WebhookRegistry:
    """The handler of every provider event this version acts on. An event of any other type
    is stored and ignored.

    ``bank`` is the bank rail this worker was given. Increase's events are read through it,
    and are always routed: one that arrives at a worker without Increase's adapter fails and
    is retried, rather than being marked as ignored and lost.
    """
    # The bank handlers are told which bank rail spoke: the name on the path it delivered to.
    sim = Provider.SIMBANK
    named = sim.value
    routes: tuple[tuple[Provider, str, Apply], ...] = (
        (sim, "deposit.received", partial(payments.apply_bank_deposit_received, provider=named)),
        (sim, "deposit.returned", partial(payments.apply_bank_deposit_returned, provider=named)),
        (sim, "payout.completed", partial(payments.apply_payout_completed, provider=named)),
        (sim, "payout.failed", partial(payments.apply_payout_failed, provider=named)),
        (Provider.SIMCUSTODY, "deposit.detected", payments.apply_chain_deposit_detected),
        (Provider.SIMCUSTODY, "deposit.confirmed", payments.apply_chain_deposit_confirmed),
        (Provider.SIMCUSTODY, "deposit.failed", payments.apply_chain_deposit_failed),
        (Provider.SIMCUSTODY, "withdrawal.completed", payments.apply_withdrawal_completed),
        (Provider.SIMCUSTODY, "withdrawal.failed", payments.apply_withdrawal_failed),
    )
    registry = WebhookRegistry()
    for provider, event_type, apply in routes:
        registry.register(provider, event_type, _handing_over_the_data(apply))
    # Increase's categories that can mean a deposit or a payout moved. Any other category
    # is stored and ignored.
    functions = _bank_functions()
    for category in sorted(INCREASE_EVENT_CATEGORIES):
        registry.register(Provider.INCREASE, category, _reading_the_object(bank, functions))
    return registry


def _handing_over_the_data(apply: Apply) -> WebhookHandler:
    async def handle(db: Database, event: WebhookEvent) -> None:
        # Only the part that depends on the event's type: the envelope is the webhooks
        # module's business, and a payment function is the same whoever calls it.
        await apply(db, event.data)

    return handle


def require_event_source(bank: BankRail | None, provider: str) -> BankEventSource:
    """The bank rail that can say what one of ``provider``'s events means, or a refusal.

    Only an adapter of the provider that sent the event is asked about it: another rail's
    reading of an object id it never issued would be no reading at all.
    """
    if bank is None or bank.name != provider or not isinstance(bank, BankEventSource):
        raise ProviderMisconfigured(
            "this worker's bank rail cannot read the provider's events",
            provider=provider,
            operation="resolve_event",
        )
    return bank


def _reading_the_object(
    bank: BankRail | None, functions: Mapping[BankEventType, BankApply]
) -> WebhookHandler:
    async def handle(db: Database, event: WebhookEvent) -> None:
        provider = event.provider.value
        source = require_event_source(bank, provider)
        # A network call, with no transaction open: the webhooks module holds none while a
        # handler runs, and each payment function opens its own.
        happened = await source.resolve_event(
            category=event.type,
            object_type=str(event.payload["associated_object_type"]),
            object_id=str(event.payload["associated_object_id"]),
        )
        for what in happened:
            await functions[what.type](db, what.data, provider=provider)
        log.info(
            "webhook.resolved",
            provider=provider,
            event_type=event.type,
            events=",".join(what.type for what in happened) or "none",
        )

    return handle
