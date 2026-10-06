"""Audit: the append-only audit log.

One table that records who did what, to what, on whose behalf, and how it turned out. An
audit log outlives every other record, so the database itself refuses to change or remove
a row once it is written.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

from alembic import context, op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# No column has a default. The application supplies every value, the time included, so
# nothing is filled in behind its back. The string is raw so that the backslash in the
# action pattern reaches PostgreSQL as written.
TABLE = r"""
CREATE TABLE audit_events (
    id             uuid        NOT NULL,
    occurred_at    timestamptz NOT NULL,
    actor_type     text        NOT NULL,
    -- A user or agent id, a provider name or a job name. The log knows nothing about the
    -- modules that write to it, so there is no foreign key out of it.
    actor_id       text,
    -- The user on whose behalf the action was taken, when there is one.
    principal_id   uuid,
    action         text        NOT NULL,
    resource_type  text,
    resource_id    text,
    outcome        text        NOT NULL,
    request_id     text,
    details        jsonb       NOT NULL,
    CONSTRAINT pk_audit_events PRIMARY KEY (id),
    CONSTRAINT ck_audit_events_actor_type
        CHECK (actor_type IN ('user', 'agent', 'admin', 'system', 'provider')),
    -- Dotted lower-case segments, at least two of them, as in transfer.created.
    CONSTRAINT ck_audit_events_action
        CHECK (action ~ '^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$'),
    CONSTRAINT ck_audit_events_outcome CHECK (outcome IN ('success', 'denied', 'failed'))
);

CREATE INDEX ix_audit_events_principal_id_occurred_at
    ON audit_events (principal_id, occurred_at);
CREATE INDEX ix_audit_events_resource_type_resource_id
    ON audit_events (resource_type, resource_id);
CREATE INDEX ix_audit_events_action_occurred_at ON audit_events (action, occurred_at)
"""

# Statement-level, so it fires even for a statement that would match no rows, and for
# TRUNCATE, which a row-level trigger never sees. This is the barrier that also stops the
# owner.
TRIGGER = """
CREATE TRIGGER audit_events_append_only
    BEFORE UPDATE OR DELETE OR TRUNCATE ON audit_events
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation()
"""

# What is left to the application role after the baseline's default of full row access:
# it may read the log and add to it, and nothing else.
REVOKE = 'REVOKE UPDATE, DELETE ON audit_events FROM "{app_role}"'


def _statements(script: str) -> list[str]:
    return [statement.strip() for statement in script.split(";\n") if statement.strip()]


def upgrade() -> None:
    app_role = context.config.attributes["app_role"]
    for statement in _statements(TABLE):
        op.execute(statement)
    op.execute(TRIGGER)
    op.execute(REVOKE.format(app_role=app_role))


def downgrade() -> None:
    # The indexes, the trigger and the privileges go with the table.
    op.execute("DROP TABLE audit_events")
