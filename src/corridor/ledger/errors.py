"""What the ledger refuses, and how."""

import uuid

from corridor.platform.errors import DomainError
from corridor.platform.money import format_amount


class InsufficientFunds(DomainError):
    """A constrained account would go below zero. Raised before anything is written."""

    status = 402
    code = "insufficient_funds"
    title = "Insufficient funds"

    def __init__(
        self, *, account_id: uuid.UUID, asset_code: str, balance: int, required: int
    ) -> None:
        super().__init__(
            f"Available balance is {format_amount(balance, asset_code)} {asset_code}; "
            f"{format_amount(required, asset_code)} {asset_code} is required."
        )
        self.account_id = account_id
        self.asset_code = asset_code
        self.balance = balance
        self.required = required


class LedgerError(Exception):
    """A caller broke the ledger's contract. A bug, not something a client can cause."""


class InvalidEntry(LedgerError):
    """The draft is not a valid journal entry: it does not balance, for example."""


class UnknownAccount(LedgerError):
    """A posting names an account that does not exist."""


class ConflictingEntry(LedgerError):
    """The business event was already posted, with different postings."""


class EntryNotFound(LedgerError):
    """There is no journal entry with that id."""
