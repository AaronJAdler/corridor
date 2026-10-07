"""The webhook table. Private to this module: nothing outside ``corridor.webhooks`` imports it.

The authoritative definition, with its grants, is ``migrations/versions/0009_webhooks.py``,
as ``0017_hardening.py`` changed it. A test compares them with this.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, Index, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from corridor.platform.db import Base


class WebhookEventRow(Base):
    __tablename__ = "webhook_events"
    __table_args__ = (
        UniqueConstraint("provider", "event_id"),
        CheckConstraint("provider IN ('simbank', 'simcustody')", name="provider"),
        CheckConstraint("event_id <> ''", name="event_id"),
        CheckConstraint("type <> ''", name="type"),
        CheckConstraint("outcome IN ('processed', 'ignored')", name="outcome"),
        CheckConstraint("(processed_at IS NULL) = (outcome IS NULL)", name="processed"),
        Index(
            "ix_webhook_events_processed_at_unredacted",
            "processed_at",
            postgresql_where=text("redacted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(Text)
    event_id: Mapped[str] = mapped_column(Text)
    type: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    received_at: Mapped[datetime]
    processed_at: Mapped[datetime | None]
    outcome: Mapped[str | None] = mapped_column(Text)
    redacted_at: Mapped[datetime | None]
