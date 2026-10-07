"""Risk tables. Private to this module: nothing outside ``corridor.risk`` imports them.

The authoritative definition, with the seeded defaults and grants, is
``migrations/versions/0012_risk.py``. A test compares the two.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Index, Numeric, SmallInteger, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from corridor.platform.db import Base, MinorUnits

_KINDS = "('transfer', 'withdrawal', 'conversion')"


class LimitRow(Base):
    """A row of ``risk_limits``: the limits of one tier, user or agent, for one kind of
    movement or for all of them. Amounts are whole US cents."""

    __tablename__ = "risk_limits"
    __table_args__ = (
        UniqueConstraint(
            "scope",
            "tier",
            "user_id",
            "agent_id",
            "kind",
            name="uq_risk_limits_subject",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint("scope IN ('tier', 'user', 'agent')", name="scope"),
        CheckConstraint(
            "(scope = 'tier' AND tier IS NOT NULL AND user_id IS NULL AND agent_id IS NULL)"
            " OR (scope = 'user' AND tier IS NULL AND user_id IS NOT NULL AND agent_id IS NULL)"
            " OR (scope = 'agent' AND tier IS NULL AND user_id IS NULL AND agent_id IS NOT NULL)",
            name="subject",
        ),
        CheckConstraint("tier BETWEEN 0 AND 2", name="tier"),
        CheckConstraint(f"kind IN {_KINDS}", name="kind"),
        CheckConstraint("per_tx_usd >= 0", name="per_tx_usd"),
        CheckConstraint("daily_usd >= 0", name="daily_usd"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    scope: Mapped[str] = mapped_column(Text)
    tier: Mapped[int | None] = mapped_column(SmallInteger)
    user_id: Mapped[uuid.UUID | None]
    agent_id: Mapped[uuid.UUID | None]
    kind: Mapped[str | None] = mapped_column(Text)
    per_tx_usd: Mapped[int | None] = mapped_column(MinorUnits)
    daily_usd: Mapped[int | None] = mapped_column(MinorUnits)
    created_at: Mapped[datetime]


class UsageRow(Base):
    """A row of ``risk_usage``: one authorised movement and what it was worth."""

    __tablename__ = "risk_usage"
    __table_args__ = (
        UniqueConstraint("kind", "movement_id"),
        CheckConstraint(f"kind IN {_KINDS}", name="kind"),
        CheckConstraint("amount > 0", name="amount"),
        CheckConstraint("usd_value >= 0", name="usd_value"),
        Index("ix_risk_usage_user_id_created_at", "user_id", "created_at"),
        Index("ix_risk_usage_agent_id_created_at", "agent_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID]
    agent_id: Mapped[uuid.UUID | None]
    kind: Mapped[str] = mapped_column(Text)
    asset: Mapped[str] = mapped_column(Text)
    amount: Mapped[int] = mapped_column(MinorUnits)
    usd_value: Mapped[int] = mapped_column(MinorUnits)
    movement_id: Mapped[uuid.UUID]
    created_at: Mapped[datetime]


class ReferenceRateRow(Base):
    """A row of ``risk_reference_rates``: what one whole unit of an asset is worth in USD."""

    __tablename__ = "risk_reference_rates"
    __table_args__ = (CheckConstraint("usd_per_unit > 0", name="usd_per_unit"),)

    asset: Mapped[str] = mapped_column(Text, primary_key=True)
    usd_per_unit: Mapped[Decimal] = mapped_column(Numeric(20, 10))
    updated_at: Mapped[datetime]


class DenylistRow(Base):
    """A row of ``risk_denylist``: a name, address or account that screening stops."""

    __tablename__ = "risk_denylist"
    __table_args__ = (
        UniqueConstraint("kind", "value_normalised"),
        CheckConstraint("kind IN ('name', 'address', 'account')", name="kind"),
        CheckConstraint("value_normalised <> ''", name="value_normalised"),
        CheckConstraint("outcome IN ('deny', 'review')", name="outcome"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(Text)
    value_normalised: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime]


class ReviewRow(Base):
    """A row of ``risk_reviews``: a movement waiting for, or decided by, an operator."""

    __tablename__ = "risk_reviews"
    __table_args__ = (
        UniqueConstraint("subject_type", "subject_id"),
        CheckConstraint("subject_type IN ('withdrawal', 'deposit')", name="subject_type"),
        CheckConstraint("outcome IN ('deny', 'review')", name="outcome"),
        CheckConstraint("status IN ('open', 'cleared', 'rejected')", name="status"),
        CheckConstraint("(status = 'open') = (resolved_at IS NULL)", name="resolved"),
        Index("ix_risk_reviews_status_id", "status", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    subject_type: Mapped[str] = mapped_column(Text)
    subject_id: Mapped[uuid.UUID]
    user_id: Mapped[uuid.UUID | None]
    outcome: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime]
    resolved_at: Mapped[datetime | None]
