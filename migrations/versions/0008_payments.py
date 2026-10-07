"""Payments: transfers between users.

A transfer is written once, complete, in the transaction that posts its journal entry, and
never changes. A correction is a new movement, as it is in the ledger.

Revision ID: 0008
Revises: 0007
"""

from collections.abc import Sequence

from alembic import context, op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = """
CREATE TABLE transfers (
    id                 uuid          NOT NULL,
    -- Users belong to identity and journal entries to the ledger. Modules do not share
    -- tables, so these are opaque ids with no foreign key.
    sender_id          uuid          NOT NULL,
    recipient_id       uuid          NOT NULL,
    asset_code         text          NOT NULL,
    -- What the recipient received, in minor units. The sender paid amount + fee.
    amount             numeric(38,0) NOT NULL,
    fee                numeric(38,0) NOT NULL,
    status             text          NOT NULL,
    entry_id           uuid          NOT NULL,
    memo               text,
    -- Who asked for it: the sender, or an agent acting for the sender.
    initiated_by_type  text          NOT NULL,
    initiated_by_id    uuid          NOT NULL,
    created_at         timestamptz   NOT NULL,
    CONSTRAINT pk_transfers PRIMARY KEY (id),
    CONSTRAINT uq_transfers_entry_id UNIQUE (entry_id),
    CONSTRAINT ck_transfers_amount CHECK (amount > 0),
    CONSTRAINT ck_transfers_fee CHECK (fee >= 0),
    CONSTRAINT ck_transfers_status CHECK (status IN ('completed')),
    CONSTRAINT ck_transfers_distinct_parties CHECK (sender_id <> recipient_id),
    CONSTRAINT ck_transfers_memo CHECK (char_length(memo) <= 140),
    CONSTRAINT ck_transfers_initiated_by_type CHECK (initiated_by_type IN ('user', 'agent'))
);

-- A user's transfers are listed newest first from either side, a page at a time, by id.
CREATE INDEX ix_transfers_sender_id_id ON transfers (sender_id, id);

CREATE INDEX ix_transfers_recipient_id_id ON transfers (recipient_id, id)
"""

# Rows are write-once: the application adds a transfer and reads it, and nothing else.
REVOKES = """
REVOKE UPDATE, DELETE ON transfers FROM "{app_role}"
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
    op.execute("DROP TABLE transfers")
