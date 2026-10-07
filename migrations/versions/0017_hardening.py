"""Hardening: narrower grants, sealed journal entries, and what login and tokens now keep.

- Failed logins are counted per address and per account in tables of their own, keyed by a
  digest of the address that was typed, so that an address nobody registered is counted
  exactly as a registered one is. The two counters on ``users`` go.
- ``users.tokens_valid_after`` ends every access token issued before it.
- A posting can be added only by the transaction that wrote its entry.
- The application role updates only the columns it has a reason to, on the tables whose
  rows it changes after writing them, and can no longer create temporary tables.
- ``webhook_events.redacted_at`` records that the personal fields of a payload are gone.

Revision ID: 0017
Revises: 0016
"""

from collections.abc import Sequence

from alembic import context, op

revision: str = "0017"
down_revision: str | Sequence[str] | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = """
ALTER TABLE users DROP CONSTRAINT ck_users_failed_logins_not_negative;
ALTER TABLE users DROP COLUMN failed_logins;
ALTER TABLE users DROP COLUMN locked_until;
-- An access token issued before this moment is refused. Null until something ends them.
ALTER TABLE users ADD COLUMN tokens_valid_after timestamptz;

-- Consecutive failed logins to one address from one client, and until when they keep that
-- client out. The client is an IPv4 address or an IPv6 /64.
CREATE TABLE login_lockouts (
    -- The SHA-256 of the address as it is stored, in hex. Not a user id: what is counted
    -- must not depend on whether the address is anybody's.
    email_hash     text        NOT NULL,
    client         text        NOT NULL,
    failed_logins  integer     NOT NULL,
    locked_until   timestamptz,
    updated_at     timestamptz NOT NULL,
    CONSTRAINT pk_login_lockouts PRIMARY KEY (email_hash, client),
    CONSTRAINT ck_login_lockouts_failed_logins_positive CHECK (failed_logins > 0)
);

CREATE INDEX ix_login_lockouts_updated_at ON login_lockouts (updated_at);

-- Recent failed logins to one address from anywhere. They slow every answer about that
-- address down and never refuse the right password.
CREATE TABLE login_throttles (
    email_hash      text        NOT NULL,
    failed_logins   integer     NOT NULL,
    last_failed_at  timestamptz NOT NULL,
    CONSTRAINT pk_login_throttles PRIMARY KEY (email_hash),
    CONSTRAINT ck_login_throttles_failed_logins_positive CHECK (failed_logins > 0)
);

CREATE INDEX ix_login_throttles_last_failed_at ON login_throttles (last_failed_at);

-- When the personal fields of the payload were removed. Null while they are still there.
ALTER TABLE webhook_events ADD COLUMN redacted_at timestamptz;

-- What the retention job looks for: processed events that still hold their payload whole.
CREATE INDEX ix_webhook_events_processed_at_unredacted
    ON webhook_events (processed_at) WHERE redacted_at IS NULL
"""

# An entry is sealed when the transaction that wrote it ends. A row this transaction can
# see was written either by a transaction that has committed or by this one, and only for
# this one is the writer still in progress; a savepoint's rows carry the savepoint's own
# id, which is in progress for as long as its transaction is. The row's xmin is 32 bits
# and the status is asked of a 64-bit id, so the epoch is taken from this transaction's
# own id, stepping back one epoch for an xmin from before the counter last wrapped.
#
# The deferred balance check would let a balanced pair through; the verifier would find
# it afterwards. This refuses it.
SEAL_ENTRY = """
CREATE FUNCTION ledger_seal_entry() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    entry_xmin bigint;
    this_xid bigint;
    writer_xid bigint;
BEGIN
    SELECT e.xmin::text::bigint INTO entry_xmin FROM journal_entries e WHERE e.id = NEW.entry_id;
    IF NOT FOUND THEN
        -- Not there, or written by a transaction that has not committed. Left to the
        -- foreign key, that second case would wait for the other transaction and then pass.
        RAISE EXCEPTION 'journal entry % was not written by this transaction', NEW.entry_id
            USING ERRCODE = 'CR004';
    END IF;

    this_xid := pg_current_xact_id()::text::bigint;
    writer_xid := this_xid - (this_xid % 4294967296) + entry_xmin;
    IF writer_xid > this_xid + 2147483648 THEN
        writer_xid := writer_xid - 4294967296;
    END IF;

    IF pg_xact_status(writer_xid::text::xid8) IS DISTINCT FROM 'in progress' THEN
        RAISE EXCEPTION 'journal entry % is sealed: postings are written with their entry',
            NEW.entry_id USING ERRCODE = 'CR004';
    END IF;
    RETURN NEW;
END
$$
"""

