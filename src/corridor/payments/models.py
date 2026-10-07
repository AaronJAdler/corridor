"""The transfers table. Private to this module: nothing outside ``corridor.payments`` imports it.

The authoritative definition, with grants, is ``migrations/versions/0008_payments.py``.
A test compares the two.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Index, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from corridor.platform.db import Base, MinorUnits


class TransferRow(Base):
    """A row of ``transfers``. Named for the row, because ``Transfer`` is the frozen value
    that the service hands to other modules."""

    __tablename__ = "transfers"
    __table_args__ = (
        UniqueConstraint("entry_id"),
        CheckConstraint("amount > 0", name="amount"),
        CheckConstraint("fee >= 0", name="fee"),
        CheckConstraint("status IN ('completed')", name="status"),
        CheckConstraint("sender_id <> recipient_id", name="distinct_parties"),
        CheckConstraint("char_length(memo) <= 140", name="memo"),
        CheckConstraint("initiated_by_type IN ('user', 'agent')", name="initiated_by_type"),
        Index("ix_transfers_sender_id_id", "sender_id", "id"),
        Index("ix_transfers_recipient_id_id", "recipient_id", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    sender_id: Mapped[uuid.UUID]
    recipient_id: Mapped[uuid.UUID]
    asset_code: Mapped[str] = mapped_column(Text)
    amount: Mapped[int] = mapped_column(MinorUnits)
    fee: Mapped[int] = mapped_column(MinorUnits)
    status: Mapped[str] = mapped_column(Text)
    entry_id: Mapped[uuid.UUID]
    memo: Mapped[str | None] = mapped_column(Text)
    initiated_by_type: Mapped[str] = mapped_column(Text)
    initiated_by_id: Mapped[uuid.UUID]
    created_at: Mapped[datetime]
