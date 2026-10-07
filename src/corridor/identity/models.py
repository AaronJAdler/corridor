"""Identity tables. Private to this module: nothing outside ``corridor.identity`` imports them.

The authoritative definition, with grants, is ``migrations/versions/0003_identity.py``, as
``0017_hardening.py`` changed it. A test compares them with these.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, SmallInteger, Text
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
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    tokens_valid_after: Mapped[datetime | None]


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


class LoginLockout(Base):
    """Consecutive failed logins to one address from one client."""

    __tablename__ = "login_lockouts"
    __table_args__ = (
        CheckConstraint("failed_logins > 0", name="failed_logins_positive"),
        Index("ix_login_lockouts_updated_at", "updated_at"),
    )

    email_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    client: Mapped[str] = mapped_column(Text, primary_key=True)
    failed_logins: Mapped[int]
    locked_until: Mapped[datetime | None]
    updated_at: Mapped[datetime]


class LoginThrottle(Base):
    """Recent failed logins to one address from anywhere."""

    __tablename__ = "login_throttles"
    __table_args__ = (
        CheckConstraint("failed_logins > 0", name="failed_logins_positive"),
        Index("ix_login_throttles_last_failed_at", "last_failed_at"),
    )

    email_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    failed_logins: Mapped[int]
    last_failed_at: Mapped[datetime]
