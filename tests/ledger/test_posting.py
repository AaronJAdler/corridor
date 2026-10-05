"""Posting journal entries through the ledger's service."""

import asyncio
import dataclasses
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import ledger
from corridor.ledger import (
    AccountKind,
    ConflictingEntry,
    Direction,
    EntryDraft,
    EntryNotFound,
    InsufficientFunds,
    InvalidEntry,
    PostingDraft,
    UnknownAccount,
    credit,
    debit,
)
from corridor.platform.clock import ManualClock
from corridor.platform.db import Database
from corridor.platform.ids import new_id
from corridor.platform.money import UnknownAsset
from tests.support.ledger import fund, funded_user, open_user, system_account, transfer_draft


async def rows(session: AsyncSession, table: str) -> int:
    return int((await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one())  # noqa: S608


# --- accounts --------------------------------------------------------------------------------


async def test_opening_a_user_account_gives_it_a_zero_cached_balance(db: Database) -> None:
    owner = new_id()
    async with db.transaction() as session:
        account = await ledger.open_account(
            session, AccountKind.USER_AVAILABLE, "USD", owner_id=owner
        )

        assert (account.kind, account.asset_code, account.owner_id) == (
            AccountKind.USER_AVAILABLE,
            "USD",
            owner,
        )
        assert (account.normal_side, account.constrained) == (Direction.CREDIT, True)
        assert await ledger.get_balance(session, account.id) == 0
        assert await rows(session, "account_balances") == 1


async def test_a_system_account_has_no_cached_balance_row(db: Database) -> None:
    async with db.transaction() as session:
        account = await ledger.open_account(session, AccountKind.FEE_REVENUE, "USD")

        assert account.constrained is False
        assert await rows(session, "account_balances") == 0
        assert await ledger.get_balance(session, account.id) == 0


async def test_opening_the_same_account_again_returns_it(db: Database) -> None:
    owner = new_id()
    async with db.transaction() as session:
        first = await ledger.open_account(
            session, AccountKind.USER_AVAILABLE, "USD", owner_id=owner
        )
    async with db.transaction() as session:
        again = await ledger.open_account(
            session, AccountKind.USER_AVAILABLE, "USD", owner_id=owner
        )
        other_asset = await ledger.open_account(
            session, AccountKind.USER_AVAILABLE, "MXN", owner_id=owner
        )

        assert again == first
        assert other_asset.id != first.id
        assert await rows(session, "ledger_accounts") == 2
        assert await rows(session, "account_balances") == 2


async def test_opening_one_account_from_many_requests_at_once_creates_it_once(db: Database) -> None:
    owner = new_id()

    async def open_it(session: AsyncSession) -> uuid.UUID:
        return (
            await ledger.open_account(session, AccountKind.USER_AVAILABLE, "USD", owner_id=owner)
        ).id

    ids = await asyncio.gather(*(db.run(open_it) for _ in range(20)))

    assert len(set(ids)) == 1
    async with db.transaction() as session:
        assert await rows(session, "ledger_accounts") == 1
        assert await rows(session, "account_balances") == 1


@pytest.mark.parametrize(
    ("kind", "owner", "provider"),
    [
        (AccountKind.USER_AVAILABLE, False, None),
        (AccountKind.USER_AVAILABLE, True, "simbank"),
        (AccountKind.BANK_SETTLEMENT, False, None),
        (AccountKind.BANK_SETTLEMENT, True, "simbank"),
        (AccountKind.FEE_REVENUE, True, None),
        (AccountKind.FEE_REVENUE, False, "simbank"),
    ],
)
async def test_an_account_is_opened_with_the_identity_its_kind_requires(
    db: Database, kind: AccountKind, owner: bool, provider: str | None
) -> None:
    async with db.transaction() as session:
        with pytest.raises(InvalidEntry):
            await ledger.open_account(
                session, kind, "USD", owner_id=new_id() if owner else None, provider=provider
            )


async def test_an_account_cannot_be_opened_in_an_unknown_asset(db: Database) -> None:
    async with db.transaction() as session:
        with pytest.raises(UnknownAsset):
            await ledger.open_account(session, AccountKind.FEE_REVENUE, "DOGE")


async def test_accounts_are_found_and_listed_by_owner(db: Database) -> None:
    async with db.transaction() as session:
        maria = await open_user(session, "USD")
        await open_user(session, "MXN", owner=maria.owner)
        await open_user(session, "USD")

        found = await ledger.find_account(
            session, AccountKind.USER_HELD, "USD", owner_id=maria.owner
        )
        assert found is not None
        assert found.id == maria.held
        assert (
            await ledger.find_account(session, AccountKind.USER_HELD, "BRL", owner_id=maria.owner)
            is None
        )
        assert await ledger.find_account(session, AccountKind.SUSPENSE, "USD") is None

        listed = await ledger.list_accounts(session, owner_id=maria.owner)
        assert [(account.asset_code, account.kind) for account in listed] == [
            ("MXN", AccountKind.USER_AVAILABLE),
            ("MXN", AccountKind.USER_HELD),
            ("USD", AccountKind.USER_AVAILABLE),
            ("USD", AccountKind.USER_HELD),
        ]


# --- posting ---------------------------------------------------------------------------------


async def test_a_deposit_credits_the_user_and_debits_settlement(
    db: Database, clock: ManualClock
) -> None:
    async with db.transaction() as session:
        user = await open_user(session)
        settlement = await system_account(session, AccountKind.BANK_SETTLEMENT)

        entry = await ledger.post_entry(
            session,
            EntryDraft(
                kind="deposit",
                source_type="deposit",
                source_id="dep_1",
                postings=(debit(settlement, 12_50), credit(user.available, 12_50)),
                metadata={"provider_reference": "TX-1"},
            ),
        )

    assert entry.created is True
    assert (entry.kind, entry.source_type, entry.source_id) == ("deposit", "deposit", "dep_1")
    assert entry.metadata == {"provider_reference": "TX-1"}
    assert entry.posted_at == clock.now()
    by_account = {posting.account_id: posting for posting in entry.postings}
    assert (by_account[user.available].direction, by_account[user.available].amount) == (
        Direction.CREDIT,
        12_50,
    )
    # A constrained account records its balance after the posting; a system account has none.
    assert by_account[user.available].balance_after == 12_50
    assert by_account[settlement].balance_after is None
    assert {posting.asset_code for posting in entry.postings} == {"USD"}

    async with db.transaction() as session:
        assert await ledger.get_balances(session, [user.available, user.held, settlement]) == {
            user.available: 12_50,
            user.held: 0,
            settlement: 12_50,
        }
        assert await ledger.get_entry(session, entry.id) == dataclasses.replace(
            entry, created=False
        )


async def test_a_transfer_with_a_fee_moves_exact_amounts(db: Database) -> None:
    async with db.transaction() as session:
        sender = await funded_user(session, 100_00)
        recipient = await open_user(session)
        fees = await system_account(session, AccountKind.FEE_REVENUE)

        entry = await ledger.post_entry(
            session,
            EntryDraft(
                kind="transfer",
                source_type="transfer",
                source_id="tr_1",
                postings=(
                    debit(sender.available, 25_30),
                    credit(recipient.available, 25_00),
                    credit(fees, 30),
                ),
            ),
        )

        assert await ledger.get_balances(
            session, [sender.available, recipient.available, fees]
        ) == {
            sender.available: 74_70,
            recipient.available: 25_00,
            fees: 30,
        }
        after = {posting.account_id: posting.balance_after for posting in entry.postings}
        assert after == {sender.available: 74_70, recipient.available: 25_00, fees: None}


async def test_a_balance_can_be_spent_down_to_exactly_zero(db: Database) -> None:
    async with db.transaction() as session:
        sender = await funded_user(session, 10_00)
        recipient = await open_user(session)

        await ledger.post_entry(
            session, transfer_draft(sender.available, recipient.available, 10_00)
        )

        assert await ledger.get_balance(session, sender.available) == 0


async def test_a_hold_moves_money_between_two_of_the_users_accounts(db: Database) -> None:
    async with db.transaction() as session:
        user = await funded_user(session, 80_00)

        entry = await ledger.post_entry(
            session,
            EntryDraft(
                kind="withdrawal_hold",
                source_type="withdrawal",
                source_id="wd_1",
                postings=(debit(user.available, 30_00), credit(user.held, 30_00)),
            ),
        )

        assert await ledger.get_balances(session, [user.available, user.held]) == {
            user.available: 50_00,
            user.held: 30_00,
        }
        assert {posting.account_id: posting.balance_after for posting in entry.postings} == {
            user.available: 50_00,
            user.held: 30_00,
        }


async def test_a_conversion_is_one_entry_that_balances_in_each_asset(db: Database) -> None:
    async with db.transaction() as session:
        usd = await funded_user(session, 100_00, "USD")
        usdc = await open_user(session, "USDC", owner=usd.owner)
        usd_position = await system_account(session, AccountKind.FX_POSITION, "USD")
        usdc_position = await system_account(session, AccountKind.FX_POSITION, "USDC")

        await ledger.post_entry(
            session,
            EntryDraft(
                kind="conversion",
                source_type="fx_conversion",
                source_id="cv_1",
                postings=(
                    debit(usd.available, 10_00),
                    credit(usd_position, 10_00),
                    debit(usdc_position, 9_990_000),
                    credit(usdc.available, 9_990_000),
                ),
            ),
        )

        assert await ledger.get_balances(
            session, [usd.available, usdc.available, usd_position, usdc_position]
        ) == {
            usd.available: 90_00,
            usdc.available: 9_990_000,
            usd_position: -10_00,  # the operator now holds 10.00 USD less of customer liability
            usdc_position: 9_990_000,
        }


async def test_a_receivable_grows_with_debits_and_cannot_be_overpaid(db: Database) -> None:
    async with db.transaction() as session:
        owner = new_id()
        receivable = (
            await ledger.open_account(session, AccountKind.USER_RECEIVABLE, "USD", owner_id=owner)
        ).id
        settlement = await system_account(session, AccountKind.BANK_SETTLEMENT)

        owed = await ledger.post_entry(
            session,
            EntryDraft(
                "deposit_return",
                "deposit",
                "dep_9",
                (debit(receivable, 40_00), credit(settlement, 40_00)),
            ),
        )
        assert {p.account_id: p.balance_after for p in owed.postings}[receivable] == 40_00

        with pytest.raises(InsufficientFunds):
            await ledger.post_entry(
                session,
                EntryDraft(
                    "repayment",
                    "repayment",
                    "rp_1",
                    (debit(settlement, 40_01), credit(receivable, 40_01)),
                ),
            )
        assert await ledger.get_balance(session, receivable) == 40_00


async def test_very_large_amounts_are_exact(db: Database) -> None:
    huge = 10**30 + 7
    async with db.transaction() as session:
        sender = await funded_user(session, huge)
        recipient = await open_user(session)

        await ledger.post_entry(
            session, transfer_draft(sender.available, recipient.available, huge - 1)
        )

        assert await ledger.get_balances(session, [sender.available, recipient.available]) == {
            sender.available: 1,
            recipient.available: huge - 1,
        }


async def test_nothing_is_kept_if_the_callers_transaction_rolls_back(db: Database) -> None:
    async with db.transaction() as session:
        sender = await funded_user(session, 50_00)
        recipient = await open_user(session)

    with pytest.raises(RuntimeError, match="later step failed"):
        async with db.transaction() as session:
            await ledger.post_entry(
                session, transfer_draft(sender.available, recipient.available, 20_00)
            )
            raise RuntimeError("later step failed")

    async with db.transaction() as session:
        assert await ledger.get_balance(session, sender.available) == 50_00
        assert await rows(session, "journal_entries") == 1  # only the funding entry


# --- insufficient funds ----------------------------------------------------------------------


async def test_insufficient_funds_is_refused_before_anything_is_written(db: Database) -> None:
    async with db.transaction() as session:
        sender = await funded_user(session, 12_50)
        recipient = await open_user(session)
        entries, postings = await rows(session, "journal_entries"), await rows(session, "postings")

        with pytest.raises(InsufficientFunds) as refusal:
            await ledger.post_entry(
                session, transfer_draft(sender.available, recipient.available, 20_00)
            )

        assert refusal.value.code == "insufficient_funds"
        assert refusal.value.detail == "Available balance is 12.50 USD; 20.00 USD is required."
        assert (refusal.value.balance, refusal.value.required) == (12_50, 20_00)
        assert (await rows(session, "journal_entries"), await rows(session, "postings")) == (
            entries,
            postings,
        )
        assert await ledger.get_balances(session, [sender.available, recipient.available]) == {
            sender.available: 12_50,
            recipient.available: 0,
        }


async def test_a_refusal_leaves_the_callers_transaction_usable(db: Database) -> None:
    async with db.transaction() as session:
        sender = await funded_user(session, 12_50)
        recipient = await open_user(session)

        with pytest.raises(InsufficientFunds):
            await ledger.post_entry(
                session, transfer_draft(sender.available, recipient.available, 20_00)
            )
        # The same transaction goes on to do something else, as an API handler does when it
        # records the refusal against the idempotency key.
        await ledger.post_entry(
            session, transfer_draft(sender.available, recipient.available, 5_00)
        )

    async with db.transaction() as session:
        assert await ledger.get_balance(session, sender.available) == 7_50


async def test_one_unaffordable_posting_refuses_the_whole_entry(db: Database) -> None:
    async with db.transaction() as session:
        rich = await funded_user(session, 100_00)
        poor = await funded_user(session, 1_00)
        sink = await system_account(session, AccountKind.SUSPENSE)

        with pytest.raises(InsufficientFunds) as refusal:
            await ledger.post_entry(
                session,
                EntryDraft(
                    "adjustment",
                    "adjustment",
                    "adj_1",
                    (
                        debit(rich.available, 10_00),
                        debit(poor.available, 2_00),
                        credit(sink, 12_00),
                    ),
                ),
            )

        assert refusal.value.account_id == poor.available
        assert await ledger.get_balance(session, rich.available) == 100_00


# --- one event, one entry --------------------------------------------------------------------


async def test_posting_the_same_event_again_returns_the_first_entry(db: Database) -> None:
    async with db.transaction() as session:
        sender = await funded_user(session, 50_00)
        recipient = await open_user(session)
        draft = transfer_draft(sender.available, recipient.available, 20_00, source_id="tr_dup")
        first = await ledger.post_entry(session, draft)

    async with db.transaction() as session:
        again = await ledger.post_entry(session, draft)

        assert again.created is False
        assert again.id == first.id
        assert [p.seq for p in again.postings] == [p.seq for p in first.postings]
        assert await ledger.get_balance(session, sender.available) == 30_00
        assert await rows(session, "journal_entries") == 2  # funding + one transfer


async def test_a_replay_is_recognised_even_when_the_balance_could_no_longer_afford_it(
    db: Database,
) -> None:
    async with db.transaction() as session:
        sender = await funded_user(session, 20_00)
        recipient = await open_user(session)
        draft = transfer_draft(sender.available, recipient.available, 20_00, source_id="tr_all")
        first = await ledger.post_entry(session, draft)

    # The balance is now zero. A naive implementation would check funds first and refuse.
    async with db.transaction() as session:
        again = await ledger.post_entry(session, draft)

    assert again.id == first.id
    assert again.created is False


async def test_the_same_event_with_different_postings_is_a_conflict(db: Database) -> None:
    async with db.transaction() as session:
        sender = await funded_user(session, 50_00)
        recipient = await open_user(session)
        await ledger.post_entry(
            session, transfer_draft(sender.available, recipient.available, 20_00, source_id="tr_x")
        )

        with pytest.raises(ConflictingEntry, match="different postings"):
            await ledger.post_entry(
                session,
                transfer_draft(sender.available, recipient.available, 20_01, source_id="tr_x"),
            )

        assert await ledger.get_balance(session, sender.available) == 30_00


async def test_the_same_source_may_post_entries_of_different_kinds(db: Database) -> None:
    # A withdrawal posts a hold and later a settle or a release, all for one withdrawal id.
    async with db.transaction() as session:
        user = await funded_user(session, 50_00)

        hold = await ledger.post_entry(
            session,
            EntryDraft(
                "withdrawal_hold",
                "withdrawal",
                "wd_7",
                (debit(user.available, 10_00), credit(user.held, 10_00)),
            ),
        )
        release = await ledger.post_entry(
            session,
            EntryDraft(
                "withdrawal_release",
                "withdrawal",
                "wd_7",
                (debit(user.held, 10_00), credit(user.available, 10_00)),
            ),
        )

        assert hold.id != release.id
        assert await ledger.get_balances(session, [user.available, user.held]) == {
            user.available: 50_00,
            user.held: 0,
        }


async def test_entries_can_be_found_by_event_and_by_id(db: Database) -> None:
    async with db.transaction() as session:
        sender = await funded_user(session, 50_00)
        recipient = await open_user(session)
        posted = await ledger.post_entry(
            session,
            transfer_draft(sender.available, recipient.available, 1_00, source_id="tr_find"),
        )

        found = await ledger.find_entry(session, "test_transfer", "tr_find", "transfer")
        assert found is not None
        assert found.id == posted.id
        assert found.postings == posted.postings
        assert await ledger.find_entry(session, "test_transfer", "tr_find", "reversal") is None
        assert (await ledger.get_entry(session, posted.id)).id == posted.id
        with pytest.raises(EntryNotFound):
            await ledger.get_entry(session, new_id())


# --- drafts the ledger will not post ---------------------------------------------------------


async def test_an_entry_that_does_not_balance_is_refused(db: Database) -> None:
    async with db.transaction() as session:
        sender = await funded_user(session, 50_00)
        recipient = await open_user(session)

        with pytest.raises(InvalidEntry, match="debits and credits differ in USD"):
            await ledger.post_entry(
                session,
                EntryDraft(
                    "transfer",
                    "transfer",
                    "tr_bad",
                    (debit(sender.available, 10_00), credit(recipient.available, 9_99)),
                ),
            )
        assert await ledger.get_balance(session, sender.available) == 50_00


async def test_amounts_in_different_assets_do_not_balance_each_other(db: Database) -> None:
    async with db.transaction() as session:
        usd = await funded_user(session, 50_00, "USD")
        mxn = await open_user(session, "MXN")

        with pytest.raises(InvalidEntry, match="MXN, USD"):
            await ledger.post_entry(
                session,
                EntryDraft(
                    "transfer",
                    "transfer",
                    "tr_fx",
                    (debit(usd.available, 10_00), credit(mxn.available, 10_00)),
                ),
            )


@pytest.mark.parametrize("amount", [0, -1, 10**38, 1.5, True, "10"])
async def test_a_posting_amount_must_be_a_positive_storable_int(
    db: Database, amount: object
) -> None:
    async with db.transaction() as session:
        sender = await funded_user(session, 50_00)
        recipient = await open_user(session)

        with pytest.raises(InvalidEntry):
            await ledger.post_entry(
                session,
                EntryDraft(
                    "transfer",
                    "transfer",
                    "tr_amt",
                    (
                        PostingDraft(sender.available, Direction.DEBIT, amount),  # type: ignore[arg-type]
                        PostingDraft(recipient.available, Direction.CREDIT, amount),  # type: ignore[arg-type]
                    ),
                ),
            )


async def test_an_entry_needs_two_postings_and_names_each_account_once(db: Database) -> None:
    async with db.transaction() as session:
        user = await funded_user(session, 50_00)

        with pytest.raises(InvalidEntry, match="at least two postings"):
            await ledger.post_entry(session, EntryDraft("x", "x", "1", (debit(user.available, 1),)))
        with pytest.raises(InvalidEntry, match="only once"):
            await ledger.post_entry(
                session,
                EntryDraft("x", "x", "2", (debit(user.available, 1), credit(user.available, 1))),
            )


@pytest.mark.parametrize(
    ("kind", "source_type", "source_id"), [("", "s", "1"), ("k", "", "1"), ("k", "s", "")]
)
async def test_an_entry_must_name_its_kind_and_its_source(
    db: Database, kind: str, source_type: str, source_id: str
) -> None:
    async with db.transaction() as session:
        user = await funded_user(session, 50_00)
        with pytest.raises(InvalidEntry, match="kind, a source type and a source id"):
            await ledger.post_entry(
                session,
                EntryDraft(
                    kind, source_type, source_id, (debit(user.available, 1), credit(user.held, 1))
                ),
            )


async def test_a_posting_to_an_unknown_account_is_refused(db: Database) -> None:
    async with db.transaction() as session:
        user = await funded_user(session, 50_00)
        with pytest.raises(UnknownAccount):
            await ledger.post_entry(session, transfer_draft(user.available, new_id(), 1_00))


# --- reversal --------------------------------------------------------------------------------


async def test_a_reversal_mirrors_the_entry_and_restores_the_balances(db: Database) -> None:
    async with db.transaction() as session:
        sender = await funded_user(session, 50_00)
        recipient = await open_user(session)
        original = await ledger.post_entry(
            session, transfer_draft(sender.available, recipient.available, 20_00)
        )

        reversal = await ledger.reverse_entry(
            session, original.id, metadata={"reason": "sent in error"}
        )

        assert reversal.created is True
        assert reversal.kind == "reversal"
        assert reversal.reverses_entry_id == original.id
        assert reversal.metadata == {"reason": "sent in error"}
        assert {(p.account_id, p.direction, p.amount) for p in reversal.postings} == {
            (sender.available, Direction.CREDIT, 20_00),
            (recipient.available, Direction.DEBIT, 20_00),
        }
        assert await ledger.get_balances(session, [sender.available, recipient.available]) == {
            sender.available: 50_00,
            recipient.available: 0,
        }
        # History grows; nothing in it was changed.
        assert await rows(session, "journal_entries") == 3


async def test_reversing_twice_returns_the_first_reversal(db: Database) -> None:
    async with db.transaction() as session:
        sender = await funded_user(session, 50_00)
        recipient = await open_user(session)
        original = await ledger.post_entry(
            session, transfer_draft(sender.available, recipient.available, 20_00)
        )
        first = await ledger.reverse_entry(session, original.id)

    async with db.transaction() as session:
        again = await ledger.reverse_entry(session, original.id)

        assert (again.id, again.created) == (first.id, False)
        assert await ledger.get_balance(session, sender.available) == 50_00


async def test_a_reversal_is_refused_if_the_money_has_been_spent(db: Database) -> None:
    async with db.transaction() as session:
        user = await open_user(session)
        other = await open_user(session)
        deposit = await fund(session, user.available, 30_00)
        await ledger.post_entry(session, transfer_draft(user.available, other.available, 25_00))

        with pytest.raises(InsufficientFunds):
            await ledger.reverse_entry(session, deposit.id)

        assert await ledger.get_balance(session, user.available) == 5_00


async def test_a_reversal_cannot_itself_be_reversed(db: Database) -> None:
    async with db.transaction() as session:
        sender = await funded_user(session, 50_00)
        recipient = await open_user(session)
        original = await ledger.post_entry(
            session, transfer_draft(sender.available, recipient.available, 20_00)
        )
        reversal = await ledger.reverse_entry(session, original.id)

        with pytest.raises(InvalidEntry):
            await ledger.reverse_entry(session, reversal.id)
        with pytest.raises(EntryNotFound):
            await ledger.reverse_entry(session, new_id())


# --- reading ---------------------------------------------------------------------------------


async def test_a_statement_lists_postings_newest_first(db: Database, clock: ManualClock) -> None:
    async with db.transaction() as session:
        user = await open_user(session)
        other = await open_user(session)
        await fund(session, user.available, 100_00)
        clock.advance(minutes=5)
        await ledger.post_entry(
            session,
            EntryDraft(
                "transfer",
                "transfer",
                "tr_s1",
                (debit(user.available, 30_00), credit(other.available, 30_00)),
                metadata={"note": "rent"},
            ),
        )
        clock.advance(minutes=5)
        await ledger.post_entry(session, transfer_draft(other.available, user.available, 4_00))

        lines = await ledger.statement(session, user.available)

        assert [(line.direction, line.amount, line.balance_after) for line in lines] == [
            (Direction.CREDIT, 4_00, 74_00),
            (Direction.DEBIT, 30_00, 70_00),
            (Direction.CREDIT, 100_00, 100_00),
        ]
        assert [line.seq for line in lines] == sorted((line.seq for line in lines), reverse=True)
        assert (lines[1].entry_kind, lines[1].source_id, lines[1].metadata) == (
            "transfer",
            "tr_s1",
            {"note": "rent"},
        )
        assert lines[0].posted_at - lines[2].posted_at == timedelta(minutes=10)


async def test_a_statement_pages_by_sequence(db: Database) -> None:
    async with db.transaction() as session:
        user = await open_user(session)
        for _ in range(5):
            await fund(session, user.available, 1_00)

        first = await ledger.statement(session, user.available, limit=2)
        second = await ledger.statement(session, user.available, before_seq=first[-1].seq, limit=2)
        third = await ledger.statement(session, user.available, before_seq=second[-1].seq, limit=2)

        assert [line.balance_after for line in first + second + third] == [
            5_00,
            4_00,
            3_00,
            2_00,
            1_00,
        ]
        assert await ledger.statement(session, user.available, before_seq=third[-1].seq) == []


async def test_derived_balances_agree_with_cached_ones(db: Database) -> None:
    async with db.transaction() as session:
        sender = await funded_user(session, 100_00)
        recipient = await open_user(session)
        untouched = await open_user(session)
        await ledger.post_entry(
            session, transfer_draft(sender.available, recipient.available, 33_33)
        )
        accounts = [sender.available, recipient.available, untouched.available]

        assert await ledger.derive_balances(session, accounts) == await ledger.get_balances(
            session, accounts
        )
        assert (await ledger.derive_balances(session, accounts))[untouched.available] == 0
