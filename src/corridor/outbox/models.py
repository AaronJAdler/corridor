"""The outbox table. Private to this module: nothing outside ``corridor.outbox`` imports it.

The authoritative definition, with the notify trigger, is
``migrations/versions/0005_async.py``. A test compares the two.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from corridor.platform.db import Base


class OutboxEventRow(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        CheckConstraint(r"topic ~ '^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$'", name="topic"),
        CheckConstraint("status IN ('pending', 'processing', 'done', 'dead')", name="status"),
        CheckConstraint("attempts >= 0", name="attempts"),
        Index(
            "ix_outbox_events_due",
            "available_at",
            "id",
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "ix_outbox_events_claimed",
            "locked_until",
            postgresql_where=text("status = 'processing'"),
        ),
        Index(
            "uq_outbox_events_topic_dedup_key",
            "topic",
            "dedup_key",
            unique=True,
            postgresql_where=text("dedup_key IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    topic: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer)
    available_at: Mapped[datetime]
    locked_until: Mapped[datetime | None]
    dedup_key: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    context: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
