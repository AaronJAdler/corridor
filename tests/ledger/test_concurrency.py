"""The ledger under real contention: many transactions, one PostgreSQL, real row locks."""

import asyncio
import random
import uuid
from collections.abc import Awaitable, Callable

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import ledger
from corridor.ledger import AccountKind, EntryDraft, InsufficientFunds, credit, debit
from corridor.platform.db import DEADLOCK_DETECTED, LOCK_NOT_AVAILABLE, Database, sqlstate_of
from corridor.platform.ids import new_id
from tests.support.ledger import funded_user, open_user, system_account, transfer_draft


def deadlock_retries() -> float:
    return (
        REGISTRY.get_sample_value(
            "corridor_db_transaction_retries_total", {"sqlstate": DEADLOCK_DETECTED}
        )
        or 0.0
    )


async def count(db: Database, table: str) -> int:
    async with db.transaction() as session:
        return int((await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one())  # noqa: S608


async def test_200_debits_against_a_balance_that_affords_50_give_exactly_50(db: Database) -> None:
    async with db.transaction() as session:
        sender = await funded_user(session, 50_00)
        sink = await system_account(session, AccountKind.SUSPENSE)

    async def spend_one(session: AsyncSession) -> bool:
        try:
            await ledger.post_entry(
                session,
                EntryDraft(
                    "transfer",
                    "spend",
                    str(new_id()),
                    (debit(sender.available, 1_00), credit(sink, 1_00)),
                ),
            )
        except InsufficientFunds:
            return False
        return True

    before = deadlock_retries()
    outcomes = await asyncio.gather(*(db.run(spend_one) for _ in range(200)))

    assert outcomes.count(True) == 50
    assert outcomes.count(False) == 150
    assert deadlock_retries() == before
    async with db.transaction() as session:
        assert await ledger.get_balances(session, [sender.available, sink]) == {
            sender.available: 0,
            sink: 50_00,
        }
    # One funding entry and exactly fifty spends. A refused debit left nothing behind.
    assert await count(db, "journal_entries") == 51


async def test_transfers_in_opposite_directions_queue_and_never_deadlock(db: Database) -> None:
    async with db.transaction() as session:
        maria = await funded_user(session, 1_000_00)
        joao = await funded_user(session, 1_000_00)

    def send(sender: uuid.UUID, recipient: uuid.UUID) -> Callable[[AsyncSession], Awaitable[None]]:
        async def work(session: AsyncSession) -> None:
            await ledger.post_entry(session, transfer_draft(sender, recipient, 1_00))

        return work

    before = deadlock_retries()
    await asyncio.gather(
        *(db.run(send(maria.available, joao.available)) for _ in range(60)),
        *(db.run(send(joao.available, maria.available)) for _ in range(40)),
    )

    assert deadlock_retries() == before
    async with db.transaction() as session:
        assert await ledger.get_balances(session, [maria.available, joao.available]) == {
            maria.available: 1_000_00 - 60_00 + 40_00,
            joao.available: 1_000_00 + 60_00 - 40_00,
        }


async def test_the_deadlock_those_locks_prevent_is_real(db: Database) -> None:
    """A control: lock the same two balance rows in posting order and PostgreSQL has to
    break the cycle by killing a transaction. This is what ascending-id locking avoids."""
    async with db.transaction() as session:
        maria = await funded_user(session, 10_00)
        joao = await funded_user(session, 10_00)

    both_hold_one = asyncio.Barrier(2)

    async def naive_transfer(first: uuid.UUID, second: uuid.UUID) -> str | None:
        lock = text("SELECT balance FROM account_balances WHERE account_id = :account FOR UPDATE")
        try:
            async with db.transaction() as session:
                await session.execute(lock, {"account": first})
                await both_hold_one.wait()
                await session.execute(lock, {"account": second})
        except DBAPIError as error:
            return sqlstate_of(error)
        return None

    outcomes = await asyncio.gather(
        naive_transfer(maria.available, joao.available),
        naive_transfer(joao.available, maria.available),
    )

    assert sorted(outcomes, key=str) == [DEADLOCK_DETECTED, None]


async def test_one_event_posted_by_50_concurrent_callers_is_posted_once(db: Database) -> None:
    async with db.transaction() as session:
        sender = await funded_user(session, 100_00)
        recipient = await open_user(session)
    draft = transfer_draft(sender.available, recipient.available, 30_00, source_id="tr_once")

    async def post(session: AsyncSession) -> tuple[uuid.UUID, bool]:
        entry = await ledger.post_entry(session, draft)
        return entry.id, entry.created

    results = await asyncio.gather(*(db.run(post) for _ in range(50)))

    assert len({entry_id for entry_id, _ in results}) == 1
    assert [created for _, created in results].count(True) == 1
    async with db.transaction() as session:
        assert await ledger.get_balances(session, [sender.available, recipient.available]) == {
            sender.available: 70_00,
            recipient.available: 30_00,
        }
    assert await count(db, "journal_entries") == 2


async def test_one_event_between_system_accounts_is_posted_once_without_any_row_lock(
    db: Database,
) -> None:
    # Neither account is constrained, so no balance row serialises the callers. The unique
    # source is the only thing standing between 50 callers and 50 entries.
    async with db.transaction() as session:
        settlement = await system_account(session, AccountKind.BANK_SETTLEMENT)
        suspense = await system_account(session, AccountKind.SUSPENSE)
    draft = EntryDraft(
        "deposit_suspense",
        "deposit",
        "dep_once",
        (debit(settlement, 75_00), credit(suspense, 75_00)),
    )

    async def post(session: AsyncSession) -> tuple[uuid.UUID, bool]:
        entry = await ledger.post_entry(session, draft)
        return entry.id, entry.created

    results = await asyncio.gather(*(db.run(post) for _ in range(50)))

    assert len({entry_id for entry_id, _ in results}) == 1
    assert [created for _, created in results].count(True) == 1
    async with db.transaction() as session:
        assert await ledger.get_balances(session, [settlement, suspense]) == {
            settlement: 75_00,
            suspense: 75_00,
        }
    assert await count(db, "postings") == 2


async def test_mixed_credits_and_debits_on_one_account_add_up(db: Database) -> None:
    async with db.transaction() as session:
        user = await funded_user(session, 20_00)
        settlement = await system_account(session, AccountKind.BANK_SETTLEMENT)
        sink = await system_account(session, AccountKind.SUSPENSE)

    randomness = random.Random(20260115)  # noqa: S311 - a reproducible workload, not a secret
    amounts = [randomness.randint(1, 9_00) for _ in range(120)]

    def operation(index: int, amount: int) -> Callable[[AsyncSession], Awaitable[int]]:
        async def work(session: AsyncSession) -> int:
            if index % 3 == 0:
                await ledger.post_entry(
                    session,
                    EntryDraft(
                        "deposit",
                        "mix",
                        str(new_id()),
                        (debit(settlement, amount), credit(user.available, amount)),
                    ),
                )
                return amount
            try:
                await ledger.post_entry(
                    session,
                    EntryDraft(
                        "transfer",
                        "mix",
                        str(new_id()),
                        (debit(user.available, amount), credit(sink, amount)),
                    ),
                )
            except InsufficientFunds:
                return 0
            return -amount

        return work

    changes = await asyncio.gather(
        *(db.run(operation(i, amount)) for i, amount in enumerate(amounts))
    )

    async with db.transaction() as session:
        balance = await ledger.get_balance(session, user.available)
        lines = await ledger.statement(session, user.available, limit=200)
    assert balance == 20_00 + sum(changes)
    assert balance >= 0
    # The running balance never dipped below zero at any point in the account's history.
    assert all(line.balance_after is not None and line.balance_after >= 0 for line in lines)


async def test_a_debit_stuck_behind_a_held_lock_gives_up_instead_of_waiting_forever(
    db: Database,
) -> None:
    async with db.transaction() as session:
        sender = await funded_user(session, 10_00)
        recipient = await open_user(session)

    async with db.transaction() as holder:
        await holder.execute(
            text("SELECT 1 FROM account_balances WHERE account_id = :account FOR UPDATE"),
            {"account": sender.available},
        )
        with pytest.raises(DBAPIError) as failure:
            async with db.transaction() as blocked:
                await blocked.execute(text("SET LOCAL lock_timeout = '100ms'"))
                await ledger.post_entry(
                    blocked, transfer_draft(sender.available, recipient.available, 1_00)
                )

    assert sqlstate_of(failure.value) == LOCK_NOT_AVAILABLE
    async with db.transaction() as session:
        assert await ledger.get_balance(session, sender.available) == 10_00


async def test_posting_locks_balance_rows_for_no_key_update(db: Database) -> None:
    async with db.transaction() as session:
        sender = await funded_user(session, 10_00)
        recipient = await open_user(session)

    statements: list[str] = []

    def record(_connection: object, _cursor: object, statement: str, *_rest: object) -> None:
        statements.append(statement)

    event.listen(db.engine.sync_engine, "before_cursor_execute", record)
    try:
        async with db.transaction() as session:
            await ledger.post_entry(
                session, transfer_draft(sender.available, recipient.available, 1_00)
            )
    finally:
        event.remove(db.engine.sync_engine, "before_cursor_execute", record)

    locking = [statement for statement in statements if " FOR " in statement]
    assert len(locking) == 1
    assert locking[0].endswith("ORDER BY account_balances.account_id FOR NO KEY UPDATE")


async def test_an_open_posting_does_not_block_a_foreign_key_check_on_its_balance_rows(
    db: Database,
) -> None:
    """A row that references a balance row takes FOR KEY SHARE on it. Posting never changes
    the key, so that check goes through while the posting is open; a second writer waits."""
    async with db.transaction() as session:
        sender = await funded_user(session, 10_00)
        recipient = await open_user(session)

    row = "SELECT 1 FROM account_balances WHERE account_id = :account"

    async def lock(statement: str) -> str | None:
        try:
            async with db.transaction() as other:
                await other.execute(text(statement), {"account": sender.available})
        except DBAPIError as error:
            return sqlstate_of(error)
        return None

    async with db.transaction() as session:
        await ledger.post_entry(
            session, transfer_draft(sender.available, recipient.available, 1_00)
        )
        assert await lock(row + " FOR KEY SHARE NOWAIT") is None
        assert await lock(row + " FOR NO KEY UPDATE NOWAIT") == LOCK_NOT_AVAILABLE
