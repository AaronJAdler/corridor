"""FX: quotes and conversions.

A quote is written with both of its amounts and changes once, from open to used, in the
transaction that converts it. A conversion is written once and never changes.

Revision ID: 0010
Revises: 0009
"""

from collections.abc import Sequence

from alembic import context, op

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = """
CREATE TABLE fx_quotes (
    id           uuid          NOT NULL,
    -- Users belong to identity and assets to the ledger. Modules do not share tables, so
    -- these are opaque values with no foreign key.
    user_id      uuid          NOT NULL,
    sell_asset   text          NOT NULL,
    buy_asset    text          NOT NULL,
    -- Minor units of each asset. A conversion moves exactly these and works nothing out.
    sell_amount  numeric(38,0) NOT NULL,
    buy_amount   numeric(38,0) NOT NULL,
    -- Units of the buy asset for one unit of the sell asset, as decimal strings: the rate
    -- the customer was given, and the mid-market rate it was made from. Kept as text so
    -- that every digit is what was quoted; nothing is computed from them again.
    rate         text          NOT NULL,
    mid          text          NOT NULL,
    status       text          NOT NULL,
    expires_at   timestamptz   NOT NULL,
    created_at   timestamptz   NOT NULL,
    CONSTRAINT pk_fx_quotes PRIMARY KEY (id),
    CONSTRAINT ck_fx_quotes_sell_amount CHECK (sell_amount > 0),
    CONSTRAINT ck_fx_quotes_buy_amount CHECK (buy_amount > 0),
    CONSTRAINT ck_fx_quotes_distinct_assets CHECK (sell_asset <> buy_asset),
    CONSTRAINT ck_fx_quotes_rate CHECK (rate ~ '^[0-9]+(\\.[0-9]+)?$'),
    CONSTRAINT ck_fx_quotes_mid CHECK (mid ~ '^[0-9]+(\\.[0-9]+)?$'),
    CONSTRAINT ck_fx_quotes_status CHECK (status IN ('open', 'used')),
    CONSTRAINT ck_fx_quotes_expires_at CHECK (expires_at > created_at)
);

CREATE TABLE fx_conversions (
    id          uuid        NOT NULL,
    quote_id    uuid        NOT NULL,
    user_id     uuid        NOT NULL,
    entry_id    uuid        NOT NULL,
    created_at  timestamptz NOT NULL,
    CONSTRAINT pk_fx_conversions PRIMARY KEY (id),
    -- A quote converts once, whatever the application believes about its status.
    CONSTRAINT uq_fx_conversions_quote_id UNIQUE (quote_id),
    CONSTRAINT uq_fx_conversions_entry_id UNIQUE (entry_id),
    CONSTRAINT fk_fx_conversions_quote_id_fx_quotes
        FOREIGN KEY (quote_id) REFERENCES fx_quotes (id)
)
"""

# A quote is added, read and marked used, and never removed. A conversion is write-once.
REVOKES = """
REVOKE DELETE ON fx_quotes FROM "{app_role}";
REVOKE UPDATE, DELETE ON fx_conversions FROM "{app_role}"
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
    for statement in ("DROP TABLE fx_conversions", "DROP TABLE fx_quotes"):
        op.execute(statement)
