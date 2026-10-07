"""The FX tables. Private to this module: nothing outside ``corridor.fx`` imports them.

The authoritative definitions, with grants, are ``migrations/versions/0010_fx.py``. A test
compares them with these.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from corridor.platform.db import Base, MinorUnits

# A plain decimal number as text: no sign, no exponent. The migration states the same.
_DECIMAL = "'^[0-9]+(\\.[0-9]+)?$'"


class QuoteRow(Base):
    """A row of ``fx_quotes``. Named for the row, because ``Quote`` is the frozen value
    that the service hands to other modules."""

    __tablename__ = "fx_quotes"
    __table_args__ = (
        CheckConstraint("sell_amount > 0", name="sell_amount"),
        CheckConstraint("buy_amount > 0", name="buy_amount"),
        CheckConstraint("sell_asset <> buy_asset", name="distinct_assets"),
        CheckConstraint(f"rate ~ {_DECIMAL}", name="rate"),
        CheckConstraint(f"mid ~ {_DECIMAL}", name="mid"),
        CheckConstraint("status IN ('open', 'used')", name="status"),
        CheckConstraint("expires_at > created_at", name="expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID]
    sell_asset: Mapped[str] = mapped_column(Text)
    buy_asset: Mapped[str] = mapped_column(Text)
    sell_amount: Mapped[int] = mapped_column(MinorUnits)
    buy_amount: Mapped[int] = mapped_column(MinorUnits)
    rate: Mapped[str] = mapped_column(Text)
    mid: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime]
    created_at: Mapped[datetime]


class ConversionRow(Base):
    __tablename__ = "fx_conversions"
    __table_args__ = (UniqueConstraint("quote_id"), UniqueConstraint("entry_id"))

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    quote_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("fx_quotes.id"))
    user_id: Mapped[uuid.UUID]
    entry_id: Mapped[uuid.UUID]
    created_at: Mapped[datetime]
