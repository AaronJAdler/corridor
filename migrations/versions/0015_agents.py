"""Agents: the principals a user lets act on their wallet, and the keys they act with.

An agent changes in one way, its status. A key changes in two: it is revoked, and the time
it was last used moves on. Nothing else about either is ever rewritten, and neither is
ever removed.

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

CREATE INDEX ix_agent_keys_agent_id ON agent_keys (agent_id)
"""

# A revoked agent and a revoked key stay on record. The application may change only the
# columns that are meant to change: it cannot move an agent to another owner, widen a
# key's scopes, push back its expiry or replace its hash.
GRANTS = """
REVOKE UPDATE, DELETE ON agents, agent_keys FROM "{app_role}";
GRANT UPDATE (status) ON agents TO "{app_role}";
GRANT UPDATE (revoked_at, last_used_at) ON agent_keys TO "{app_role}"
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
    for statement in ("DROP TABLE agent_keys", "DROP TABLE agents"):
        op.execute(statement)
