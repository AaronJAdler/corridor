"""Scheduled-job bookkeeping. Private to this module: nothing outside ``corridor.worker`` imports it.

The authoritative definition is ``migrations/versions/0005_async.py``. A test compares the two.
"""

from datetime import datetime

from sqlalchemy import Text
from sqlalchemy.orm import Mapped, mapped_column

from corridor.platform.db import Base


class JobRun(Base):
    __tablename__ = "job_runs"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    last_started_at: Mapped[datetime]
    last_finished_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
