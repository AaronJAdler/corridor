"""Write-once payment and FX rows, and the purge of quotes nobody took.

Transfers, conversions, beneficiaries and deposit instructions are written once and never
change. The application role already lacks the privilege to change them; the trigger is
the barrier that also stops the owner and anyone holding a broader role by mistake.

A quote that expired unused is working state, as an idempotency key is, and the
application deletes it a day later. A quote that was converted cannot be deleted while
its conversion refers to it.

Revision ID: 0016
Revises: 0015
"""

from collections.abc import Sequence

from alembic import context, op

revision: str = "0016"
down_revision: str | Sequence[str] | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

WRITE_ONCE = ("transfers", "fx_conversions", "beneficiaries", "deposit_instructions")

# Statement-level, as on the ledger's tables, so that it fires even for a statement that
# would match no rows.
TRIGGER = """
CREATE TRIGGER {table}_write_once
    BEFORE UPDATE OR DELETE ON {table}
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation()
"""


def upgrade() -> None:
    app_role = context.config.attributes["app_role"]
    for table in WRITE_ONCE:
        op.execute(TRIGGER.format(table=table).strip())
    op.execute(f'GRANT DELETE ON fx_quotes TO "{app_role}"')


def downgrade() -> None:
    app_role = context.config.attributes["app_role"]
    op.execute(f'REVOKE DELETE ON fx_quotes FROM "{app_role}"')
    for table in reversed(WRITE_ONCE):
        op.execute(f"DROP TRIGGER {table}_write_once ON {table}")
