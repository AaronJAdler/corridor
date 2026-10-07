"""FX: quotes and conversions.

Revision ID: 0010
Revises: 0009
"""

from collections.abc import Sequence

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Placeholder: the revision id and its place in the chain are reserved."""


def downgrade() -> None:
    """Placeholder."""
