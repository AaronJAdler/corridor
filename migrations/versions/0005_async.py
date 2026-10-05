"""Async backbone: the outbox and scheduled-job bookkeeping.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Placeholder: the revision id and its place in the chain are reserved."""


def downgrade() -> None:
    """Placeholder."""
