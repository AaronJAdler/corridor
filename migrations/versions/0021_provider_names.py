"""A provider's name is its adapter's, so the webhook table allows every provider's name.

The check constraint on ``webhook_events.provider`` spelled out the two simulators. A bank
rail with another name, the Increase sandbox first, could not have an event stored. The
constraint now lists every name in ``corridor.webhooks.Provider``; a test compares the two so
that a provider added to one cannot be missed in the other.

Nothing else carries a provider's name as a constraint: deposits, withdrawals, deposit
instructions, beneficiaries and ledger accounts hold it as text and are scoped by it, so a
second bank rail has settlement accounts, deposits and instructions of its own and shares
nothing with the first.

Revision ID: 0021
Revises: 0020
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0021"
down_revision: str | Sequence[str] | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = """
ALTER TABLE webhook_events DROP CONSTRAINT ck_webhook_events_provider;
ALTER TABLE webhook_events ADD CONSTRAINT ck_webhook_events_provider
    CHECK (provider IN ('simbank', 'simcustody', 'increase'))
"""

# An event stored for the new provider would make the old constraint fail, and the downgrade
# with it: that is intended. Such an event is removed by hand, as a person who has decided
# that what it recorded can go, before the code that reads it is taken away.
DOWNGRADE = """
ALTER TABLE webhook_events DROP CONSTRAINT ck_webhook_events_provider;
ALTER TABLE webhook_events ADD CONSTRAINT ck_webhook_events_provider
    CHECK (provider IN ('simbank', 'simcustody'))
"""


def _statements(script: str) -> list[str]:
    return [statement.strip() for statement in script.split(";\n") if statement.strip()]


def upgrade() -> None:
    for statement in _statements(UPGRADE):
        op.execute(statement)


def downgrade() -> None:
    for statement in _statements(DOWNGRADE):
        op.execute(statement)
