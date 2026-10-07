"""Operations: adjustments with dual approval.

Revision ID: 0014
Revises: 0013
"""

from collections.abc import Sequence

revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Placeholder: the revision id and its place in the chain are reserved."""


def downgrade() -> None:
    """Placeholder."""
