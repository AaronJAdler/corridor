"""The wallet table. Private to this module: nothing outside ``corridor.wallets`` imports it.

The authoritative definition, with grants, is ``migrations/versions/0006_wallets.py``.
A test compares the two.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from corridor.platform.db import Base


class WalletAccount(Base):
    __tablename__ = "wallet_accounts"
    __table_args__ = (
        UniqueConstraint("available_account_id"),
        UniqueConstraint("held_account_id"),
        CheckConstraint("available_account_id <> held_account_id", name="distinct_accounts"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    asset_code: Mapped[str] = mapped_column(Text, primary_key=True)
    available_account_id: Mapped[uuid.UUID]
    held_account_id: Mapped[uuid.UUID]
    created_at: Mapped[datetime]
