"""Operations: adjustments with dual approval.

An adjustment is a journal entry a person writes by hand, so two people are needed for it:
one asks, another approves, and the database refuses a row that says they were the same.

Revision ID: 0014
Revises: 0013
"""

from collections.abc import Sequence

from alembic import context, op

revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = """
CREATE TABLE ops_adjustments (
    id            uuid        NOT NULL,
    -- Users belong to identity. Modules do not share tables, so these are opaque ids.
    requested_by  uuid        NOT NULL,
    approved_by   uuid,
    status        text        NOT NULL,
    reason        text        NOT NULL,
    -- The postings asked for: [{"account_id", "asset", "direction", "amount"}], the amount
    -- in minor units as a string.
    legs          jsonb       NOT NULL,
    -- The journal entry the approval posted.
    entry_id      uuid,
    created_at    timestamptz NOT NULL,
    decided_at    timestamptz,
    CONSTRAINT pk_ops_adjustments PRIMARY KEY (id),
    CONSTRAINT uq_ops_adjustments_entry_id UNIQUE (entry_id),
    CONSTRAINT ck_ops_adjustments_status CHECK (status IN ('pending', 'approved', 'rejected')),
    -- Nobody approves what they asked for. Null while there is no approver.
    CONSTRAINT ck_ops_adjustments_distinct_approver CHECK (requested_by <> approved_by),
    CONSTRAINT ck_ops_adjustments_approval CHECK (
        (status = 'approved') = (approved_by IS NOT NULL)
        AND (status = 'approved') = (entry_id IS NOT NULL)),
    CONSTRAINT ck_ops_adjustments_decided CHECK ((status = 'pending') = (decided_at IS NULL)),
    CONSTRAINT ck_ops_adjustments_reason CHECK (char_length(reason) BETWEEN 1 AND 500)
);

CREATE INDEX ix_ops_adjustments_status_id ON ops_adjustments (status, id)
"""

# An adjustment is decided and never removed.
REVOKES = """
REVOKE DELETE ON ops_adjustments FROM "{app_role}"
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
    op.execute("DROP TABLE ops_adjustments")
