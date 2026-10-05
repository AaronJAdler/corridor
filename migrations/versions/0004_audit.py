"""Audit: the append-only audit log.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Placeholder: the revision id and its place in the chain are reserved."""


def downgrade() -> None:
    """Placeholder."""
