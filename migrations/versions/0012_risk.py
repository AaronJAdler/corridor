"""Risk: limits, what was spent against them, reference rates, the deny list and reviews.

Revision ID: 0012
Revises: 0011
"""

from collections.abc import Sequence

from alembic import context, op

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Users, agents and assets belong to other modules. Modules do not share tables, so every
# reference to one of them here is an opaque id or code with no foreign key.
TABLES = """
CREATE TABLE risk_limits (
    id          uuid          NOT NULL,
    scope       text          NOT NULL,
    tier        smallint,
    user_id     uuid,
    agent_id    uuid,
    -- The kind of movement the rule is for. Null means every kind.
    kind        text,
    -- Whole US cents. Null means the rule sets no limit of that sort.
    per_tx_usd  numeric(38,0),
    daily_usd   numeric(38,0),
    created_at  timestamptz   NOT NULL,
    CONSTRAINT pk_risk_limits PRIMARY KEY (id),
    -- One rule per subject and kind, where "every kind" counts as a kind of its own.
    CONSTRAINT uq_risk_limits_subject
        UNIQUE NULLS NOT DISTINCT (scope, tier, user_id, agent_id, kind),
    CONSTRAINT ck_risk_limits_scope CHECK (scope IN ('tier', 'user', 'agent')),
    -- A rule names exactly the subject its scope says it has.
    CONSTRAINT ck_risk_limits_subject CHECK (
        (scope = 'tier' AND tier IS NOT NULL AND user_id IS NULL AND agent_id IS NULL)
        OR (scope = 'user' AND tier IS NULL AND user_id IS NOT NULL AND agent_id IS NULL)
        OR (scope = 'agent' AND tier IS NULL AND user_id IS NULL AND agent_id IS NOT NULL)),
    CONSTRAINT ck_risk_limits_tier CHECK (tier BETWEEN 0 AND 2),
    CONSTRAINT ck_risk_limits_kind CHECK (kind IN ('transfer', 'withdrawal', 'conversion')),
    CONSTRAINT ck_risk_limits_per_tx_usd CHECK (per_tx_usd >= 0),
    CONSTRAINT ck_risk_limits_daily_usd CHECK (daily_usd >= 0)
);

-- The defaults for each KYC tier, in US cents: 1,000 and 2,500 dollars, then 10,000 and
-- 25,000, then 100,000 and 250,000.
INSERT INTO risk_limits (id, scope, tier, user_id, agent_id, kind, per_tx_usd, daily_usd, created_at)
VALUES
    ('01970000-0000-7000-8000-000000000000', 'tier', 0, NULL, NULL, NULL, 100000, 250000, CURRENT_TIMESTAMP),
    ('01970000-0000-7000-8000-000000000001', 'tier', 1, NULL, NULL, NULL, 1000000, 2500000, CURRENT_TIMESTAMP),
    ('01970000-0000-7000-8000-000000000002', 'tier', 2, NULL, NULL, NULL, 10000000, 25000000, CURRENT_TIMESTAMP);

-- One row for each movement that was authorised, written in the movement's own
-- transaction: a movement that is rolled back leaves no usage behind.
CREATE TABLE risk_usage (
    id           uuid          NOT NULL,
    user_id      uuid          NOT NULL,
    -- The agent that asked, when it was not the user themselves.
    agent_id     uuid,
    kind         text          NOT NULL,
    asset        text          NOT NULL,
    amount       numeric(38,0) NOT NULL,
    -- What the amount was worth in whole US cents when it was authorised, rounded up.
    usd_value    numeric(38,0) NOT NULL,
    movement_id  uuid          NOT NULL,
    created_at   timestamptz   NOT NULL,
    -- When the movement ended without moving the money out: a withdrawal that was
    -- canceled, failed or given back. From then on it counts against no limit.
    released_at  timestamptz,
    CONSTRAINT pk_risk_usage PRIMARY KEY (id),
    -- A movement is counted once, however often it is authorised.
    CONSTRAINT uq_risk_usage_kind_movement_id UNIQUE (kind, movement_id),
    CONSTRAINT ck_risk_usage_kind CHECK (kind IN ('transfer', 'withdrawal', 'conversion')),
    CONSTRAINT ck_risk_usage_amount CHECK (amount > 0),
    CONSTRAINT ck_risk_usage_usd_value CHECK (usd_value >= 0)
);

-- The rolling window is read by user and by agent, newest first.
CREATE INDEX ix_risk_usage_user_id_created_at ON risk_usage (user_id, created_at);
CREATE INDEX ix_risk_usage_agent_id_created_at ON risk_usage (agent_id, created_at);

-- What one whole unit of an asset is worth in US dollars, for valuing usage only. It is a
-- reference, not a price: nothing is bought or sold at it.
CREATE TABLE risk_reference_rates (
    asset         text           NOT NULL,
    usd_per_unit  numeric(20,10) NOT NULL,
    updated_at    timestamptz    NOT NULL,
    CONSTRAINT pk_risk_reference_rates PRIMARY KEY (asset),
    CONSTRAINT ck_risk_reference_rates_usd_per_unit CHECK (usd_per_unit > 0)
);

INSERT INTO risk_reference_rates (asset, usd_per_unit, updated_at) VALUES
    ('USD', 1, CURRENT_TIMESTAMP),
    ('USDC', 1, CURRENT_TIMESTAMP),
    ('MXN', 0.058, CURRENT_TIMESTAMP),
    ('BRL', 0.18, CURRENT_TIMESTAMP);

CREATE TABLE risk_denylist (
    id                uuid        NOT NULL,
    kind              text        NOT NULL,
    -- The value as screening compares it: see the normalisation in the risk module.
    value_normalised  text        NOT NULL,
    outcome           text        NOT NULL,
    note              text,
    created_at        timestamptz NOT NULL,
    CONSTRAINT pk_risk_denylist PRIMARY KEY (id),
    CONSTRAINT uq_risk_denylist_kind_value_normalised UNIQUE (kind, value_normalised),
    CONSTRAINT ck_risk_denylist_kind CHECK (kind IN ('name', 'address', 'account')),
    CONSTRAINT ck_risk_denylist_value_normalised CHECK (value_normalised <> ''),
    CONSTRAINT ck_risk_denylist_outcome CHECK (outcome IN ('deny', 'review'))
);

-- Something screening would not let through unseen, waiting for an operator.
CREATE TABLE risk_reviews (
    id            uuid        NOT NULL,
    subject_type  text        NOT NULL,
    subject_id    uuid        NOT NULL,
    -- Whose movement it is, when that is known.
    user_id       uuid,
    -- What screening answered: the reason the review exists.
    outcome       text        NOT NULL,
    status        text        NOT NULL,
    created_at    timestamptz NOT NULL,
    resolved_at   timestamptz,
    CONSTRAINT pk_risk_reviews PRIMARY KEY (id),
    CONSTRAINT uq_risk_reviews_subject_type_subject_id UNIQUE (subject_type, subject_id),
    CONSTRAINT ck_risk_reviews_subject_type CHECK (subject_type IN ('withdrawal', 'deposit')),
    CONSTRAINT ck_risk_reviews_outcome CHECK (outcome IN ('deny', 'review')),
    CONSTRAINT ck_risk_reviews_status CHECK (status IN ('open', 'cleared', 'rejected')),
    -- A review is resolved exactly when it is no longer open.
    CONSTRAINT ck_risk_reviews_resolved CHECK ((status = 'open') = (resolved_at IS NULL))
);

CREATE INDEX ix_risk_reviews_status_id ON risk_reviews (status, id)
"""

# Usage is a record of what was authorised and is never rewritten: the one thing the
# application may write to a row afterwards is when it was given back. Reference rates are
# changed by a migration, not by the application. A review is resolved, never removed.
REVOKES = """
REVOKE UPDATE, DELETE ON risk_usage FROM "{app_role}";
GRANT UPDATE (released_at) ON risk_usage TO "{app_role}";
REVOKE INSERT, UPDATE, DELETE ON risk_reference_rates FROM "{app_role}";
REVOKE DELETE ON risk_reviews FROM "{app_role}"
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
        "DROP TABLE risk_reviews",
        "DROP TABLE risk_denylist",
        "DROP TABLE risk_reference_rates",
        "DROP TABLE risk_usage",
        "DROP TABLE risk_limits",
    ):
        op.execute(statement)
