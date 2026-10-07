"""Deposits, deposit instructions, beneficiaries and withdrawals.

Revision ID: 0011
Revises: 0010
"""

from collections.abc import Sequence

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Placeholder: the revision id and its place in the chain are reserved."""


def downgrade() -> None:
    """Placeholder."""
