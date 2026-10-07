"""The agent tables. Private to this module: nothing outside ``corridor.agents`` imports them.

The authoritative definitions, with grants, are ``migrations/versions/0015_agents.py``. A
test compares them with these.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from corridor.platform.db import Base


class AgentRow(Base):
    """A row of ``agents``. Named for the row, because ``Agent`` is the frozen value that
    the service hands to other modules."""

    __tablename__ = "agents"
    __table_args__ = (
        CheckConstraint("char_length(name) BETWEEN 1 AND 100", name="name"),
        CheckConstraint("status IN ('active', 'paused', 'revoked')", name="status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(index=True)
    name: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime]


class AgentKeyRow(Base):
    __tablename__ = "agent_keys"
    __table_args__ = (
        CheckConstraint("prefix ~ '^[a-z0-9]{12}$'", name="prefix"),
        CheckConstraint("key_hash ~ '^[0-9a-f]{64}$'", name="key_hash"),
        CheckConstraint("NOT ('*' = ANY (scopes))", name="scopes_never_all"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id"), index=True)
    prefix: Mapped[str] = mapped_column(Text, unique=True)
    key_hash: Mapped[str] = mapped_column(Text)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text))
    expires_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]
    last_used_at: Mapped[datetime | None]
    created_at: Mapped[datetime]
