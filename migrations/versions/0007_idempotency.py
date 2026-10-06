"""Idempotency keys: one row per request a client has asked to be performed at most once.

A key is working state, not history: the application writes the outcome onto the row in the
transaction that produced it and deletes the row a day later, so the application role keeps
the full row access the baseline grants.

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = r"""
CREATE TABLE idempotency_keys (
    -- A key belongs to the principal that sent it: two principals may use the same key.
    actor_id          uuid        NOT NULL,
    key               text        NOT NULL,
    -- SHA-256 of the method, the route template and the canonical body, in hex. A key
    -- that comes back with another fingerprint is being reused for a different request.
    fingerprint       text        NOT NULL,
    -- The outcome of the one attempt the key stands for. All null until it is known.
    status_code       integer,
    response_body     jsonb,
    response_headers  jsonb,
    created_at        timestamptz NOT NULL,
    completed_at      timestamptz,
    CONSTRAINT pk_idempotency_keys PRIMARY KEY (actor_id, key)
);

-- The purge deletes by age.
CREATE INDEX ix_idempotency_keys_created_at ON idempotency_keys (created_at)
"""


def _statements(script: str) -> list[str]:
    return [statement.strip() for statement in script.split(";\n") if statement.strip()]


def upgrade() -> None:
    for statement in _statements(TABLES):
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE idempotency_keys")
