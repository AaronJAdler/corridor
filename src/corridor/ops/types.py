"""What the operations module hands to its callers."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from corridor.ledger import Direction

AdjustmentStatus = Literal["pending", "approved", "rejected"]


@dataclass(frozen=True, slots=True)
class Leg:
    """One posting an adjustment asks for."""

    account_id: uuid.UUID
    # The account's asset, stated by whoever wrote the leg and checked against the account:
    # an amount means nothing without it.
    asset: str
    direction: Direction
    # Minor units of that asset.
    amount: int


@dataclass(frozen=True, slots=True)
class Adjustment:
    """A journal entry written by hand: asked for by one admin, decided by another."""

    id: uuid.UUID
    requested_by: uuid.UUID
    # Set by an approval, and never the requester.
    approved_by: uuid.UUID | None
    status: AdjustmentStatus
    reason: str
    legs: tuple[Leg, ...]
    # The journal entry the approval posted.
    entry_id: uuid.UUID | None
    created_at: datetime
    decided_at: datetime | None
