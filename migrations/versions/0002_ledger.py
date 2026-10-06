"""Ledger: assets, accounts, cached balances, journal entries and postings.

The rules that keep money conserved are enforced here, in the database, as well as in the
application: an entry must balance, history cannot be edited, and a constrained balance
cannot go below zero.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

from alembic import context, op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = """
CREATE TABLE assets (
    code      text     NOT NULL,
    kind      text     NOT NULL,
    decimals  smallint NOT NULL,
    CONSTRAINT pk_assets PRIMARY KEY (code),
    CONSTRAINT ck_assets_kind CHECK (kind IN ('fiat', 'stablecoin')),
    CONSTRAINT ck_assets_decimals CHECK (decimals BETWEEN 0 AND 18)
);

INSERT INTO assets (code, kind, decimals) VALUES
    ('USD', 'fiat', 2),
    ('MXN', 'fiat', 2),
    ('BRL', 'fiat', 2),
    ('USDC', 'stablecoin', 6);

CREATE TABLE ledger_accounts (
    id              uuid        NOT NULL,
    asset_code      text        NOT NULL,
    kind            text        NOT NULL,
    category        text        NOT NULL,
    normal_side     text        NOT NULL,
    -- The owner is an opaque id. The ledger knows nothing about users, so there is no
    -- foreign key out of it.
    owner_id        uuid,
    provider        text,
    is_constrained  boolean     NOT NULL,
    created_at      timestamptz NOT NULL,
    CONSTRAINT pk_ledger_accounts PRIMARY KEY (id),
    CONSTRAINT fk_ledger_accounts_asset_code_assets FOREIGN KEY (asset_code) REFERENCES assets (code),
    -- One account per kind, asset, owner and provider, where an absent owner or provider
    -- counts as a value: there is exactly one fee_revenue account for USD.
    CONSTRAINT uq_ledger_accounts_identity
        UNIQUE NULLS NOT DISTINCT (kind, asset_code, owner_id, provider),
    -- The target of the composite foreign key that ties a posting's asset to its account's.
    CONSTRAINT uq_ledger_accounts_id_asset_code UNIQUE (id, asset_code),
    CONSTRAINT ck_ledger_accounts_category
        CHECK (category IN ('asset', 'liability', 'revenue', 'expense')),
    CONSTRAINT ck_ledger_accounts_normal_side CHECK (normal_side IN ('D', 'C')),
    CONSTRAINT ck_ledger_accounts_kind CHECK (
        (kind IN ('user_available', 'user_held', 'user_receivable')
            AND owner_id IS NOT NULL AND provider IS NULL AND is_constrained)
        OR (kind IN ('bank_settlement', 'custody_omnibus')
            AND owner_id IS NULL AND provider IS NOT NULL AND NOT is_constrained)
        OR (kind IN ('suspense', 'fx_position', 'fee_revenue', 'provider_fee_expense')
            AND owner_id IS NULL AND provider IS NULL AND NOT is_constrained)
    )
);

-- Cached balances, for constrained accounts only. Posting locks the row, checks the result
-- and updates it. Accounts that every transaction touches have no row here, so they never
-- become a lock that every transaction queues behind.
CREATE TABLE account_balances (
    account_id        uuid          NOT NULL,
    balance           numeric(38,0) NOT NULL,
    last_posting_seq  bigint        NOT NULL,
    updated_at        timestamptz   NOT NULL,
    CONSTRAINT pk_account_balances PRIMARY KEY (account_id),
    CONSTRAINT fk_account_balances_account_id_ledger_accounts
        FOREIGN KEY (account_id) REFERENCES ledger_accounts (id),
    CONSTRAINT ck_account_balances_not_negative CHECK (balance >= 0)
);

CREATE TABLE journal_entries (
    id                 uuid        NOT NULL,
    kind               text        NOT NULL,
    source_type        text        NOT NULL,
    source_id          text        NOT NULL,
    metadata           jsonb       NOT NULL,
    reverses_entry_id  uuid,
    posted_at          timestamptz NOT NULL,
    CONSTRAINT pk_journal_entries PRIMARY KEY (id),
    -- One business event posts at most once, however many times its handler runs.
    CONSTRAINT uq_journal_entries_source UNIQUE (source_type, source_id, kind),
    -- An entry is reversed at most once.
    CONSTRAINT uq_journal_entries_reverses_entry_id UNIQUE (reverses_entry_id),
    CONSTRAINT fk_journal_entries_reverses_entry_id_journal_entries
        FOREIGN KEY (reverses_entry_id) REFERENCES journal_entries (id),
    CONSTRAINT ck_journal_entries_kind CHECK (kind ~ '^[a-z][a-z_]{0,39}$'),
    CONSTRAINT ck_journal_entries_source CHECK (source_type <> '' AND source_id <> '')
);