SEAL_TRIGGER = """
CREATE TRIGGER postings_entry_unsealed
    BEFORE INSERT ON postings
    FOR EACH ROW EXECUTE FUNCTION ledger_seal_entry()
"""

# The columns the application changes after a row is written, table by table. Everything
# else in these rows is fixed when it is inserted: who, how much, in what, to where.
GRANTS = """
REVOKE UPDATE ON withdrawals, deposits, fx_quotes, webhook_events FROM "{app_role}";
GRANT UPDATE (status, provider_ref, provider_fee, failure_reason, final_entry_id,
              submitted_at, updated_at)
    ON withdrawals TO "{app_role}";
GRANT UPDATE (status, user_id, entry_id, updated_at) ON deposits TO "{app_role}";
GRANT UPDATE (status) ON fx_quotes TO "{app_role}";
GRANT UPDATE (processed_at, outcome, payload, redacted_at) ON webhook_events TO "{app_role}"
"""

UNGRANTS = """
REVOKE UPDATE (status, provider_ref, provider_fee, failure_reason, final_entry_id,
               submitted_at, updated_at)
    ON withdrawals FROM "{app_role}";
REVOKE UPDATE (status, user_id, entry_id, updated_at) ON deposits FROM "{app_role}";
REVOKE UPDATE (status) ON fx_quotes FROM "{app_role}";
REVOKE UPDATE (processed_at, outcome, payload, redacted_at) ON webhook_events FROM "{app_role}";
GRANT UPDATE ON withdrawals, deposits, fx_quotes, webhook_events TO "{app_role}"
"""

# Every role may create temporary tables in a new database, through PUBLIC, so taking the
# privilege from the application role alone would take nothing. The owner keeps it by
# owning the database. The database's name is not known here, and a privilege on a
# database cannot be named without it.
#
# This is a privilege on the database and not on anything in it: a database made as a
# copy of this one starts with PUBLIC's default again.
TEMPORARY = """
DO $$
BEGIN
    EXECUTE format('{verb} TEMPORARY ON DATABASE %I {preposition} PUBLIC', current_database());
    EXECUTE format(
        '{verb} TEMPORARY ON DATABASE %I {preposition} %I', current_database(), '{app_role}'
    );
END
$$
"""

UNDO_TABLES = """
DROP INDEX ix_webhook_events_processed_at_unredacted;
ALTER TABLE webhook_events DROP COLUMN redacted_at;
DROP TABLE login_throttles;
DROP TABLE login_lockouts;
ALTER TABLE users DROP COLUMN tokens_valid_after;
ALTER TABLE users ADD COLUMN failed_logins integer;
UPDATE users SET failed_logins = 0;
ALTER TABLE users ALTER COLUMN failed_logins SET NOT NULL;
ALTER TABLE users ADD COLUMN locked_until timestamptz;
ALTER TABLE users
    ADD CONSTRAINT ck_users_failed_logins_not_negative CHECK (failed_logins >= 0)
"""


def _statements(script: str) -> list[str]:
    return [statement.strip() for statement in script.split(";\n") if statement.strip()]


def upgrade() -> None:
    app_role = context.config.attributes["app_role"]
    for statement in _statements(TABLES):
        op.execute(statement)
    op.execute(SEAL_ENTRY)
    op.execute(SEAL_TRIGGER)
    for statement in _statements(GRANTS.format(app_role=app_role)):
        op.execute(statement)
    op.execute(TEMPORARY.format(verb="REVOKE", preposition="FROM", app_role=app_role))


def downgrade() -> None:
    app_role = context.config.attributes["app_role"]
    # Back to PUBLIC only, which is where the application role had it from.
    op.execute(
        "DO $$ BEGIN EXECUTE format('GRANT TEMPORARY ON DATABASE %I TO PUBLIC',"
        " current_database()); END $$"
    )
    for statement in _statements(UNGRANTS.format(app_role=app_role)):
        op.execute(statement)
    op.execute("DROP TRIGGER postings_entry_unsealed ON postings")
    op.execute("DROP FUNCTION ledger_seal_entry()")
    for statement in _statements(UNDO_TABLES):
        op.execute(statement)
