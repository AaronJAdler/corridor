"""The reconciliation tables. Private to this module: nothing outside ``corridor.recon``
imports them.

The authoritative definition, with grants, is ``migrations/versions/0013_recon.py`` and
``0018_review_c.py``. A test
compares the two.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from corridor.platform.db import Base, MinorUnits


class ReconRunRow(Base):
    __tablename__ = "recon_runs"
    __table_args__ = (
        CheckConstraint("status IN ('completed', 'incomplete')", name="status"),
        CheckConstraint("window_start < window_end", name="window"),
        CheckConstraint("breaks_opened >= 0 AND breaks_found >= breaks_opened", name="counts"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    window_start: Mapped[datetime]
    window_end: Mapped[datetime]
    status: Mapped[str] = mapped_column(Text)
    breaks_found: Mapped[int] = mapped_column(Integer)
    breaks_opened: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime]


class ReconBreakRow(Base):
    __tablename__ = "recon_breaks"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('missing_deposit', 'unknown_deposit', 'amount_mismatch',"
            " 'missing_payout_result', 'unknown_payout', 'settlement_balance')",
            name="kind",
        ),
        CheckConstraint("status IN ('open', 'resolved')", name="status"),
        CheckConstraint(
            "(status = 'resolved') = (resolved_by IS NOT NULL)"
            " AND (status = 'resolved') = (resolved_at IS NOT NULL)",
            name="resolution",
        ),
        CheckConstraint("char_length(note) <= 500", name="note"),
        Index(
            "uq_recon_breaks_open",
            "kind",
            "provider",
            "provider_ref",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
        Index("ix_recon_breaks_status_id", "status", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recon_runs.id", deferrable=True, initially="DEFERRED"), index=True
    )
    last_seen_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recon_runs.id", deferrable=True, initially="DEFERRED")
    )
    kind: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    provider_ref: Mapped[str] = mapped_column(Text)
    asset_code: Mapped[str] = mapped_column(Text)
    expected: Mapped[int | None] = mapped_column(MinorUnits)
    actual: Mapped[int | None] = mapped_column(MinorUnits)
    status: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    resolved_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime]
    resolved_at: Mapped[datetime | None]
