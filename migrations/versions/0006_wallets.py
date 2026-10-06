"""Wallets: which ledger accounts are a user's, per asset.

A row is written once, when the user is provisioned, and never changes: the accounts a
wallet names are the accounts its money is in.

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

from alembic import context, op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = """
CREATE TABLE wallet_accounts (
    -- The user and the two accounts are opaque ids. Users belong to identity and accounts
    -- to the ledger, and modules do not share tables, so there is no foreign key to either.
    user_id               uuid        NOT NULL,
    asset_code            text        NOT NULL,
    available_account_id  uuid        NOT NULL,
    held_account_id       uuid        NOT NULL,
    created_at            timestamptz NOT NULL,
    CONSTRAINT pk_wallet_accounts PRIMARY KEY (user_id, asset_code),
    -- A ledger account is one wallet's at most, which is what lets an account be traced
    -- back to a single owner.
    CONSTRAINT uq_wallet_accounts_available_account_id UNIQUE (available_account_id),
    CONSTRAINT uq_wallet_accounts_held_account_id UNIQUE (held_account_id),
    CONSTRAINT ck_wallet_accounts_distinct_accounts
        CHECK (available_account_id <> held_account_id)
)
"""

# Rows are write-once: the application adds a wallet and reads it, and nothing else.
REVOKES = """
REVOKE UPDATE, DELETE ON wallet_accounts FROM "{app_role}"
"""


def _statements(script: str) -> list[str]:
    return [statement.strip() for statement in script.split(";\n") if statement.strip()]


def upgrade() -> None:
    app_role = context.config.attributes["app_role"]
    for statement in _statements(TABLES):
        op.execute(statement)
    for statement in _statements(REVOKES.format(app_role=app_role)):
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE wallet_accounts")
