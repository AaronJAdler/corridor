"""A transfer between two users: what moves, what is recorded, and what is refused."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from corridor import audit, identity, ledger, payments, wallets
from corridor.identity import InsufficientScope, Scope, User
from corridor.ledger import Direction, InsufficientFunds
from corridor.platform.clock import ManualClock
from corridor.platform.config import Settings
from corridor.platform.db import (
    LOCK_NOT_AVAILABLE,
    Database,
    advisory_xact_lock,
    lock_key,
    sqlstate_of,
)
from corridor.platform.ids import new_id
from corridor.platform.money import InvalidAmount, UnknownAsset
from corridor.risk import UserRestricted
from tests.identity.support import add_user, close_account
from tests.payments.support import (
    acting_as,
    agent_of,
    available,
    count,
    deposit,
    fee_revenue,
    rows,
    send,
)


async def test_a_transfer_moves_exactly_the_amount_from_sender_to_recipient(
    db: Database, settings: Settings, maria: User, joao: User
) -> None:
    await deposit(db, maria, 100_00)

    await send(db, settings, maria, joao, 30_00)

    assert await available(db, maria) == 70_00
    assert await available(db, joao) == 30_00
    assert await fee_revenue(db) == 0


async def test_a_transfer_says_what_happened(
    db: Database, settings: Settings, maria: User, joao: User, clock: ManualClock
) -> None:
    await deposit(db, maria, 100_00)
    transfer_id = new_id()

    transfer = await send(
        db, settings, maria, joao, 30_00, memo="for lunch", transfer_id=transfer_id
    )

    assert transfer.id == transfer_id
    assert (transfer.sender_id, transfer.recipient_id) == (maria.id, joao.id)
    assert (transfer.asset, transfer.amount, transfer.fee) == ("USD", 30_00, 0)
    assert transfer.status == "completed"
    assert transfer.memo == "for lunch"
    assert (transfer.initiated_by_type, transfer.initiated_by_id) == ("user", maria.id)
    assert transfer.created_at == clock.now()


async def test_the_fee_is_paid_by_the_sender_on_top_of_the_amount(
    db: Database, with_fee: Settings, maria: User, joao: User
) -> None:
    await deposit(db, maria, 100_00)

    transfer = await send(db, with_fee, maria, joao, 30_00)

    assert transfer.fee == 30
    assert await available(db, maria) == 100_00 - 30_00 - 30
    assert await available(db, joao) == 30_00
    assert await fee_revenue(db) == 30


async def test_a_fee_is_earned_in_the_asset_that_was_sent(
    db: Database, with_fee: Settings, maria: User, joao: User
) -> None:
    await deposit(db, maria, 5_000_000, "USDC")

    await send(db, with_fee, maria, joao, 2_000_000, asset="USDC")

    assert await fee_revenue(db, "USDC") == 20_000
    assert await fee_revenue(db, "USD") == 0
    assert await available(db, maria, "USDC") == 5_000_000 - 2_020_000


async def test_a_fee_free_transfer_is_an_entry_of_two_postings(
    db: Database, settings: Settings, maria: User, joao: User
) -> None:
    await deposit(db, maria, 10_00)

    transfer = await send(db, settings, maria, joao, 4_00)

    async with db.transaction() as session:
        entry = await ledger.get_entry(session, transfer.entry_id)
    assert (entry.kind, entry.source_type, entry.source_id) == (
        "transfer",
        "transfer",
        str(transfer.id),
    )
    assert sorted((p.direction, p.amount) for p in entry.postings) == [
        (Direction.CREDIT, 4_00),
        (Direction.DEBIT, 4_00),
    ]


async def test_a_transfer_with_a_fee_is_an_entry_of_three_postings(
    db: Database, with_fee: Settings, maria: User, joao: User
) -> None:
    await deposit(db, maria, 10_00)

    transfer = await send(db, with_fee, maria, joao, 4_00)

    async with db.transaction() as session:
        entry = await ledger.get_entry(session, transfer.entry_id)
    assert sorted((p.direction, p.amount) for p in entry.postings) == [
        (Direction.CREDIT, 4),
        (Direction.CREDIT, 4_00),
        (Direction.DEBIT, 4_04),
    ]


async def test_the_whole_balance_can_be_sent_when_there_is_no_fee(
    db: Database, settings: Settings, maria: User, joao: User
) -> None:
    await deposit(db, maria, 12_34)

    await send(db, settings, maria, joao, 12_34)

    assert (await available(db, maria), await available(db, joao)) == (0, 12_34)


@pytest.mark.parametrize("address", ["joao", "@joao", "  @JOAO ", "joao@example.com"])
async def test_a_recipient_is_addressed_by_handle_or_email(
    db: Database, settings: Settings, maria: User, joao: User, address: str
) -> None:
    await deposit(db, maria, 10_00)

    transfer = await send(db, settings, maria, address, 1_00)

    assert transfer.recipient_id == joao.id


async def test_one_transfer_writes_one_entry_one_row_one_outbox_event_and_one_audit_event(
    db: Database, settings: Settings, maria: User, joao: User
) -> None:
    await deposit(db, maria, 100_00)
    entries_before = await count(db, "journal_entries")

    transfer = await send(db, settings, maria, joao, 30_00)

    assert await count(db, "journal_entries") == entries_before + 1
    assert await count(db, "transfers") == 1
    assert await rows(db, "SELECT topic, payload FROM outbox_events") == [
        {
            "topic": "transfer.completed",
            "payload": {
                "transfer_id": str(transfer.id),
                "sender_id": str(maria.id),
                "recipient_id": str(joao.id),
                "entry_id": str(transfer.entry_id),
                "asset": "USD",
                "amount": "3000",
                "fee": "0",
            },
        }
    ]
    async with db.transaction() as session:
        events = await audit.list_events(session, action="transfer.created")
    assert len(events) == 1
    event = events[0]
    assert (event.actor_type, event.actor_id) == ("user", str(maria.id))
    assert event.principal_id == maria.id
    assert (event.resource_type, event.resource_id) == ("transfer", str(transfer.id))
    assert event.outcome == "success"
    assert event.details == {
        "recipient_id": str(joao.id),
        "asset": "USD",
        "amount": "3000",
        "fee": "0",
    }


async def test_a_transfer_an_agent_makes_is_recorded_as_the_agents_for_its_owner(
    db: Database, settings: Settings, maria: User, joao: User
) -> None:
    await deposit(db, maria, 10_00)
    agent = agent_of(maria, Scope.TRANSFERS_CREATE)

    transfer = await send(db, settings, maria, joao, 1_00, principal=agent)

    assert transfer.sender_id == maria.id
    assert (transfer.initiated_by_type, transfer.initiated_by_id) == ("agent", agent.actor_id)
    async with db.transaction() as session:
        (event,) = await audit.list_events(session, action="transfer.created")
    assert (event.actor_type, event.actor_id) == ("agent", str(agent.actor_id))
    assert event.principal_id == maria.id


async def test_everything_is_undone_together_when_the_last_step_fails(
    db: Database,
    settings: Settings,
    maria: User,
    joao: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await deposit(db, maria, 100_00)
    entries_before = await count(db, "journal_entries")

    async def broken(*args: object, **kwargs: object) -> uuid.UUID:
        raise RuntimeError("the audit log is unavailable")

    monkeypatch.setattr(audit, "record", broken)

    with pytest.raises(RuntimeError, match="audit log"):
        await send(db, settings, maria, joao, 30_00)

    assert await count(db, "journal_entries") == entries_before
    assert await count(db, "transfers") == 0
    assert await count(db, "outbox_events") == 0
    assert (await available(db, maria), await available(db, joao)) == (100_00, 0)


async def test_a_transfer_to_oneself_is_refused(
    db: Database, settings: Settings, maria: User
) -> None:
    await deposit(db, maria, 10_00)

    with pytest.raises(payments.CannotTransferToSelf) as refusal:
        await send(db, settings, maria, "@maria", 1_00)

    assert (refusal.value.status, refusal.value.code) == (422, "transfer_to_self")
    assert await count(db, "transfers") == 0
    assert await available(db, maria) == 10_00


async def test_a_transfer_to_nobody_is_refused(
    db: Database, settings: Settings, maria: User
) -> None:
    await deposit(db, maria, 10_00)

    with pytest.raises(payments.RecipientNotFound) as refusal:
        await send(db, settings, maria, "@nobody", 1_00)

    assert (refusal.value.status, refusal.value.code) == (404, "recipient_not_found")
    assert await available(db, maria) == 10_00


async def test_a_closed_account_cannot_be_sent_money(
    db: Database, settings: Settings, maria: User, joao: User
) -> None:
    await deposit(db, maria, 10_00)
    async with db.transaction() as session:
        await close_account(session, joao.id)

    with pytest.raises(payments.RecipientNotFound):
        await send(db, settings, maria, joao, 1_00)


async def test_a_transfer_the_balance_cannot_cover_is_refused_and_writes_nothing(
    db: Database, settings: Settings, maria: User, joao: User
) -> None:
    await deposit(db, maria, 10_00)
    entries_before = await count(db, "journal_entries")

    with pytest.raises(InsufficientFunds) as refusal:
        await send(db, settings, maria, joao, 10_01)

    assert refusal.value.status == 402
    assert await count(db, "journal_entries") == entries_before
    assert await count(db, "transfers") == 0
    assert await count(db, "outbox_events") == 0
    async with db.transaction() as session:
        assert await audit.list_events(session, action="transfer.created") == []
    assert (await available(db, maria), await available(db, joao)) == (10_00, 0)


async def test_the_balance_has_to_cover_the_fee_as_well(
    db: Database, with_fee: Settings, maria: User, joao: User
) -> None:
    await deposit(db, maria, 100_00)

    with pytest.raises(InsufficientFunds):
        await send(db, with_fee, maria, joao, 100_00)

    assert await available(db, maria) == 100_00
    assert await fee_revenue(db) == 0


async def test_a_refused_transfer_leaves_the_callers_transaction_usable(
    db: Database, settings: Settings, maria: User, joao: User
) -> None:
    await deposit(db, maria, 10_00)

    async with db.transaction() as session:
        with pytest.raises(InsufficientFunds):
            await payments.create_transfer(
                session,
                acting_as(maria),
                transfer_id=new_id(),
                recipient="@joao",
                asset="USD",
                amount=99_00,
                memo=None,
                settings=settings,
            )
        # The handler records the refusal on the idempotency key in this same transaction.
        assert (await session.execute(text("SELECT 1"))).scalar_one() == 1


async def test_a_restricted_user_cannot_send(
    db: Database, settings: Settings, maria: User, joao: User
) -> None:
    await deposit(db, maria, 10_00)
    async with db.transaction() as session:
        await identity.restrict_user(session, maria.id, "review")

    with pytest.raises(UserRestricted) as refusal:
        await send(db, settings, maria, joao, 1_00)

    assert (refusal.value.status, refusal.value.code) == (403, "user_restricted")
    assert await count(db, "transfers") == 0
    assert await available(db, maria) == 10_00


async def test_a_restricted_user_can_still_receive(
    db: Database, settings: Settings, maria: User, joao: User
) -> None:
    await deposit(db, maria, 10_00)
    async with db.transaction() as session:
        await identity.restrict_user(session, joao.id, "review")

    await send(db, settings, maria, joao, 1_00)

    assert await available(db, joao) == 1_00


async def test_a_credential_without_the_transfers_create_scope_cannot_send(
    db: Database, settings: Settings, maria: User, joao: User
) -> None:
    await deposit(db, maria, 10_00)
    reader = agent_of(maria, Scope.TRANSFERS_READ, Scope.WALLET_READ)

    with pytest.raises(InsufficientScope):
        await send(db, settings, maria, joao, 1_00, principal=reader)

    assert await count(db, "transfers") == 0
    assert await available(db, maria) == 10_00


async def test_a_credential_without_the_scope_learns_nothing_about_the_recipient(
    db: Database, settings: Settings, maria: User
) -> None:
    reader = agent_of(maria, Scope.TRANSFERS_READ)

    # The scope is checked first: not "recipient not found".
    with pytest.raises(InsufficientScope):
        await send(db, settings, maria, "@nobody", 1_00, principal=reader)


@pytest.mark.parametrize("amount", [0, -1, -100_00])
async def test_an_amount_that_is_not_positive_is_refused(
    db: Database, settings: Settings, maria: User, joao: User, amount: int
) -> None:
    await deposit(db, maria, 10_00)

    with pytest.raises(InvalidAmount):
        await send(db, settings, maria, joao, amount)

    assert (await available(db, maria), await available(db, joao)) == (10_00, 0)


async def test_an_unsupported_asset_is_refused(
    db: Database, settings: Settings, maria: User, joao: User
) -> None:
    with pytest.raises(UnknownAsset):
        await send(db, settings, maria, joao, 1_00, asset="EUR")


async def test_a_memo_of_140_characters_is_kept(
    db: Database, settings: Settings, maria: User, joao: User
) -> None:
    await deposit(db, maria, 10_00)

    transfer = await send(db, settings, maria, joao, 1_00, memo="m" * 140)

    assert transfer.memo == "m" * 140


async def test_a_memo_longer_than_140_characters_is_refused(
    db: Database, settings: Settings, maria: User, joao: User
) -> None:
    await deposit(db, maria, 10_00)

    with pytest.raises(payments.InvalidMemo) as refusal:
        await send(db, settings, maria, joao, 1_00, memo="m" * 141)

    assert (refusal.value.status, refusal.value.code) == (422, "invalid_memo")
    assert await available(db, maria) == 10_00


async def test_a_transfer_id_that_was_already_used_is_a_broken_contract_not_a_second_transfer(
    db: Database, settings: Settings, maria: User, joao: User
) -> None:
    await deposit(db, maria, 10_00)
    first = await send(db, settings, maria, joao, 1_00)

    with pytest.raises(payments.DuplicateTransfer) as failure:
        await send(db, settings, maria, joao, 1_00, transfer_id=first.id)

    assert not isinstance(failure.value, IntegrityError)
    assert (await available(db, maria), await available(db, joao)) == (9_00, 1_00)
    assert await count(db, "transfers") == 1


async def test_two_transfers_by_one_sender_in_different_assets_are_serialised(
    db: Database, settings: Settings, maria: User, joao: User
) -> None:
    """They share no balance row, so only the sender's money-out lock can make the second
    wait for the first."""
    await deposit(db, maria, 10_00, "USD")
    await deposit(db, maria, 10_00, "MXN")

    async with db.transaction() as first:
        await payments.create_transfer(
            first,
            acting_as(maria),
            transfer_id=new_id(),
            recipient="@joao",
            asset="USD",
            amount=1_00,
            memo=None,
            settings=settings,
        )
        with pytest.raises(DBAPIError) as failure:
            async with db.transaction() as second:
                await second.execute(text("SET LOCAL lock_timeout = '100ms'"))
                await payments.create_transfer(
                    second,
                    acting_as(maria),
                    transfer_id=new_id(),
                    recipient="@joao",
                    asset="MXN",
                    amount=1_00,
                    memo=None,
                    settings=settings,
                )

    assert sqlstate_of(failure.value) == LOCK_NOT_AVAILABLE
    assert await available(db, maria, "USD") == 9_00
    assert await available(db, maria, "MXN") == 10_00


async def test_a_transfer_waits_for_the_senders_money_out_lock(
    db: Database, settings: Settings, maria: User, joao: User
) -> None:
    await deposit(db, maria, 10_00)

    async with db.transaction() as holder:
        await advisory_xact_lock(holder, [lock_key("money_out", maria.id)])
        with pytest.raises(DBAPIError) as failure:
            async with db.transaction() as blocked:
                await blocked.execute(text("SET LOCAL lock_timeout = '100ms'"))
                await payments.create_transfer(
                    blocked,
                    acting_as(maria),
                    transfer_id=new_id(),
                    recipient="@joao",
                    asset="USD",
                    amount=1_00,
                    memo=None,
                    settings=settings,
                )

    assert sqlstate_of(failure.value) == LOCK_NOT_AVAILABLE
    assert await available(db, maria) == 10_00


async def test_receiving_does_not_wait_for_the_recipients_money_out_lock(
    db: Database, settings: Settings, maria: User, joao: User
) -> None:
    await deposit(db, maria, 10_00)

    async with db.transaction() as holder:
        await advisory_xact_lock(holder, [lock_key("money_out", joao.id)])
        async with db.transaction() as session:
            await session.execute(text("SET LOCAL lock_timeout = '100ms'"))
            await payments.create_transfer(
                session,
                acting_as(maria),
                transfer_id=new_id(),
                recipient="@joao",
                asset="USD",
                amount=1_00,
                memo=None,
                settings=settings,
            )

    assert await available(db, joao) == 1_00


async def test_a_refused_scope_or_recipient_never_takes_the_lock(
    db: Database, settings: Settings, maria: User
) -> None:
    async with db.transaction() as holder:
        await advisory_xact_lock(holder, [lock_key("money_out", maria.id)])
        async with db.transaction() as session:
            await session.execute(text("SET LOCAL lock_timeout = '100ms'"))
            with pytest.raises(payments.RecipientNotFound):
                await payments.create_transfer(
                    session,
                    acting_as(maria),
                    transfer_id=new_id(),
                    recipient="@nobody",
                    asset="USD",
                    amount=1_00,
                    memo=None,
                    settings=settings,
                )


async def test_a_user_registered_without_wallets_cannot_be_sent_money(
    db: Database, settings: Settings, maria: User
) -> None:
    await deposit(db, maria, 10_00)
    async with db.transaction() as session:
        await add_user(session, "bare")

    with pytest.raises(wallets.WalletNotFound):
        await send(db, settings, maria, "@bare", 1_00)

    assert await available(db, maria) == 10_00
