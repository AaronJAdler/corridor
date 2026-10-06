"""Statements: a wallet's entries, newest first, in pages that neither repeat nor skip."""

import base64
import json
import uuid

import pytest

from corridor import ledger, wallets
from corridor.ledger import Direction, EntryDraft, credit, debit
from corridor.platform.db import Database
from corridor.platform.errors import InvalidRequest
from corridor.platform.ids import new_id
from corridor.platform.money import UnknownAsset
from corridor.platform.pagination import InvalidCursor, Page, encode_cursor
from corridor.wallets import StatementEntry
from tests.support.ledger import fund


async def deposit(db: Database, user: uuid.UUID, amount: int, asset: str = "USD") -> int:
    """Credit the user's wallet and return the seq of the posting on it."""
    async with db.transaction() as session:
        wallet = await wallets.get_wallet(session, user, asset)
        entry = await fund(session, wallet.available_account_id, amount, asset)
    return next(p.seq for p in entry.postings if p.account_id == wallet.available_account_id)


async def page(
    db: Database, user: uuid.UUID, asset: str = "USD", *, cursor: str | None = None, limit: int = 50
) -> Page[StatementEntry]:
    async with db.transaction() as session:
        return await wallets.list_entries(session, user, asset, cursor=cursor, limit=limit)


