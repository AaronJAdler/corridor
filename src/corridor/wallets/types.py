"""What the wallets module hands to its callers."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from corridor.ledger import Direction


@dataclass(frozen=True, slots=True)
class Wallet:
    """A user's two ledger accounts in one asset."""

    user_id: uuid.UUID
    asset: str
    available_account_id: uuid.UUID
    held_account_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class WalletBalance:
    """What a user holds in one asset, in minor units."""

    asset: str
    available: int
    # Reserved for something in flight, such as a withdrawal. Not spendable.
    held: int
    total: int
    available_account_id: uuid.UUID
    held_account_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class StatementEntry:
    """One movement on a wallet's available balance."""

    # The position of the movement in the ledger's global order. Statements page on it.
    seq: int
    entry_id: uuid.UUID
    kind: str
    asset: str
    # A credit adds to the balance and a debit takes from it.
    direction: Direction
    amount: int
    balance_after: int
    posted_at: datetime
