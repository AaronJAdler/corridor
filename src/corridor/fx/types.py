"""What the FX module hands to its callers."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

QuoteStatus = Literal["open", "used"]


@dataclass(frozen=True, slots=True)
class Quote:
    """A price offered to one user for a short time, with both amounts already worked out."""

    id: uuid.UUID
    user_id: uuid.UUID
    sell_asset: str
    buy_asset: str
    # Minor units of each asset. A conversion moves exactly these.
    sell_amount: int
    buy_amount: int
    # Units of the buy asset for one unit of the sell asset: what the customer gets, and
    # the mid-market rate it was made from.
    rate: Decimal
    mid: Decimal
    status: QuoteStatus
    expires_at: datetime
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Conversion:
    """A quote that was executed, with what it moved."""

    id: uuid.UUID
    quote_id: uuid.UUID
    user_id: uuid.UUID
    # The journal entry that moved the money.
    entry_id: uuid.UUID
    sell_asset: str
    buy_asset: str
    sell_amount: int
    buy_amount: int
    rate: Decimal
    created_at: datetime
