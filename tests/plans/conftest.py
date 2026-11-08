"""A database with enough rows in the hot tables for the planner to have a choice."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

import pytest
from sqlalchemy import text

from corridor import ledger
from corridor.platform.clock import utcnow
from corridor.platform.db import Database
from corridor.platform.ids import new_id
from tests.support.ledger import fund, open_user, transfer_draft

ROWS = 4_000  # per append-only table: well past the point where a sequential scan is cheaper
USERS = 25
TRANSFERS_PER_USER = 8


@dataclass(frozen=True)
class Seeded:
    actor_id: uuid.UUID  # has idempotency keys
    account_id: uuid.UUID  # a user's available account with a long statement
    principal_id: uuid.UUID  # has audit events
    oldest_key_cutoff: object  # a moment that only the oldest sliver of keys precedes


@pytest.fixture
async def seeded(db: Database, owner_db: Database) -> Seeded:
    now = utcnow()
    actor = new_id()
    principal = new_id()

    async with db.transaction() as session:
        # Append-only tables take rows directly, spread over time, through the application
        # role, as the application writes them. generate_series keeps the seeding to one
        # statement each.
        await session.execute(
            text(
                """
                INSERT INTO idempotency_keys
                    (actor_id, key, fingerprint, status_code, response_body, response_headers,
                     created_at, completed_at)
                SELECT CASE WHEN n % 50 = 0 THEN CAST(:actor AS uuid) ELSE gen_random_uuid() END,
                       'key-' || n, 'fp-' || n, 200, '{}'::jsonb, '{}'::jsonb,
                       CAST(:now AS timestamptz) - make_interval(mins => CAST(:rows AS int) - n),
                       CAST(:now AS timestamptz) - make_interval(mins => CAST(:rows AS int) - n)
                FROM generate_series(1, CAST(:rows AS int)) AS n
                """
            ),
            {"actor": actor, "now": now, "rows": ROWS},
        )
        await session.execute(
            text(
                """
                INSERT INTO outbox_events
                    (id, topic, payload, status, attempts, available_at, locked_until, claim_id,
                     dedup_key, last_error, context, created_at, finished_at)
                -- The steady state of a queue that is kept up with: nearly everything is
                -- done, a handful is pending, and now and then a claim has run out. (A
                -- backlog, where most rows are due, is a different and easier case for the
                -- planner: it walks the primary key and finds due rows at once.)
                SELECT gen_random_uuid(), 'plans.seed', '{}'::jsonb,
                       CASE WHEN n % 250 = 0 THEN 'pending'
                            WHEN n % 250 = 1 THEN 'processing'
                            ELSE 'done' END,
                       1, CAST(:now AS timestamptz) - make_interval(secs => n),
                       CASE WHEN n % 250 = 1 THEN CAST(:now AS timestamptz) - make_interval(secs => 60) END,
                       CASE WHEN n % 250 = 1 THEN gen_random_uuid() END,
                       NULL, NULL, '{}'::jsonb,
                       CAST(:now AS timestamptz) - make_interval(secs => n),
                       CASE WHEN n % 250 IN (0, 1) THEN NULL
                            ELSE CAST(:now AS timestamptz) - make_interval(secs => n) END
                FROM generate_series(1, CAST(:rows AS int)) AS n
                """
            ),
            {"now": now, "rows": ROWS},
        )
        await session.execute(
            text(
                """
                INSERT INTO audit_events
                    (id, occurred_at, actor_type, actor_id, principal_id, action, resource_type,
                     resource_id, outcome, request_id, details)
                SELECT gen_random_uuid(), CAST(:now AS timestamptz) - make_interval(secs => n), 'user', 'seed',
                       CASE WHEN n % 40 = 0 THEN CAST(:principal AS uuid) ELSE gen_random_uuid() END,
                       'plans.seed', 'seed', 'r-' || n, 'success', NULL, '{}'::jsonb
                FROM generate_series(1, CAST(:rows AS int)) AS n
                """
            ),
            {"now": now, "principal": principal, "rows": ROWS},
        )

        # The ledger is written only through ledger.post_entry, so its rows come from real
        # entries: funded users transferring among themselves.
        users = [await open_user(session) for _ in range(USERS)]
        for user in users:
            await fund(session, user.available, 1_000_000)
        for i, user in enumerate(users):
            for j in range(TRANSFERS_PER_USER):
                recipient = users[(i + j + 1) % USERS]
                await ledger.post_entry(
                    session, transfer_draft(user.available, recipient.available, 100 + j)
                )
        account = users[0].available

    # Fresh statistics: the plans below are the plans the planner would make for these rows.
    # As the owner: ANALYZE by any other role is skipped with a warning, not refused, and the
    # planner then works from defaults that make every estimate wrong.
    async with owner_db.transaction() as session:
        for table in ("idempotency_keys", "outbox_events", "audit_events", "postings"):
            await session.execute(text(f"ANALYZE {table}"))

    return Seeded(
        actor_id=actor,
        account_id=account,
        principal_id=principal,
        # The purge job runs every few minutes against keys kept for a day, so one run deletes
        # a fraction of a percent of the table; the cutoff here selects the oldest 0.25%.
        oldest_key_cutoff=now - timedelta(minutes=ROWS - ROWS // 400),
    )
