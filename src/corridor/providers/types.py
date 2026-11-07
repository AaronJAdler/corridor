"""What the providers return, in Corridor's terms.

Amounts are integer minor units and times are timezone-aware UTC. The decimal strings of
the provider contract do not travel past the adapters.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

PayoutStatus = Literal["pending", "completed", "failed"]
WithdrawalStatus = Literal["pending", "broadcast", "completed", "failed"]
TransactionDirection = Literal["credit", "debit"]
# What a bank rail can tell Corridor has happened, in the words of the provider contract's
# bank webhooks.
BankEventType = Literal["deposit.received", "deposit.returned", "payout.completed", "payout.failed"]


@dataclass(frozen=True, slots=True)
class VirtualAccount:
    """The bank account a customer deposits into."""

    id: str
    customer_reference: str
    asset_code: str
    rail: str
    bank_name: str
    # Shown to the customer, and to nobody else: kept out of the repr so that a log line or
    # a traceback that prints the object does not print the numbers.
    account_number: str = field(repr=False)
    routing_number: str | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class Beneficiary:
    """An external bank account, as the provider's token and a masked value. The account
    number itself stays with the provider."""

    id: str
    asset_code: str
    rail: str
    holder_name: str
    account_mask: str


@dataclass(frozen=True, slots=True)
class Payout:
    id: str
    status: PayoutStatus
    beneficiary_id: str
    asset_code: str
    amount: int
    # What the provider charges Corridor when the payout completes, in the same asset.
    fee: int
    reference: str
    created_at: datetime
    settled_at: datetime | None
    failure_reason: str | None


@dataclass(frozen=True, slots=True)
class DepositAddress:
    id: str
    customer_reference: str
    asset_code: str
    network: str
    address: str


@dataclass(frozen=True, slots=True)
class Withdrawal:
    id: str
    status: WithdrawalStatus
    asset_code: str
    amount: int
    # Charged to Corridor only when the withdrawal completes.
    network_fee: int
    to_address: str
    reference: str
    tx_hash: str | None
    confirmations: int
    created_at: datetime
    completed_at: datetime | None
    failure_reason: str | None


@dataclass(frozen=True, slots=True)
class ProviderTransaction:
    """One settled movement on a provider's statement. ``amount`` is positive; the
    direction says which way it moved."""

    id: str
    type: str
    direction: TransactionDirection
    asset_code: str
    amount: int
    reference: str | None
    related_id: str | None
    occurred_at: datetime
    # Set for on-chain movements only.
    tx_hash: str | None


@dataclass(frozen=True, slots=True)
class ProviderStatement:
    """A provider's account of one asset over the half-open window ``[start, end)``."""

    asset_code: str
    start: datetime
    end: datetime
    transactions: tuple[ProviderTransaction, ...]
    # The balance at ``end``. Negative when Corridor is overdrawn at the provider.
    closing_balance: int


@dataclass(frozen=True, slots=True)
class Rate:
    """How many units of ``quote`` one unit of ``base`` buys, mid-market. ``as_of`` is when
    the provider last updated it; the consumer decides how old is too old."""

    base: str
    quote: str
    mid: Decimal
    as_of: datetime


@dataclass(frozen=True, slots=True)
class BankEvent:
    """Something a bank rail says has happened, read from what it holds now.

    ``data`` has the shape the provider contract gives the ``data`` of a bank webhook of
    this type, amounts as decimal strings in major units, so that the payment functions read
    one shape whichever bank rail spoke. It may name the sender of a deposit: it is handed
    to those functions and stored nowhere by the adapter.
    """

    type: BankEventType
    data: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True, slots=True)
class ListedEvent:
    """One of a provider's events, as its API lists it rather than as a webhook delivers it.

    ``payload`` is the event as the provider's webhook would carry it, so a listed event and
    a delivered one are stored the same and deduplicated against each other by ``id``.
    """

    id: str
    category: str
    created_at: datetime
    payload: Mapping[str, Any]
