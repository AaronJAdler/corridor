"""Deposits, deposit instructions, beneficiaries and withdrawals.

Money in and money out. A deposit and a withdrawal are advanced by events that arrive late,
twice and out of order, so each carries its state in a row that is locked while it changes.

Revision ID: 0011
Revises: 0010
"""

from collections.abc import Sequence

from alembic import context, op

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = """
CREATE TABLE deposit_instructions (
    -- Users belong to identity. Modules do not share tables, so this is an opaque id.
    user_id       uuid        NOT NULL,
    asset_code    text        NOT NULL,
    provider      text        NOT NULL,
    -- The provider's id for the virtual account or the address. An incoming deposit names
    -- it, and that is the only thing a deposit is attributed by.
    provider_ref  text        NOT NULL,
    -- What the user is shown: the account to pay into, or the address to send to.
    details       jsonb       NOT NULL,
    created_at    timestamptz NOT NULL,
    CONSTRAINT pk_deposit_instructions PRIMARY KEY (user_id, asset_code),
    CONSTRAINT uq_deposit_instructions_provider_provider_ref UNIQUE (provider, provider_ref)
);

CREATE TABLE deposits (
    id            uuid          NOT NULL,
    -- Null for a deposit nobody could be credited with: its money is in suspense.
    user_id       uuid,
    asset_code    text          NOT NULL,
    amount        numeric(38,0) NOT NULL,
    provider      text          NOT NULL,
    -- The provider's id for the deposit. One provider deposit is one row, however many
    -- times its events are delivered.
    provider_ref  text          NOT NULL,
    kind          text          NOT NULL,
    status        text          NOT NULL,
    -- The journal entry that credited it. Null until, and unless, one is posted.
    entry_id      uuid,
    tx_hash       text,
    created_at    timestamptz   NOT NULL,
    updated_at    timestamptz   NOT NULL,
    CONSTRAINT pk_deposits PRIMARY KEY (id),
    CONSTRAINT uq_deposits_provider_provider_ref UNIQUE (provider, provider_ref),
    CONSTRAINT uq_deposits_entry_id UNIQUE (entry_id),
    CONSTRAINT ck_deposits_amount CHECK (amount > 0),
    CONSTRAINT ck_deposits_kind CHECK (kind IN ('bank', 'chain')),
    CONSTRAINT ck_deposits_status
        CHECK (status IN ('pending', 'completed', 'suspense', 'failed', 'returned'))
);

CREATE INDEX ix_deposits_user_id_id ON deposits (user_id, id);

-- There is no column for an account number: it goes to the provider and is not kept.
CREATE TABLE beneficiaries (
    id            uuid        NOT NULL,
    user_id       uuid        NOT NULL,
    asset_code    text        NOT NULL,
    provider      text        NOT NULL,
    provider_ref  text        NOT NULL,
    holder_name   text        NOT NULL,
    account_mask  text        NOT NULL,
    created_at    timestamptz NOT NULL,
    CONSTRAINT pk_beneficiaries PRIMARY KEY (id),
    CONSTRAINT uq_beneficiaries_provider_provider_ref UNIQUE (provider, provider_ref)
);

CREATE INDEX ix_beneficiaries_user_id_id ON beneficiaries (user_id, id);

CREATE TABLE withdrawals (
    id              uuid          NOT NULL,
    user_id         uuid          NOT NULL,
    asset_code      text          NOT NULL,
    -- What leaves for the beneficiary or the address. The user is debited amount + fee.
    amount          numeric(38,0) NOT NULL,
    fee             numeric(38,0) NOT NULL,
    kind            text          NOT NULL,
    beneficiary_id  uuid,
    to_address      text,
    status          text          NOT NULL,
    provider        text          NOT NULL,
    -- The provider's id for the payout. Null until the provider is known to have one.
    provider_ref    text,
    -- What the provider charged Corridor. Known when the payout settles.
    provider_fee    numeric(38,0),
    failure_reason  text,
    hold_entry_id   uuid          NOT NULL,
    -- The entry that ended the hold: the settlement, or the release.
    final_entry_id  uuid,
    created_at      timestamptz   NOT NULL,
    updated_at      timestamptz   NOT NULL,
    submitted_at    timestamptz,
    CONSTRAINT pk_withdrawals PRIMARY KEY (id),
    CONSTRAINT uq_withdrawals_hold_entry_id UNIQUE (hold_entry_id),
    CONSTRAINT uq_withdrawals_final_entry_id UNIQUE (final_entry_id),
    CONSTRAINT ck_withdrawals_amount CHECK (amount > 0),
    CONSTRAINT ck_withdrawals_fee CHECK (fee >= 0),
    CONSTRAINT ck_withdrawals_provider_fee CHECK (provider_fee >= 0),
    CONSTRAINT ck_withdrawals_kind CHECK (kind IN ('bank', 'chain')),
    -- 'submitting' is written before the provider is asked, so that no payout can exist
    -- for a withdrawal whose user was still able to call it back.
    CONSTRAINT ck_withdrawals_status CHECK (status IN ('held', 'under_review', 'submitting',
        'submitted', 'completed', 'failed', 'canceled', 'released')),
    -- A bank withdrawal goes to a saved beneficiary and an on-chain one to an address.
    CONSTRAINT ck_withdrawals_target CHECK (
        (kind = 'bank' AND beneficiary_id IS NOT NULL AND to_address IS NULL)
        OR (kind = 'chain' AND beneficiary_id IS NULL AND to_address IS NOT NULL))
);

CREATE INDEX ix_withdrawals_user_id_id ON withdrawals (user_id, id);

-- The sweeper reads the few that are still in flight, oldest first.
CREATE INDEX ix_withdrawals_status_id ON withdrawals (status, id)
"""

# An instruction and a beneficiary are written once. A deposit and a withdrawal change
# state and are never removed.
REVOKES = """
REVOKE UPDATE, DELETE ON deposit_instructions, beneficiaries FROM "{app_role}";
REVOKE DELETE ON deposits, withdrawals FROM "{app_role}"
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
    for statement in (
        "DROP TABLE withdrawals",
        "DROP TABLE beneficiaries",
        "DROP TABLE deposits",
        "DROP TABLE deposit_instructions",
    ):
        op.execute(statement)