CREATE TABLE postings (
    -- A global order. For a constrained account it is also the order of commits, because
    -- the balance row is locked from before the insert until the commit.
    seq            bigint GENERATED ALWAYS AS IDENTITY,
    entry_id       uuid          NOT NULL,
    account_id     uuid          NOT NULL,
    asset_code     text          NOT NULL,
    direction      text          NOT NULL,
    amount         numeric(38,0) NOT NULL,
    -- The account's balance after this posting. Set for constrained accounts only.
    balance_after  numeric(38,0),
    CONSTRAINT pk_postings PRIMARY KEY (seq),
    CONSTRAINT fk_postings_entry_id_journal_entries
        FOREIGN KEY (entry_id) REFERENCES journal_entries (id),
    -- Composite, so a posting can never name an asset other than its account's.
    CONSTRAINT fk_postings_account_id_asset_code_ledger_accounts
        FOREIGN KEY (account_id, asset_code) REFERENCES ledger_accounts (id, asset_code),
    CONSTRAINT ck_postings_direction CHECK (direction IN ('D', 'C')),
    CONSTRAINT ck_postings_amount_positive CHECK (amount > 0),
    CONSTRAINT ck_postings_balance_after_not_negative CHECK (balance_after >= 0)
);

CREATE INDEX ix_postings_entry_id ON postings (entry_id);
CREATE INDEX ix_postings_account_id_seq ON postings (account_id, seq)
"""

# The search path is pinned with the temporary schema last. Left to the session's default,
# a temporary table named "postings" would be found first and the check would read that
# instead of the ledger.
CHECK_ENTRY = """
CREATE FUNCTION ledger_check_entry() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    target uuid;
    posting_count bigint;
BEGIN
    IF TG_TABLE_NAME = 'journal_entries' THEN
        target := NEW.id;
    ELSE
        target := NEW.entry_id;
    END IF;

    SELECT count(*) INTO posting_count FROM postings WHERE entry_id = target;
    IF posting_count < 2 THEN
        RAISE EXCEPTION 'journal entry % has % posting(s); an entry needs at least two',
            target, posting_count USING ERRCODE = 'CR002';
    END IF;

    PERFORM 1
       FROM postings
      WHERE entry_id = target
      GROUP BY asset_code
     HAVING sum(CASE direction WHEN 'D' THEN amount ELSE -amount END) <> 0;
    IF FOUND THEN
        RAISE EXCEPTION 'journal entry % does not balance: debits and credits differ', target
            USING ERRCODE = 'CR003';
    END IF;

    RETURN NULL;
END
$$
"""

TRIGGERS = """
-- Deferred to commit, when the entry and all of its postings exist. A posting added to an
-- old entry by a later transaction is caught the same way.
CREATE CONSTRAINT TRIGGER journal_entries_balanced
    AFTER INSERT ON journal_entries
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION ledger_check_entry();

CREATE CONSTRAINT TRIGGER postings_balanced
    AFTER INSERT ON postings
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION ledger_check_entry();

-- Statement-level, so they fire even for a statement that would match no rows, and for
-- TRUNCATE, which row-level triggers never see.
CREATE TRIGGER journal_entries_append_only
    BEFORE UPDATE OR DELETE OR TRUNCATE ON journal_entries
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

CREATE TRIGGER postings_append_only
    BEFORE UPDATE OR DELETE OR TRUNCATE ON postings
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation()
"""

# What is left to the application role after the baseline's default of full row access:
# it may add accounts, entries and postings and maintain cached balances, and nothing else.
REVOKES = """
REVOKE UPDATE, DELETE ON journal_entries, postings, ledger_accounts FROM "{app_role}";
REVOKE INSERT, UPDATE, DELETE ON assets FROM "{app_role}";
REVOKE DELETE ON account_balances FROM "{app_role}"
"""


def _statements(script: str) -> list[str]:
    return [statement.strip() for statement in script.split(";\n") if statement.strip()]


def upgrade() -> None:
    app_role = context.config.attributes["app_role"]
    for statement in _statements(TABLES):
        op.execute(statement)
    op.execute(CHECK_ENTRY)
    for statement in _statements(TRIGGERS):
        op.execute(statement)
    for statement in _statements(REVOKES.format(app_role=app_role)):
        op.execute(statement)


def downgrade() -> None:
    for statement in (
        "DROP TABLE postings",
        "DROP TABLE journal_entries",
        "DROP TABLE account_balances",
        "DROP TABLE ledger_accounts",
        "DROP TABLE assets",
        "DROP FUNCTION ledger_check_entry()",
    ):
        op.execute(statement)
