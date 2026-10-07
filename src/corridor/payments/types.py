"""What the payments module hands to its callers."""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

TransferStatus = Literal["completed"]

# The providers money comes in and goes out through, by the names their ledger accounts,
# their webhooks and their rows carry.
BANK_PROVIDER: Final = "simbank"
CUSTODY_PROVIDER: Final = "simcustody"

# How money arrives or leaves: over a bank rail, or on a chain.
FlowKind = Literal["bank", "chain"]

DepositStatus = Literal["pending", "completed", "suspense", "failed", "returned"]
# ``submitting`` is the state between deciding to ask the provider and knowing its answer:
# the provider may or may not have the payout, so the funds stay reserved and the user can
# no longer call the withdrawal back.
WithdrawalStatus = Literal[
    "held",
    "under_review",
    "submitting",
    "submitted",
    "completed",
    "failed",
    "canceled",
    "released",
]


@dataclass(frozen=True, slots=True)
class Transfer:
    """Money one user sent to another, as recorded."""

    id: uuid.UUID
    sender_id: uuid.UUID
    recipient_id: uuid.UUID
    asset: str
    # What the recipient received, in minor units. The sender paid ``amount + fee``.
    amount: int
    fee: int
    status: TransferStatus
    # The journal entry that moved the money.
    entry_id: uuid.UUID
    memo: str | None
    # Who asked for it: the sender, or an agent acting for the sender.
    initiated_by_type: Literal["user", "agent"]
    initiated_by_id: uuid.UUID
    created_at: datetime


@dataclass(frozen=True, slots=True)
class DepositInstruction:
    """Where a user sends one asset to have it credited."""

    user_id: uuid.UUID
    asset: str
    kind: FlowKind
    provider: str
    # The provider's id for the virtual account or the address.
    provider_ref: str
    # What the user needs: the bank account to pay into, or the network and the address.
    details: Mapping[str, str]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Deposit:
    """Money that arrived at a provider, as recorded."""

    id: uuid.UUID
    # None when it could not be attributed to anyone: the money is in suspense.
    user_id: uuid.UUID | None
    asset: str
    amount: int
    kind: FlowKind
    status: DepositStatus
    provider: str
    # The provider's id for the deposit.
    provider_ref: str
    # The journal entry that credited it, once there is one.
    entry_id: uuid.UUID | None
    tx_hash: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SuspenseSettlement:
    """A deposit that has left suspense, to a user or back where it came from, and the
    journal entry that took it out."""

    deposit: Deposit
    entry_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class Beneficiary:
    """A user's external bank account, as the provider's token and a masked value. The
    account number is not here because Corridor does not have it."""

    id: uuid.UUID
    user_id: uuid.UUID
    asset: str
    provider: str
    # The provider's token for the account: what a payout is addressed to.
    provider_ref: str
    holder_name: str
    account_mask: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Withdrawal:
    """Money on its way out of a user's wallet, as recorded."""

    id: uuid.UUID
    user_id: uuid.UUID
    asset: str
    # What leaves for the beneficiary or the address. The user is debited ``amount + fee``.
    amount: int
    fee: int
    kind: FlowKind
    # One of the two is set: a beneficiary for a bank withdrawal, an address for a chain one.
    beneficiary_id: uuid.UUID | None
    to_address: str | None
    status: WithdrawalStatus
    provider: str
    # The provider's id for the payout, once the provider is known to have one.
    provider_ref: str | None
    # What the provider charged Corridor, known when the payout settles.
    provider_fee: int | None
    failure_reason: str | None
    hold_entry_id: uuid.UUID
    # The entry that ended the hold: the settlement, or the release.
    final_entry_id: uuid.UUID | None
    # Who asked for it: the user, or an agent acting for the user.
    initiated_by_type: Literal["user", "agent"]
    initiated_by_id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    submitted_at: datetime | None