def forged(payload: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


async def test_a_new_wallet_has_an_empty_statement(db: Database, user_id: uuid.UUID) -> None:
    assert await page(db, user_id) == Page(items=(), next_cursor=None)


async def test_entries_are_listed_newest_first_with_what_the_ledger_recorded(
    db: Database, user_id: uuid.UUID
) -> None:
    first = await deposit(db, user_id, 10_00)
    second = await deposit(db, user_id, 5_25)

    found = await page(db, user_id)

    assert [e.seq for e in found.items] == [second, first]
    newest = found.items[0]
    assert (newest.kind, newest.direction, newest.amount, newest.balance_after) == (
        "deposit",
        Direction.CREDIT,
        5_25,
        15_25,
    )
    assert newest.posted_at.tzinfo is not None
    assert found.next_cursor is None


async def test_a_statement_is_of_the_available_account_so_a_hold_shows_once_as_a_debit(
    db: Database, user_id: uuid.UUID
) -> None:
    await deposit(db, user_id, 10_00)
    async with db.transaction() as session:
        wallet = await wallets.get_wallet(session, user_id, "USD")
        held = await ledger.post_entry(
            session,
            EntryDraft(
                kind="withdrawal_hold",
                source_type="test_withdrawal",
                source_id=str(new_id()),
                postings=(
                    debit(wallet.available_account_id, 4_00),
                    credit(wallet.held_account_id, 4_00),
                ),
            ),
        )

    found = await page(db, user_id)

    newest = found.items[0]
    assert (newest.entry_id, newest.direction, newest.amount, newest.balance_after) == (
        held.id,
        Direction.DEBIT,
        4_00,
        6_00,
    )
    assert len(found.items) == 2


async def test_a_statement_shows_only_its_own_asset_and_its_own_user(
    db: Database, user_id: uuid.UUID
) -> None:
    other = new_id()
    async with db.transaction() as session:
        await wallets.provision(session, other)
    mine = await deposit(db, user_id, 10_00)
    await deposit(db, user_id, 2_000_000, "USDC")
    await deposit(db, other, 7_00)

    assert [e.seq for e in (await page(db, user_id)).items] == [mine]


async def test_a_page_that_ends_the_statement_has_no_cursor_and_one_that_does_not_has_one(
    db: Database, user_id: uuid.UUID
) -> None:
    for _ in range(3):
        await deposit(db, user_id, 1_00)

    assert (await page(db, user_id, limit=2)).next_cursor is not None
    assert (await page(db, user_id, limit=3)).next_cursor is None
    assert (await page(db, user_id, limit=4)).next_cursor is None


async def test_pages_neither_repeat_nor_skip_while_new_entries_arrive_between_them(
    db: Database, user_id: uuid.UUID
) -> None:
    existing = [await deposit(db, user_id, 1_00) for _ in range(7)]

    seen: list[int] = []
    sizes: list[int] = []
    cursor: str | None = None
    # Three pages hold seven entries. The bound is for a paging that never ends.
    for _ in range(5):
        found = await page(db, user_id, cursor=cursor, limit=3)
        seen += [e.seq for e in found.items]
        sizes.append(len(found.items))
        if found.next_cursor is None:
            break
        cursor = found.next_cursor
        # Newer than everything already listed: it belongs to no later page.
        await deposit(db, user_id, 9_00)

    assert seen == sorted(existing, reverse=True)
    assert sizes == [3, 3, 1]


async def test_a_cursor_from_another_asset_of_the_same_user_is_refused(
    db: Database, user_id: uuid.UUID
) -> None:
    for _ in range(2):
        await deposit(db, user_id, 1_00)
        await deposit(db, user_id, 1_000_000, "USDC")
    usd_cursor = (await page(db, user_id, limit=1)).next_cursor
    assert usd_cursor is not None

    with pytest.raises(InvalidCursor):
        await page(db, user_id, "USDC", cursor=usd_cursor, limit=1)


async def test_a_cursor_from_another_users_wallet_is_refused(
    db: Database, user_id: uuid.UUID
) -> None:
    other = new_id()
    async with db.transaction() as session:
        await wallets.provision(session, other)
    for user in (user_id, other):
        for _ in range(2):
            await deposit(db, user, 1_00)
    theirs = (await page(db, other, limit=1)).next_cursor
    assert theirs is not None

    with pytest.raises(InvalidCursor):
        await page(db, user_id, cursor=theirs, limit=1)


async def test_a_tampered_cursor_is_refused(db: Database, user_id: uuid.UUID) -> None:
    for _ in range(2):
        await deposit(db, user_id, 1_00)
    cursor = (await page(db, user_id, limit=1)).next_cursor
    assert cursor is not None

    for tampered in (cursor[:-2], cursor + "A", "x" + cursor, cursor[::-1]):
        with pytest.raises(InvalidCursor):
            await page(db, user_id, cursor=tampered, limit=1)


@pytest.mark.parametrize("position", ["12", 0, -1, 2**63, 1.5, None, True])
async def test_a_cursor_for_this_account_whose_position_is_not_a_posting_seq_is_refused(
    db: Database, user_id: uuid.UUID, position: object
) -> None:
    async with db.transaction() as session:
        wallet = await wallets.get_wallet(session, user_id, "USD")
    scope = str(wallet.available_account_id)
    # The kind is whatever the module uses: take it from a cursor the module would accept.
    await deposit(db, user_id, 1_00)
    await deposit(db, user_id, 1_00)
    genuine = (await page(db, user_id, limit=1)).next_cursor
    assert genuine is not None
    kind = json.loads(base64.urlsafe_b64decode(genuine + "=" * (-len(genuine) % 4)))["k"]

    with pytest.raises(InvalidCursor):
        await page(db, user_id, cursor=forged({"k": kind, "s": scope, "p": position}))


async def test_a_cursor_of_another_kind_for_the_same_account_is_refused(
    db: Database, user_id: uuid.UUID
) -> None:
    async with db.transaction() as session:
        wallet = await wallets.get_wallet(session, user_id, "USD")
    foreign = encode_cursor(
        kind="something_else", scope=str(wallet.available_account_id), position=5
    )

    with pytest.raises(InvalidCursor):
        await page(db, user_id, cursor=foreign)


@pytest.mark.parametrize("cursor", ["", "garbage", "!!!", "e30"])
async def test_garbage_for_a_cursor_is_refused(
    db: Database, user_id: uuid.UUID, cursor: str
) -> None:
    with pytest.raises(InvalidCursor):
        await page(db, user_id, cursor=cursor)


async def test_the_default_page_is_50_entries(db: Database, user_id: uuid.UUID) -> None:
    async with db.transaction() as session:
        wallet = await wallets.get_wallet(session, user_id, "USD")
        for _ in range(51):
            await fund(session, wallet.available_account_id, 1_00)

    async with db.transaction() as session:
        found = await wallets.list_entries(session, user_id, "USD")

    assert len(found.items) == 50
    assert found.next_cursor is not None


async def test_a_page_is_capped_at_200_entries_however_many_are_asked_for(
    db: Database, user_id: uuid.UUID
) -> None:
    async with db.transaction() as session:
        wallet = await wallets.get_wallet(session, user_id, "USD")
        for _ in range(203):
            await fund(session, wallet.available_account_id, 1_00)

    found = await page(db, user_id, limit=10_000)
    rest = await page(db, user_id, cursor=found.next_cursor, limit=10_000)

    assert len(found.items) == 200
    assert len(rest.items) == 3
    assert rest.next_cursor is None


@pytest.mark.parametrize("limit", [0, -1])
async def test_a_limit_below_one_is_refused(db: Database, user_id: uuid.UUID, limit: int) -> None:
    with pytest.raises(InvalidRequest) as refusal:
        await page(db, user_id, limit=limit)

    assert refusal.value.status == 422


async def test_the_statement_of_an_unsupported_asset_is_an_unknown_asset(
    db: Database, user_id: uuid.UUID
) -> None:
    with pytest.raises(UnknownAsset):
        await page(db, user_id, "EUR")


async def test_the_statement_of_a_wallet_that_does_not_exist_is_not_found(db: Database) -> None:
    with pytest.raises(wallets.WalletNotFound):
        await page(db, new_id())
