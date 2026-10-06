"""The idempotency-key table. Private to ``corridor.api``: nothing else imports it.

The authoritative definition is ``migrations/versions/0007_idempotency.py``. A test
compares the two.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Index, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from corridor.platform.db import Base


class IdempotencyKeyRow(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (Index("ix_idempotency_keys_created_at", "created_at"),)

    actor_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(Text)
    status_code: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[Any | None] = mapped_column(JSONB)
    response_headers: Mapped[dict[str, str] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime]
    completed_at: Mapped[datetime | None]
