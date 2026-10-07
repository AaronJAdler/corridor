"""What the operator's endpoints and a last look at the grants asked the tables to keep.

- The application role changes only the columns of a user that the code changes: the
  standing, the role, the KYC tier and when tokens were ended. A statement that got in
  through the application cannot rewrite an email address or a password hash.
- A withdrawal is in one of the six states the code writes. ``under_review`` and
  ``released`` were allowed and never written: a withdrawal that waits for a review stays
  ``held``, and one that is given back ends ``failed`` or ``canceled``.
- The list of deposits in suspense is read by status, newest first.

Revision ID: 0019
Revises: 0018
"""

from collections.abc import Sequence

from alembic import context, op

revision: str = "0019"
down_revision: str | Sequence[str] | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# A row in a state that is no longer allowed would make the new constraint fail, and the
# migration with it: that is the intended answer to data no code path could have written.
TABLES = """
ALTER TABLE withdrawals DROP CONSTRAINT ck_withdrawals_status;
ALTER TABLE withdrawals ADD CONSTRAINT ck_withdrawals_status
    CHECK (status IN ('held', 'submitting', 'submitted', 'completed', 'failed', 'canceled'));
CREATE INDEX ix_deposits_status_id ON deposits (status, id)
"""

# What identity changes on a user after registration. Who the user is, how they log in and
# when they registered are fixed when the row is written.
GRANTS = """
REVOKE UPDATE ON users FROM "{app_role}";
GRANT UPDATE (role, kyc_tier, status, restricted_reason, tokens_valid_after, updated_at)
    ON users TO "{app_role}"
"""

UNGRANTS = """
REVOKE UPDATE (role, kyc_tier, status, restricted_reason, tokens_valid_after, updated_at)
    ON users FROM "{app_role}";
GRANT UPDATE ON users TO "{app_role}"
"""

UNDO_TABLES = """
DROP INDEX ix_deposits_status_id;
ALTER TABLE withdrawals DROP CONSTRAINT ck_withdrawals_status;
ALTER TABLE withdrawals ADD CONSTRAINT ck_withdrawals_status
    CHECK (status IN ('held', 'under_review', 'submitting', 'submitted', 'completed',
        'failed', 'canceled', 'released'))
"""


def _statements(script: str) -> list[str]:
    return [statement.strip() for statement in script.split(";\n") if statement.strip()]


def upgrade() -> None:
    app_role = context.config.attributes["app_role"]
    for statement in _statements(TABLES):
        op.execute(statement)
    for statement in _statements(GRANTS.format(app_role=app_role)):
        op.execute(statement)


def downgrade() -> None:
    app_role = context.config.attributes["app_role"]
    for statement in _statements(UNGRANTS.format(app_role=app_role)):
        op.execute(statement)
    for statement in _statements(UNDO_TABLES):
        op.execute(statement)
