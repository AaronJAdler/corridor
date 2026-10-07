"""What the payments module hands to its callers."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

TransferStatus = Literal["completed"]


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
