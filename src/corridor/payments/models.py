"""The payments tables. Private to this module: nothing outside ``corridor.payments`` imports them.

The authoritative definitions, with grants, are ``migrations/versions/0008_payments.py``
(transfers), ``0011_money_flows.py`` (everything else) and ``0018_review_c.py`` (who asked
for a withdrawal). A test compares them with these.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, Index, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
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


class DepositInstructionRow(Base):
    """Where one user sends one asset: a virtual bank account or a deposit address."""

    __tablename__ = "deposit_instructions"
    __table_args__ = (UniqueConstraint("provider", "provider_ref"),)

    user_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    asset_code: Mapped[str] = mapped_column(Text, primary_key=True)
    provider: Mapped[str] = mapped_column(Text)
    provider_ref: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime]


class DepositRow(Base):
    __tablename__ = "deposits"
    __table_args__ = (
        UniqueConstraint("provider", "provider_ref"),
        UniqueConstraint("entry_id"),
        CheckConstraint("amount > 0", name="amount"),
        CheckConstraint("kind IN ('bank', 'chain')", name="kind"),
        CheckConstraint(
            "status IN ('pending', 'completed', 'suspense', 'failed', 'returned')", name="status"
        ),
        Index("ix_deposits_user_id_id", "user_id", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID | None]
    asset_code: Mapped[str] = mapped_column(Text)
    amount: Mapped[int] = mapped_column(MinorUnits)
    provider: Mapped[str] = mapped_column(Text)
    provider_ref: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    entry_id: Mapped[uuid.UUID | None]
    tx_hash: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class BeneficiaryRow(Base):
    """An external bank account, as the provider's token and a masked value. There is no
    column an account number could be put in."""

    __tablename__ = "beneficiaries"
    __table_args__ = (
        UniqueConstraint("provider", "provider_ref"),
        Index("ix_beneficiaries_user_id_id", "user_id", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID]
    asset_code: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    provider_ref: Mapped[str] = mapped_column(Text)
    holder_name: Mapped[str] = mapped_column(Text)
    account_mask: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime]


class WithdrawalRow(Base):
    __tablename__ = "withdrawals"
    __table_args__ = (
        UniqueConstraint("hold_entry_id"),
        UniqueConstraint("final_entry_id"),
        CheckConstraint("amount > 0", name="amount"),
        CheckConstraint("fee >= 0", name="fee"),
        CheckConstraint("provider_fee >= 0", name="provider_fee"),
        CheckConstraint("kind IN ('bank', 'chain')", name="kind"),
        CheckConstraint(
            "status IN ('held', 'under_review', 'submitting', 'submitted', 'completed',"
            " 'failed', 'canceled', 'released')",
            name="status",
        ),
        CheckConstraint(
            "(kind = 'bank' AND beneficiary_id IS NOT NULL AND to_address IS NULL)"
            " OR (kind = 'chain' AND beneficiary_id IS NULL AND to_address IS NOT NULL)",
            name="target",
        ),
        CheckConstraint("initiated_by_type IN ('user', 'agent')", name="initiated_by_type"),
        Index("ix_withdrawals_user_id_id", "user_id", "id"),
        Index("ix_withdrawals_status_id", "status", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID]
    asset_code: Mapped[str] = mapped_column(Text)
    amount: Mapped[int] = mapped_column(MinorUnits)
    fee: Mapped[int] = mapped_column(MinorUnits)
    kind: Mapped[str] = mapped_column(Text)
    beneficiary_id: Mapped[uuid.UUID | None]
    to_address: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    provider_ref: Mapped[str | None] = mapped_column(Text)
    provider_fee: Mapped[int | None] = mapped_column(MinorUnits)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    hold_entry_id: Mapped[uuid.UUID]
    final_entry_id: Mapped[uuid.UUID | None]
    initiated_by_type: Mapped[str] = mapped_column(Text)
    initiated_by_id: Mapped[uuid.UUID]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    submitted_at: Mapped[datetime | None]
