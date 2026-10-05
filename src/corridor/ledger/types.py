"""The ledger's vocabulary: account kinds, entry drafts and what posting returns."""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final, Literal


class Direction(StrEnum):
    DEBIT = "D"
    CREDIT = "C"


class AccountKind(StrEnum):
    USER_AVAILABLE = "user_available"
    USER_HELD = "user_held"
    USER_RECEIVABLE = "user_receivable"
    BANK_SETTLEMENT = "bank_settlement"
    CUSTODY_OMNIBUS = "custody_omnibus"
    SUSPENSE = "suspense"
    FX_POSITION = "fx_position"
    FEE_REVENUE = "fee_revenue"
    PROVIDER_FEE_EXPENSE = "provider_fee_expense"


@dataclass(frozen=True, slots=True)
class AccountSpec:
    category: Literal["asset", "liability", "revenue", "expense"]
    normal_side: Direction
    # A constrained account may never go below zero. It has a cached balance, and posting to
    # it takes a row lock. Unconstrained accounts have neither.
    constrained: bool
    # Who the account belongs to: one per user, one per provider, or one for the system.
    scope: Literal["user", "provider", "system"]


# The chart of accounts, from the operator's point of view: what customers hold is a
# liability, what sits at a bank or custodian is an asset.
CHART: Final[Mapping[AccountKind, AccountSpec]] = MappingProxyType(
    {
        AccountKind.USER_AVAILABLE: AccountSpec("liability", Direction.CREDIT, True, "user"),
        AccountKind.USER_HELD: AccountSpec("liability", Direction.CREDIT, True, "user"),
        AccountKind.USER_RECEIVABLE: AccountSpec("asset", Direction.DEBIT, True, "user"),
        AccountKind.BANK_SETTLEMENT: AccountSpec("asset", Direction.DEBIT, False, "provider"),
        AccountKind.CUSTODY_OMNIBUS: AccountSpec("asset", Direction.DEBIT, False, "provider"),
        AccountKind.SUSPENSE: AccountSpec("liability", Direction.CREDIT, False, "system"),
        AccountKind.FX_POSITION: AccountSpec("asset", Direction.DEBIT, False, "system"),
        AccountKind.FEE_REVENUE: AccountSpec("revenue", Direction.CREDIT, False, "system"),
        AccountKind.PROVIDER_FEE_EXPENSE: AccountSpec("expense", Direction.DEBIT, False, "system"),
    }
)


@dataclass(frozen=True, slots=True)
class Account:
    id: uuid.UUID
    asset_code: str
    kind: AccountKind
    normal_side: Direction
    constrained: bool
    owner_id: uuid.UUID | None
    provider: str | None


@dataclass(frozen=True, slots=True)
class PostingDraft:
    account_id: uuid.UUID
    direction: Direction
    amount: int


def debit(account_id: uuid.UUID, amount: int) -> PostingDraft:
    return PostingDraft(account_id, Direction.DEBIT, amount)


def credit(account_id: uuid.UUID, amount: int) -> PostingDraft:
    return PostingDraft(account_id, Direction.CREDIT, amount)


@dataclass(frozen=True, slots=True)
class EntryDraft:
    """A journal entry that has not been posted.

    ``(source_type, source_id, kind)`` names the business event. Posting the same event
    again returns the entry that already exists, so a handler that runs twice moves money
    once.
    """

    kind: str
    source_type: str
    source_id: str
    postings: tuple[PostingDraft, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)
    reverses_entry_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class PostedPosting:
    seq: int
    account_id: uuid.UUID
    asset_code: str
    direction: Direction
    amount: int
    # None for an unconstrained account, which has no cached balance.
    balance_after: int | None


@dataclass(frozen=True, slots=True)
class PostedEntry:
    id: uuid.UUID
    kind: str
    source_type: str
    source_id: str
    metadata: Mapping[str, Any]
    reverses_entry_id: uuid.UUID | None
    posted_at: datetime
    postings: tuple[PostedPosting, ...]
    # False when this call found the entry already posted and changed nothing.
    created: bool


@dataclass(frozen=True, slots=True)
class StatementLine:
    """One posting on an account, with the entry it belongs to."""

    seq: int
    entry_id: uuid.UUID
    entry_kind: str
    source_type: str
    source_id: str
    metadata: Mapping[str, Any]
    direction: Direction
    amount: int
    balance_after: int | None
    posted_at: datetime
