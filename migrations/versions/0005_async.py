"""Async backbone: the outbox and scheduled-job bookkeeping.

The outbox is a work queue, not history: the application updates an event as it is claimed
and finished and deletes it once it is old, so the application role keeps the full row
access the baseline grants. The same goes for the one row each scheduled job keeps.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = r"""
CREATE TABLE outbox_events (
    -- UUIDv7, so id order is the order in which events were created.
    id            uuid        NOT NULL,
    topic         text        NOT NULL,
    payload       jsonb       NOT NULL,
    status        text        NOT NULL,
    -- How many times a worker has claimed the event.
    attempts      integer     NOT NULL,
    -- Not to be processed before this time: now for a new event, later for a retry.
    available_at  timestamptz NOT NULL,
    -- While an event is being processed, when its claim runs out. A worker that dies
    -- leaves the claim to expire, and the event is then picked up again.
    locked_until  timestamptz,
    -- Made new for each claim and null outside one. A worker records its result only
    -- where this is still the claim it took, which the attempt number cannot promise:
    -- a requeue starts the count again.
    claim_id      uuid,
    dedup_key     text,
    last_error    text,
    -- The request id and the like, carried across the queue so a request can be followed.
    context       jsonb       NOT NULL,
    created_at    timestamptz NOT NULL,
    finished_at   timestamptz,
    CONSTRAINT pk_outbox_events PRIMARY KEY (id),
    CONSTRAINT ck_outbox_events_topic
        CHECK (topic ~ '^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$'),
    CONSTRAINT ck_outbox_events_status
        CHECK (status IN ('pending', 'processing', 'done', 'dead')),
    CONSTRAINT ck_outbox_events_attempts CHECK (attempts >= 0)
);

-- The two things a claim looks for, each indexed over only the rows that can match. Events
-- that are done, which is nearly all of them, are in neither index.
CREATE INDEX ix_outbox_events_due ON outbox_events (available_at, id)
    WHERE status = 'pending';
CREATE INDEX ix_outbox_events_claimed ON outbox_events (locked_until)
    WHERE status = 'processing';

-- One event per topic and key, for a caller that might enqueue the same thing twice.
CREATE UNIQUE INDEX uq_outbox_events_topic_dedup_key ON outbox_events (topic, dedup_key)
    WHERE dedup_key IS NOT NULL;

-- One row per scheduled job: when it last started, and how that run ended.
CREATE TABLE job_runs (
    name              text        NOT NULL,
    last_started_at   timestamptz NOT NULL,
    -- Null while a run is in progress, and for good if the worker died during it.
    last_finished_at  timestamptz,
    last_error        text,
    CONSTRAINT pk_job_runs PRIMARY KEY (name)
)
"""

# The search path is pinned, with the temporary schema last, so that `pg_notify` is the
# catalogue's whatever path the inserting session has.
NOTIFY_FUNCTION = """
CREATE FUNCTION outbox_notify() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    PERFORM pg_notify('corridor_outbox', '');
    RETURN NULL;
END
$$
"""

# PostgreSQL delivers a notification when the notifying transaction commits, which is the
# moment the new events become visible to a worker. Statement-level, because a worker needs
# to hear that there is work, not how much.
NOTIFY_TRIGGER = """
CREATE TRIGGER outbox_events_notify
    AFTER INSERT ON outbox_events
    FOR EACH STATEMENT EXECUTE FUNCTION outbox_notify()
"""


def _statements(script: str) -> list[str]:
    return [statement.strip() for statement in script.split(";\n") if statement.strip()]


def upgrade() -> None:
    for statement in _statements(TABLES):
        op.execute(statement)
    op.execute(NOTIFY_FUNCTION)
    op.execute(NOTIFY_TRIGGER)


def downgrade() -> None:
    for statement in (
        "DROP TABLE job_runs",
        "DROP TABLE outbox_events",
        "DROP FUNCTION outbox_notify()",
    ):
        op.execute(statement)
