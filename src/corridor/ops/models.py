"""The operations table. Private to this module: nothing outside ``corridor.ops`` imports it.

The authoritative definition, with grants, is ``migrations/versions/0014_ops.py``. A test
compares the two.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, Index, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from corridor.platform.db import Base


class AdjustmentRow(Base):
    """A row of ``ops_adjustments``. Named for the row, because ``Adjustment`` is the
    frozen value that the service hands to other modules."""

    __tablename__ = "ops_adjustments"
    __table_args__ = (
        UniqueConstraint("entry_id"),
        CheckConstraint("status IN ('pending', 'approved', 'rejected')", name="status"),
        CheckConstraint("requested_by <> approved_by", name="distinct_approver"),
        CheckConstraint(
            "(status = 'approved') = (approved_by IS NOT NULL)"
            " AND (status = 'approved') = (entry_id IS NOT NULL)",
            name="approval",
        ),
        CheckConstraint("(status = 'pending') = (decided_at IS NULL)", name="decided"),
        CheckConstraint("char_length(reason) BETWEEN 1 AND 500", name="reason"),
        Index("ix_ops_adjustments_status_id", "status", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    requested_by: Mapped[uuid.UUID]
    approved_by: Mapped[uuid.UUID | None]
    status: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    legs: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    entry_id: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime]
    decided_at: Mapped[datetime | None]
