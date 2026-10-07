"""What a second review of the money paths asked the tables to keep.

- An adjustment says what kind it is. One that takes a deposit out of suspense names the
  deposit, and a release names the user, so that approving it can look at that deposit
  under its lock instead of at an amount.
- A withdrawal keeps who asked for it, as a transfer does, so that an agent can be held to
  canceling its own.
- A reconciliation break keeps the run that last saw it, and the application role changes
  only the columns of a break that a run or a resolution has a reason to.

Revision ID: 0018
Revises: 0017
"""

from collections.abc import Sequence

from alembic import context, op

revision: str = "0018"
down_revision: str | Sequence[str] | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = """
ALTER TABLE ops_adjustments ADD COLUMN kind text;
-- Every adjustment so far was written by hand, or was a suspense adjustment that named an
-- amount and no deposit. Such a one is kept as written by hand, and one that would take
-- money out of suspense can no longer be approved.
UPDATE ops_adjustments SET kind = 'manual';
ALTER TABLE ops_adjustments ALTER COLUMN kind SET NOT NULL;
-- Deposits belong to payments and users to identity, so these are opaque ids.
ALTER TABLE ops_adjustments ADD COLUMN deposit_id uuid;
ALTER TABLE ops_adjustments ADD COLUMN user_id uuid;
ALTER TABLE ops_adjustments ADD CONSTRAINT ck_ops_adjustments_kind
    CHECK (kind IN ('manual', 'suspense_release', 'suspense_return'));
ALTER TABLE ops_adjustments ADD CONSTRAINT ck_ops_adjustments_suspense
    CHECK ((kind = 'manual') = (deposit_id IS NULL)
        AND (kind = 'suspense_release') = (user_id IS NOT NULL));

ALTER TABLE withdrawals ADD COLUMN initiated_by_type text;
ALTER TABLE withdrawals ADD COLUMN initiated_by_id uuid;
-- Who asked for the withdrawals there already are was not kept. They are recorded as
-- their user's own, which leaves each of them the user's to cancel and no agent's.
UPDATE withdrawals SET initiated_by_type = 'user', initiated_by_id = user_id;
ALTER TABLE withdrawals ALTER COLUMN initiated_by_type SET NOT NULL;
ALTER TABLE withdrawals ALTER COLUMN initiated_by_id SET NOT NULL;
ALTER TABLE withdrawals ADD CONSTRAINT ck_withdrawals_initiated_by_type
    CHECK (initiated_by_type IN ('user', 'agent'));

-- The latest run that still saw the break. The run that opened it, to begin with.
ALTER TABLE recon_breaks ADD COLUMN last_seen_run_id uuid;
UPDATE recon_breaks SET last_seen_run_id = run_id;
ALTER TABLE recon_breaks ALTER COLUMN last_seen_run_id SET NOT NULL;
-- Deferred, as the first run's is: a run's row is written when the run ends.
ALTER TABLE recon_breaks ADD CONSTRAINT fk_recon_breaks_last_seen_run_id_recon_runs
    FOREIGN KEY (last_seen_run_id) REFERENCES recon_runs (id) DEFERRABLE INITIALLY DEFERRED
"""

# What a later run changes on a break it sees again, and what resolving it changes. What
# the break is about, and the run that opened it, are fixed when it is written.
GRANTS = """
REVOKE UPDATE ON recon_breaks FROM "{app_role}";
GRANT UPDATE (expected, actual, last_seen_run_id, status, note, resolved_by, resolved_at)
    ON recon_breaks TO "{app_role}"
"""

UNGRANTS = """
REVOKE UPDATE (expected, actual, last_seen_run_id, status, note, resolved_by, resolved_at)
    ON recon_breaks FROM "{app_role}";
GRANT UPDATE ON recon_breaks TO "{app_role}"
"""

UNDO_TABLES = """
ALTER TABLE recon_breaks DROP CONSTRAINT fk_recon_breaks_last_seen_run_id_recon_runs;
ALTER TABLE recon_breaks DROP COLUMN last_seen_run_id;
ALTER TABLE withdrawals DROP CONSTRAINT ck_withdrawals_initiated_by_type;
ALTER TABLE withdrawals DROP COLUMN initiated_by_id;
ALTER TABLE withdrawals DROP COLUMN initiated_by_type;
ALTER TABLE ops_adjustments DROP CONSTRAINT ck_ops_adjustments_suspense;
ALTER TABLE ops_adjustments DROP CONSTRAINT ck_ops_adjustments_kind;
ALTER TABLE ops_adjustments DROP COLUMN user_id;
ALTER TABLE ops_adjustments DROP COLUMN deposit_id;
ALTER TABLE ops_adjustments DROP COLUMN kind
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
