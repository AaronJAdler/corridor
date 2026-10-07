"""Agents: the principals a user lets act on their wallet, the keys they act with, what
each may spend, and what it asked to spend beyond that.

An agent changes in one way, its status. A key changes in two: it is revoked, and the time
it was last used moves on. Nothing else about either is ever rewritten, and neither is
ever removed.

A policy is its owner's to rewrite, and the list of whom the agent may pay is replaced
whole with it. A request for approval is decided once: its status, when it was decided and
why it failed are all of it that ever changes.

Revision ID: 0015
Revises: 0014
"""

from collections.abc import Sequence

from alembic import context, op

revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# No column has a default: the application supplies every value, so a forgotten one is an
# error rather than a guess.
TABLES = """
CREATE TABLE agents (
    id             uuid        NOT NULL,
    -- Users belong to identity. Modules do not share tables, so this is an opaque value
    -- with no foreign key.
    owner_user_id  uuid        NOT NULL,
    name           text        NOT NULL,
    status         text        NOT NULL,
    created_at     timestamptz NOT NULL,
    CONSTRAINT pk_agents PRIMARY KEY (id),
    CONSTRAINT ck_agents_name CHECK (char_length(name) BETWEEN 1 AND 100),
    CONSTRAINT ck_agents_status CHECK (status IN ('active', 'paused', 'revoked'))
);

CREATE INDEX ix_agents_owner_user_id ON agents (owner_user_id);

CREATE TABLE agent_keys (
    id            uuid        NOT NULL,
    agent_id      uuid        NOT NULL,
    -- The public part of the key, which a key is found by.
    prefix        text        NOT NULL,
    -- The HMAC-SHA256 of the key's secret part under the server's key, in hex. Neither
    -- the secret nor the key is ever stored.
    key_hash      text        NOT NULL,
    scopes        text[]      NOT NULL,
    -- Null for a key that does not expire.
    expires_at    timestamptz,
    revoked_at    timestamptz,
    last_used_at  timestamptz,
    created_at    timestamptz NOT NULL,
    CONSTRAINT pk_agent_keys PRIMARY KEY (id),
    CONSTRAINT fk_agent_keys_agent_id_agents FOREIGN KEY (agent_id) REFERENCES agents (id),
    CONSTRAINT uq_agent_keys_prefix UNIQUE (prefix),
    CONSTRAINT ck_agent_keys_prefix CHECK (prefix ~ '^[a-z0-9]{12}$'),
    CONSTRAINT ck_agent_keys_key_hash CHECK (key_hash ~ '^[0-9a-f]{64}$'),
    -- "*" is the scope of a user's own session. No key holds it.
    CONSTRAINT ck_agent_keys_scopes_never_all CHECK (NOT ('*' = ANY (scopes)))
);

CREATE INDEX ix_agent_keys_agent_id ON agent_keys (agent_id);

CREATE TABLE agent_policies (
    agent_id               uuid          NOT NULL,
    -- Whole US cents. Null means the policy sets no limit of that sort. The two caps are
    -- kept here as the owner set them and enforced by risk, which is given a copy.
    per_tx_usd             numeric(38,0),
    daily_usd              numeric(38,0),
    -- A movement worth more than this waits for the owner. Null means none does.
    approval_threshold_usd numeric(38,0),
    -- False means the agent pays only whom agent_allowed_recipients names.
    any_recipient          boolean       NOT NULL,
    updated_at             timestamptz   NOT NULL,
    CONSTRAINT pk_agent_policies PRIMARY KEY (agent_id),
    CONSTRAINT fk_agent_policies_agent_id_agents FOREIGN KEY (agent_id) REFERENCES agents (id),
    CONSTRAINT ck_agent_policies_per_tx_usd CHECK (per_tx_usd >= 0),
    CONSTRAINT ck_agent_policies_daily_usd CHECK (daily_usd >= 0),
    CONSTRAINT ck_agent_policies_approval_threshold_usd CHECK (approval_threshold_usd >= 0)
);

CREATE TABLE agent_allowed_recipients (
    agent_id   uuid NOT NULL,
    kind       text NOT NULL,
    -- A user or one of the owner's beneficiaries. Both belong to other modules, so this
    -- is an opaque value with no foreign key.
    target_id  uuid NOT NULL,
    CONSTRAINT pk_agent_allowed_recipients PRIMARY KEY (agent_id, kind, target_id),
    CONSTRAINT fk_agent_allowed_recipients_agent_id_agent_policies
        FOREIGN KEY (agent_id) REFERENCES agent_policies (agent_id),
    CONSTRAINT ck_agent_allowed_recipients_kind CHECK (kind IN ('user', 'beneficiary'))
);

CREATE TABLE agent_approval_requests (
    id             uuid        NOT NULL,
    agent_id       uuid        NOT NULL,
    owner_user_id  uuid        NOT NULL,
    kind           text        NOT NULL,
    -- What the agent asked for, as it was validated: never a credential.
    request        jsonb       NOT NULL,
    status         text        NOT NULL,
    -- The id the movement is made under if the request is approved. It is chosen when the
    -- request is, so that approving it twice can only ever name one movement.
    movement_id    uuid        NOT NULL,
    -- The code of the refusal that an approved request met. Null unless it failed.
    failure_code   text,
    expires_at     timestamptz NOT NULL,
    decided_at     timestamptz,
    created_at     timestamptz NOT NULL,
    CONSTRAINT pk_agent_approval_requests PRIMARY KEY (id),
    CONSTRAINT fk_agent_approval_requests_agent_id_agents
        FOREIGN KEY (agent_id) REFERENCES agents (id),
    CONSTRAINT uq_agent_approval_requests_movement_id UNIQUE (movement_id),
    CONSTRAINT ck_agent_approval_requests_kind CHECK (kind IN ('transfer', 'withdrawal')),
    CONSTRAINT ck_agent_approval_requests_status CHECK (
        status IN ('pending', 'approved', 'rejected', 'expired', 'executed', 'failed')),
    CONSTRAINT ck_agent_approval_requests_request CHECK (jsonb_typeof(request) = 'object'),
    -- Decided exactly when it is no longer pending, and failed exactly when it says why.
    CONSTRAINT ck_agent_approval_requests_decided CHECK ((status = 'pending') = (decided_at IS NULL)),
    CONSTRAINT ck_agent_approval_requests_failure CHECK ((status = 'failed') = (failure_code IS NOT NULL))
);

CREATE INDEX ix_agent_approval_requests_owner_user_id ON agent_approval_requests (owner_user_id)
"""

