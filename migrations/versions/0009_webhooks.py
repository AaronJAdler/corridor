"""Webhooks: stored provider events.

Every delivery that passes signature verification is stored here before it is acknowledged,
once per provider and event id. The row is the record of what the provider said, so the
application may add one and mark it processed, and may never delete one.

Revision ID: 0009
Revises: 0008
"""

from collections.abc import Sequence

from alembic import context, op

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = """
CREATE TABLE webhook_events (
    id            uuid        NOT NULL,
    provider      text        NOT NULL,
    -- The provider's own id for the event. A delivery is retried with the same id.
    event_id      text        NOT NULL,
    type          text        NOT NULL,
    -- The envelope as it was delivered, so that the event can be processed again.
    payload       jsonb       NOT NULL,
    received_at   timestamptz NOT NULL,
    -- Both null until the worker has dealt with the event, and both set from then on.
    processed_at  timestamptz,
    outcome       text,
    CONSTRAINT pk_webhook_events PRIMARY KEY (id),
    -- What makes a second delivery of an event a no-op, however the two are interleaved.
    CONSTRAINT uq_webhook_events_provider_event_id UNIQUE (provider, event_id),
    CONSTRAINT ck_webhook_events_provider CHECK (provider IN ('simbank', 'simcustody')),
    CONSTRAINT ck_webhook_events_event_id CHECK (event_id <> ''),
    CONSTRAINT ck_webhook_events_type CHECK (type <> ''),
    CONSTRAINT ck_webhook_events_outcome CHECK (outcome IN ('processed', 'ignored')),
    CONSTRAINT ck_webhook_events_processed CHECK ((processed_at IS NULL) = (outcome IS NULL))
)
"""

# What is left to the application role after the baseline's default of full row access:
# it stores an event and marks it processed. A stored event is never removed.
REVOKES = """
REVOKE DELETE ON webhook_events FROM "{app_role}"
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
    op.execute("DROP TABLE webhook_events")
