"""Identity tables. Private to this module: nothing outside ``corridor.identity`` imports them.

The authoritative definition, with grants, is ``migrations/versions/0003_identity.py``.
A test compares the two.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, SmallInteger, Text
from sqlalchemy.orm import Mapped, mapped_column

from corridor.platform.db import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("email = lower(email)", name="email_lowercase"),
        CheckConstraint("handle ~ '^[a-z0-9_]{3,30}$'", name="handle"),
        CheckConstraint("role IN ('user', 'admin')", name="role"),
        CheckConstraint("kyc_tier BETWEEN 0 AND 2", name="kyc_tier"),
        CheckConstraint("status IN ('active', 'restricted', 'closed')", name="status"),
        CheckConstraint("failed_logins >= 0", name="failed_logins_not_negative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(Text, unique=True)
    handle: Mapped[str] = mapped_column(Text, unique=True)
    display_name: Mapped[str] = mapped_column(Text)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)
    kyc_tier: Mapped[int] = mapped_column(SmallInteger)
    status: Mapped[str] = mapped_column(Text)
    restricted_reason: Mapped[str | None] = mapped_column(Text)
    failed_logins: Mapped[int]
    locked_until: Mapped[datetime | None]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    family_id: Mapped[uuid.UUID] = mapped_column(index=True)
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    issued_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]
