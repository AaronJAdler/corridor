"""The names providers go by in Corridor, and which kind of money each moves.

A provider's name is written into ledger accounts, deposits, withdrawals, webhook events and
audit records, and it is the last part of the path its webhooks are delivered to. It is the
adapter's ``name``. Nothing that is written down is spelled out anywhere else: a module that
needs to know which provider it is dealing with is told by the adapter, the event's path or
the row, and a module that needs to know what kind of provider that is asks here.

A new provider is one more entry in a set below, one adapter whose ``name`` is that entry,
and, if it delivers webhooks, one more value of ``corridor.webhooks.Provider``, which the
webhook table's check constraint must also allow.
"""

from typing import Final, Literal

# How money arrives or leaves: over a bank rail, or on a chain.
FlowKind = Literal["bank", "chain"]

SIM_BANK: Final = "simbank"
SIM_CUSTODY: Final = "simcustody"
SIM_RATES: Final = "simfx"
INCREASE: Final = "increase"

BANK_RAILS: Final = frozenset({SIM_BANK, INCREASE})
CUSTODIANS: Final = frozenset({SIM_CUSTODY})


def flow_kind(provider: str) -> FlowKind | None:
    """What a provider moves: money over a bank rail or on a chain, or None if it is neither.

    A rate source moves nothing, and a name nobody has registered is not a provider.
    """
    if provider in BANK_RAILS:
        return "bank"
    if provider in CUSTODIANS:
        return "chain"
    return None


def is_bank_rail(provider: str) -> bool:
    return provider in BANK_RAILS


def require_bank_rail(provider: str) -> None:
    """Refuse a name that is not a bank rail, before an event is read as a bank's."""
    if not is_bank_rail(provider):
        raise ValueError(f"{provider!r} is not a bank rail")
