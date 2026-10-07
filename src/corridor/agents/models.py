"""The agent tables. Private to this module: nothing outside ``corridor.agents`` imports them.

The authoritative definitions, with grants, are ``migrations/versions/0015_agents.py``. A
test compares them with these.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from corridor.platform.db import Base, MinorUnits


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


class AgentPolicyRow(Base):
    """What an agent may spend, as its owner set it. Amounts are whole US cents."""

    __tablename__ = "agent_policies"
    __table_args__ = (
        CheckConstraint("per_tx_usd >= 0", name="per_tx_usd"),
        CheckConstraint("daily_usd >= 0", name="daily_usd"),
        CheckConstraint("approval_threshold_usd >= 0", name="approval_threshold_usd"),
    )

    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id"), primary_key=True)
    per_tx_usd: Mapped[int | None] = mapped_column(MinorUnits)
    daily_usd: Mapped[int | None] = mapped_column(MinorUnits)
    approval_threshold_usd: Mapped[int | None] = mapped_column(MinorUnits)
    any_recipient: Mapped[bool]
    updated_at: Mapped[datetime]


class AgentAllowedRecipientRow(Base):
    __tablename__ = "agent_allowed_recipients"
    __table_args__ = (CheckConstraint("kind IN ('user', 'beneficiary')", name="kind"),)

    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_policies.agent_id"), primary_key=True
    )
    kind: Mapped[str] = mapped_column(Text, primary_key=True)
    target_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)


class AgentApprovalRequestRow(Base):
    __tablename__ = "agent_approval_requests"
    __table_args__ = (
        CheckConstraint("kind IN ('transfer', 'withdrawal')", name="kind"),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'expired', 'executed', 'failed')",
            name="status",
        ),
        CheckConstraint("jsonb_typeof(request) = 'object'", name="request"),
        CheckConstraint("(status = 'pending') = (decided_at IS NULL)", name="decided"),
        CheckConstraint("(status = 'failed') = (failure_code IS NOT NULL)", name="failure"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id"))
    owner_user_id: Mapped[uuid.UUID] = mapped_column(index=True)
    kind: Mapped[str] = mapped_column(Text)
    request: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text)
    movement_id: Mapped[uuid.UUID] = mapped_column(unique=True)
    failure_code: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime]
    decided_at: Mapped[datetime | None]
    created_at: Mapped[datetime]
