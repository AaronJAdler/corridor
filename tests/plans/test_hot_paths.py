"""The statements on the hot paths use their indexes.

Each test runs a path as the application runs it and reads the plans PostgreSQL makes for
the statements it issued. A plan that scans a large table sequentially, where an index was
built for that statement, fails the test. The suite has no other way to notice an index that
was dropped, a predicate that stopped matching one, or a statement that was rewritten past
its index: such a change passes every functional test and shows up as a slow production.

ADR 0022 says why plans are tested and `docs/architecture.md`, "Hot paths and their
indexes", lists what is expected of each path.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import audit, ledger
from corridor.api import idempotency
from corridor.outbox import service as outbox
from corridor.platform.clock import utcnow
from corridor.platform.db import Database
from corridor.worker.purge import purge_idempotency_keys
from tests.plans.conftest import Seeded
from tests.plans.support import Plan, explain, explained, recording


def _no_seq_scan(plans: list[Plan], relation: str) -> None:
    for plan in plans:
        assert not plan.seq_scans_of(relation), (
            f"a sequential scan of {relation} in:\n{plan.statement.sql}\n{plan}"
        )


def _index_used(plans: list[Plan], relation: str, index: str) -> None:
    used = {name for plan in plans for name in plan.indexes_used(relation)}
    assert index in used, f"{relation} was not read through {index} but through {used or 'nothing'}"


async def test_the_outbox_claim_reads_due_events_through_their_partial_indexes(
    db: Database, seeded: Seeded
) -> None:
    """The claim is one UPDATE over a materialised CTE. Its CTE must find the pending and the
    expired rows through ``ix_outbox_events_due`` and ``ix_outbox_events_claimed``, which are
    partial on status: with thousands of ``done`` rows in the table, a scan over all of them
    on every poll is what the indexes exist to avoid."""

    async def run(session: AsyncSession) -> None:
        await outbox.claim(session, now=utcnow(), limit=10, claim_seconds=30, max_attempts=5)

    plans = await explained(db, "outbox_events", run)
    _no_seq_scan(plans, "outbox_events")
    _index_used(plans, "outbox_events", "ix_outbox_events_due")
    _index_used(plans, "outbox_events", "ix_outbox_events_claimed")


async def test_an_account_statement_walks_its_postings_index_backwards(
    db: Database, seeded: Seeded
) -> None:
    """Newest first, one account: ``ix_postings_account_id_seq`` finds the account's postings
    and gives their order, however long the account's history and however many accounts
    there are. (Whether the planner walks it backwards or sorts the few rows it finds is its
    call; that it never reads the whole table is ours.)"""

    async def run(session: AsyncSession) -> None:
        await ledger.statement(session, seeded.account_id, limit=50)

    plans = await explained(db, "postings", run)
    _no_seq_scan(plans, "postings")
    _index_used(plans, "postings", "ix_postings_account_id_seq")


async def test_the_audit_log_for_one_principal_does_not_scan_the_log(
    db: Database, seeded: Seeded
) -> None:
    async def run(session: AsyncSession) -> None:
        await audit.list_events(session, principal_id=seeded.principal_id, limit=50)

    plans = await explained(db, "audit_events", run)
    _no_seq_scan(plans, "audit_events")
    _index_used(plans, "audit_events", "ix_audit_events_principal_id_occurred_at")


async def test_an_idempotency_key_is_looked_up_by_its_primary_key(
    db: Database, seeded: Seeded
) -> None:
    """The lookup runs on every idempotent request; it is a primary-key probe or nothing."""

    async def work(session: AsyncSession) -> idempotency.StoredResponse:
        return idempotency.StoredResponse(201, {"ok": True}, {})

    async with recording(db) as recorder:
        await idempotency.run_idempotent(
            db,
            actor_id=seeded.actor_id,
            key="plans-fresh-key",
            method="POST",
            route="/plans",
            body=b"{}",
            work=work,
        )
    reads = [s for s in recorder.about("idempotency_keys") if s.sql.lstrip().startswith("SELECT")]
    assert reads, "the path read the key's row"
    async with db.transaction() as session:
        plans = [await explain(session, s) for s in reads]
    _no_seq_scan(plans, "idempotency_keys")
    _index_used(plans, "idempotency_keys", "pk_idempotency_keys")


async def test_purging_old_idempotency_keys_finds_them_by_created_at(
    db: Database, seeded: Seeded
) -> None:
    """The purge runs often, so each run deletes a sliver of the oldest keys.
    ``ix_idempotency_keys_created_at`` is what keeps that from being a scan of every key
    ever stored. (The sliver matters: PostgreSQL rightly scans a small table sequentially
    when a delete would touch a few percent of it, so the seed makes the cutoff realistic.)"""

    async def run(session: AsyncSession) -> None:
        await purge_idempotency_keys(session, older_than=seeded.oldest_key_cutoff)  # type: ignore[arg-type]

    plans = await explained(db, "idempotency_keys", run)
    _no_seq_scan(plans, "idempotency_keys")
    _index_used(plans, "idempotency_keys", "ix_idempotency_keys_created_at")


@pytest.mark.parametrize(
    ("relation", "index"),
    [
        ("outbox_events", "ix_outbox_events_due"),
        ("outbox_events", "ix_outbox_events_claimed"),
        ("postings", "ix_postings_account_id_seq"),
        ("audit_events", "ix_audit_events_principal_id_occurred_at"),
        ("idempotency_keys", "ix_idempotency_keys_created_at"),
    ],
)
async def test_every_index_a_plan_test_relies_on_exists(
    db: Database, relation: str, index: str
) -> None:
    """A plan test that names an index the migrations never built would fail confusingly;
    this one fails plainly."""
    from sqlalchemy import text

    async with db.transaction() as session:
        found = await session.execute(
            text("SELECT 1 FROM pg_indexes WHERE tablename = :t AND indexname = :i"),
            {"t": relation, "i": index},
        )
        assert found.scalar() == 1, f"{index} on {relation} is not in the schema"