# A revoked agent and a revoked key stay on record. The application may change only the
# columns that are meant to change: it cannot move an agent to another owner, widen a
# key's scopes, push back its expiry or replace its hash. Nor can it move a policy to
# another agent, or rewrite what a request for approval asked for, whose it is, when it
# lapses or which movement it would make.
GRANTS = """
REVOKE UPDATE, DELETE ON agents, agent_keys FROM "{app_role}";
GRANT UPDATE (status) ON agents TO "{app_role}";
GRANT UPDATE (revoked_at, last_used_at) ON agent_keys TO "{app_role}";
REVOKE UPDATE, DELETE ON agent_policies FROM "{app_role}";
GRANT UPDATE (per_tx_usd, daily_usd, approval_threshold_usd, any_recipient, updated_at)
    ON agent_policies TO "{app_role}";
REVOKE UPDATE ON agent_allowed_recipients FROM "{app_role}";
REVOKE UPDATE, DELETE ON agent_approval_requests FROM "{app_role}";
GRANT UPDATE (status, decided_at, failure_code) ON agent_approval_requests TO "{app_role}"
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
    for statement in (
        "DROP TABLE agent_approval_requests",
        "DROP TABLE agent_allowed_recipients",
        "DROP TABLE agent_policies",
        "DROP TABLE agent_keys",
        "DROP TABLE agents",
    ):
        op.execute(statement)
