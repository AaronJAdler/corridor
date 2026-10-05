"""Ledger tables. Private to this module: nothing outside ``corridor.ledger`` imports them.

The authoritative definition, with triggers and grants, is ``migrations/versions/0002_ledger.py``.
A test compares the two.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    SmallInteger,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from corridor.platform.db import Base, MinorUnits


class Asset(Base):
    __tablename__ = "assets"
    __table_args__ = (
        CheckConstraint("kind IN ('fiat', 'stablecoin')", name="kind"),
        CheckConstraint("decimals BETWEEN 0 AND 18", name="decimals"),
    )

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text)
    decimals: Mapped[int] = mapped_column(SmallInteger)


class LedgerAccount(Base):
    __tablename__ = "ledger_accounts"
    __table_args__ = (
        UniqueConstraint(
            "kind",
            "asset_code",
            "owner_id",
            "provider",
            name="uq_ledger_accounts_identity",
            postgresql_nulls_not_distinct=True,
        ),
        UniqueConstraint("id", "asset_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    asset_code: Mapped[str] = mapped_column(Text, ForeignKey("assets.code"))
    kind: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text)
    normal_side: Mapped[str] = mapped_column(Text)
    owner_id: Mapped[uuid.UUID | None]
    provider: Mapped[str | None] = mapped_column(Text)
    is_constrained: Mapped[bool]
    created_at: Mapped[datetime]


class AccountBalance(Base):
    __tablename__ = "account_balances"
    __table_args__ = (CheckConstraint("balance >= 0", name="not_negative"),)

    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ledger_accounts.id"), primary_key=True
    )
    balance: Mapped[int] = mapped_column(MinorUnits)
    last_posting_seq: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[datetime]


class JournalEntry(Base):
    __tablename__ = "journal_entries"
    __table_args__ = (
        UniqueConstraint("source_type", "source_id", "kind", name="uq_journal_entries_source"),
        UniqueConstraint("reverses_entry_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(Text)
    source_id: Mapped[str] = mapped_column(Text)
    # "metadata" is taken by SQLAlchemy's declarative base, hence the attribute name.
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB)
    reverses_entry_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("journal_entries.id"))
    posted_at: Mapped[datetime]


class Posting(Base):
    __tablename__ = "postings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id", "asset_code"], ["ledger_accounts.id", "ledger_accounts.asset_code"]
        ),
        Index("ix_postings_account_id_seq", "account_id", "seq"),
    )

    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    entry_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("journal_entries.id"), index=True)
    account_id: Mapped[uuid.UUID]
    asset_code: Mapped[str] = mapped_column(Text)
    direction: Mapped[str] = mapped_column(Text)
    amount: Mapped[int] = mapped_column(MinorUnits)
    balance_after: Mapped[int | None] = mapped_column(MinorUnits)
