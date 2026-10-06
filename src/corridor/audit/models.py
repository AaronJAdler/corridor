"""The audit table. Private to this module: nothing outside ``corridor.audit`` imports it.

The authoritative definition, with its trigger and grants, is
``migrations/versions/0004_audit.py``. A test compares the two.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, Index, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from corridor.platform.db import Base


class AuditEventRow(Base):
    """A row of ``audit_events``. Named for the row, because ``AuditEvent`` is the frozen
    value that the service hands to other modules."""

    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint(
            "actor_type IN ('user', 'agent', 'admin', 'system', 'provider')", name="actor_type"
        ),
        CheckConstraint(r"action ~ '^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$'", name="action"),
        CheckConstraint("outcome IN ('success', 'denied', 'failed')", name="outcome"),
        Index("ix_audit_events_principal_id_occurred_at", "principal_id", "occurred_at"),
        Index("ix_audit_events_resource_type_resource_id", "resource_type", "resource_id"),
        Index("ix_audit_events_action_occurred_at", "action", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    occurred_at: Mapped[datetime]
    actor_type: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[str | None] = mapped_column(Text)
    principal_id: Mapped[uuid.UUID | None]
    action: Mapped[str] = mapped_column(Text)
    resource_type: Mapped[str | None] = mapped_column(Text)
    resource_id: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB)
