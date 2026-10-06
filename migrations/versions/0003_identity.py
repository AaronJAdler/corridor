"""Identity: users and refresh tokens.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

from alembic import context, op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# No column has a default: the application supplies every value, so a forgotten one is an
# error rather than a guess.
TABLES = """
CREATE TABLE users (
    id                 uuid        NOT NULL,
    -- Stored lower-cased, so the unique constraint is also a case-insensitive one.
    email              text        NOT NULL,
    handle             text        NOT NULL,
    display_name       text        NOT NULL,
    password_hash      text        NOT NULL,
    role               text        NOT NULL,
    kyc_tier           smallint    NOT NULL,
    status             text        NOT NULL,
    restricted_reason  text,
    -- Consecutive failed logins, and until when they keep the account locked.
    failed_logins      integer     NOT NULL,
    locked_until       timestamptz,
    created_at         timestamptz NOT NULL,
    updated_at         timestamptz NOT NULL,
    CONSTRAINT pk_users PRIMARY KEY (id),
    CONSTRAINT uq_users_email UNIQUE (email),
    CONSTRAINT uq_users_handle UNIQUE (handle),
    CONSTRAINT ck_users_email_lowercase CHECK (email = lower(email)),
    CONSTRAINT ck_users_handle CHECK (handle ~ '^[a-z0-9_]{3,30}$'),
    CONSTRAINT ck_users_role CHECK (role IN ('user', 'admin')),
    CONSTRAINT ck_users_kyc_tier CHECK (kyc_tier BETWEEN 0 AND 2),
    CONSTRAINT ck_users_status CHECK (status IN ('active', 'restricted', 'closed')),
    CONSTRAINT ck_users_failed_logins_not_negative CHECK (failed_logins >= 0)
);

CREATE TABLE refresh_tokens (
    id          uuid        NOT NULL,
    user_id     uuid        NOT NULL,
    -- One login session. Every token a session is rotated into shares its family.
    family_id   uuid        NOT NULL,
    -- The SHA-256 of the opaque token, in hex. The token itself is never stored.
    token_hash  text        NOT NULL,
    issued_at   timestamptz NOT NULL,
    expires_at  timestamptz NOT NULL,
    -- Set when the token is exchanged for its successor.
    used_at     timestamptz,
    revoked_at  timestamptz,
    CONSTRAINT pk_refresh_tokens PRIMARY KEY (id),
    CONSTRAINT fk_refresh_tokens_user_id_users FOREIGN KEY (user_id) REFERENCES users (id),
    CONSTRAINT uq_refresh_tokens_token_hash UNIQUE (token_hash)
);

CREATE INDEX ix_refresh_tokens_family_id ON refresh_tokens (family_id);
CREATE INDEX ix_refresh_tokens_user_id ON refresh_tokens (user_id)
"""

# A user is closed, never removed: rows in other modules go on referring to the id. Refresh
# tokens keep DELETE, so that expired ones can be pruned.
REVOKES = """
REVOKE DELETE ON users FROM "{app_role}"
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
        "DROP TABLE refresh_tokens",
        "DROP TABLE users",
    ):
        op.execute(statement)
