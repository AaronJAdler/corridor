"""Reading wallets: balances come from the ledger, and a wallet is found only by its owner."""

import uuid
from collections.abc import Awaitable, Callable

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import ledger, wallets
from corridor.ledger import EntryDraft, credit, debit
from corridor.platform.db import Database
from corridor.platform.ids import new_id
from corridor.platform.money import ASSETS, UnknownAsset
from tests.support.ledger import fund


async def hold(db: Database, wallet: wallets.Wallet, amount: int) -> None:
    """Move money from available to held, as a withdrawal does while it is in flight."""
    async with db.transaction() as session:
        await ledger.post_entry(
            session,
            EntryDraft(
                kind="withdrawal_hold",
                source_type="test_withdrawal",
                source_id=str(new_id()),
                postings=(
                    debit(wallet.available_account_id, amount),
                    credit(wallet.held_account_id, amount),
                ),
            ),
        )


async def balance(db: Database, user: uuid.UUID, asset: str) -> wallets.WalletBalance:
    async with db.transaction() as session:
        found = {b.asset: b for b in await wallets.get_wallets(session, user)}
    return found[asset]


async def test_a_new_wallet_is_empty_in_every_asset_in_asset_order(
    db: Database, user_id: uuid.UUID
) -> None:
    async with db.transaction() as session:
        balances = await wallets.get_wallets(session, user_id)

    assert [b.asset for b in balances] == sorted(ASSETS)
    assert [(b.available, b.held, b.total) for b in balances] == [(0, 0, 0)] * len(ASSETS)


async def test_a_balance_carries_the_account_ids_of_its_wallet(
    db: Database, user_id: uuid.UUID
) -> None:
    async with db.transaction() as session:
        balances = await wallets.get_wallets(session, user_id)
        wallet = await wallets.get_wallet(session, user_id, "USD")

    usd = next(b for b in balances if b.asset == "USD")
    assert usd.available_account_id == wallet.available_account_id
    assert usd.held_account_id == wallet.held_account_id


async def test_a_user_without_wallets_has_no_balances(db: Database) -> None:
    async with db.transaction() as session:
        assert await wallets.get_wallets(session, new_id()) == []


async def test_a_deposit_shows_as_available_in_its_asset_only(
    db: Database, user_id: uuid.UUID
) -> None:
    async with db.transaction() as session:
        usd = await wallets.get_wallet(session, user_id, "USD")
        usdc = await wallets.get_wallet(session, user_id, "USDC")
        await fund(session, usd.available_account_id, 125_50)
        await fund(session, usd.available_account_id, 10_00)
        await fund(session, usdc.available_account_id, 3_000_000, "USDC")

    found = await balance(db, user_id, "USD")
    assert (found.available, found.held, found.total) == (135_50, 0, 135_50)
    found = await balance(db, user_id, "USDC")
    assert (found.available, found.held, found.total) == (3_000_000, 0, 3_000_000)
    found = await balance(db, user_id, "MXN")
    assert (found.available, found.held, found.total) == (0, 0, 0)


async def test_a_hold_moves_money_from_available_to_held_and_the_total_stays(
    db: Database, user_id: uuid.UUID
) -> None:
    async with db.transaction() as session:
        usd = await wallets.get_wallet(session, user_id, "USD")
        await fund(session, usd.available_account_id, 100_00)

    await hold(db, usd, 30_00)

    found = await balance(db, user_id, "USD")
    assert (found.available, found.held, found.total) == (70_00, 30_00, 100_00)


async def test_one_users_money_does_not_show_in_anothers_wallet(
    db: Database, user_id: uuid.UUID
) -> None:
    other = new_id()
    async with db.transaction() as session:
        await wallets.provision(session, other)
        usd = await wallets.get_wallet(session, user_id, "USD")
        await fund(session, usd.available_account_id, 100_00)

    assert (await balance(db, other, "USD")).total == 0


async def test_get_wallet_and_resolve_return_the_users_wallet_for_the_asset(
    db: Database, user_id: uuid.UUID
) -> None:
    async with db.transaction() as session:
        provisioned = {w.asset: w for w in await wallets.provision(session, user_id)}
        got = await wallets.get_wallet(session, user_id, "BRL")
        resolved = await wallets.resolve(session, user_id, "BRL")

    assert got == resolved == provisioned["BRL"]
    assert got.asset == "BRL"
    assert got.user_id == user_id


@pytest.mark.parametrize("find", [wallets.get_wallet, wallets.resolve])
async def test_a_wallet_that_was_never_provisioned_is_not_found(
    db: Database, find: Callable[[AsyncSession, uuid.UUID, str], Awaitable[wallets.Wallet]]
) -> None:
    with pytest.raises(wallets.WalletNotFound) as refusal:
        async with db.transaction() as session:
            await find(session, new_id(), "USD")

    assert (refusal.value.status, refusal.value.code) == (404, "wallet_not_found")


@pytest.mark.parametrize("asset", ["EUR", "usd", "", "USD "])
async def test_an_asset_that_is_not_supported_is_an_unknown_asset_not_a_missing_wallet(
    db: Database, user_id: uuid.UUID, asset: str
) -> None:
    with pytest.raises(UnknownAsset):
        async with db.transaction() as session:
            await wallets.get_wallet(session, user_id, asset)


async def test_an_account_is_traced_back_to_its_owner(db: Database, user_id: uuid.UUID) -> None:
    async with db.transaction() as session:
        wallet = await wallets.get_wallet(session, user_id, "MXN")

        assert await wallets.owner_of(session, wallet.available_account_id) == user_id
        assert await wallets.owner_of(session, wallet.held_account_id) == user_id


async def test_an_account_that_is_nobodys_wallet_has_no_owner(db: Database) -> None:
    async with db.transaction() as session:
        settlement = await ledger.open_account(
            session, ledger.AccountKind.BANK_SETTLEMENT, "USD", provider="simbank"
        )

        assert await wallets.owner_of(session, settlement.id) is None
        assert await wallets.owner_of(session, new_id()) is None
