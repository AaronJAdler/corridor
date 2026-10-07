"""What the operations module hands to its callers."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from corridor.ledger import Direction

AdjustmentStatus = Literal["pending", "approved", "rejected"]
# ``manual`` is postings typed by an admin. The other two take one deposit out of suspense:
# to a user, or back through the provider it arrived at.
AdjustmentKind = Literal["manual", "suspense_release", "suspense_return"]


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
    kind: AdjustmentKind
    # The deposit a suspense adjustment takes out of suspense, and for a release the user
    # it goes to. None for an adjustment written by hand.
    deposit_id: uuid.UUID | None
    user_id: uuid.UUID | None
    reason: str
    legs: tuple[Leg, ...]
    # The journal entry the approval posted. For a suspense adjustment it is the deposit's
    # own release or return entry, with the legs recorded here as its postings.
    entry_id: uuid.UUID | None
    created_at: datetime
    decided_at: datetime | None
