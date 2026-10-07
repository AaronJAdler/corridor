"""What the final review asked the database to keep for itself.

- A ledger account is written once. Its kind, its normal side and its owner give every
  posting on it its meaning, so the table gets the guard the journal has: no ``UPDATE``, no
  ``DELETE`` and no ``TRUNCATE``, for the owner as well as the application.
- An account's category and normal side are the ones the chart of accounts gives its kind
  (``corridor.ledger.types.CHART``). Until now only the application said so.
- Reconciliation has a kind of break for a deposit the provider's statement shows as
  recalled while it is still credited here.

Revision ID: 0020
Revises: 0019
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0020"
down_revision: str | Sequence[str] | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# An account that does not follow the chart would make the new constraint fail, and the
# migration with it: that is the intended answer to a row no code path could have written.
UPGRADE = """
CREATE TRIGGER ledger_accounts_append_only
    BEFORE UPDATE OR DELETE OR TRUNCATE ON ledger_accounts
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

ALTER TABLE ledger_accounts ADD CONSTRAINT ck_ledger_accounts_chart CHECK (
    (kind IN ('user_available', 'user_held', 'suspense')
        AND category = 'liability' AND normal_side = 'C')
    OR (kind IN ('user_receivable', 'bank_settlement', 'custody_omnibus', 'fx_position')
        AND category = 'asset' AND normal_side = 'D')
    OR (kind = 'fee_revenue' AND category = 'revenue' AND normal_side = 'C')
    OR (kind = 'provider_fee_expense' AND category = 'expense' AND normal_side = 'D')
);

ALTER TABLE recon_breaks DROP CONSTRAINT ck_recon_breaks_kind;
ALTER TABLE recon_breaks ADD CONSTRAINT ck_recon_breaks_kind CHECK (kind IN ('missing_deposit',
    'unknown_deposit', 'amount_mismatch', 'missing_payout_result', 'unknown_payout',
    'settlement_balance', 'missing_return'))
"""

# A break of the new kind would make the old constraint fail, and the downgrade with it:
# such a break is resolved, or removed by hand, before the code that reads it is taken away.
DOWNGRADE = """
ALTER TABLE recon_breaks DROP CONSTRAINT ck_recon_breaks_kind;
ALTER TABLE recon_breaks ADD CONSTRAINT ck_recon_breaks_kind CHECK (kind IN ('missing_deposit',
    'unknown_deposit', 'amount_mismatch', 'missing_payout_result', 'unknown_payout',
    'settlement_balance'));
ALTER TABLE ledger_accounts DROP CONSTRAINT ck_ledger_accounts_chart;
DROP TRIGGER ledger_accounts_append_only ON ledger_accounts
"""


def _statements(script: str) -> list[str]:
    return [statement.strip() for statement in script.split(";\n") if statement.strip()]


def upgrade() -> None:
    for statement in _statements(UPGRADE):
        op.execute(statement)


def downgrade() -> None:
    for statement in _statements(DOWNGRADE):
        op.execute(statement)
