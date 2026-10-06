"""Provisioning: a user gets one available and one held account per asset, exactly once."""

import asyncio
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import ledger, wallets
from corridor.ledger import AccountKind
from corridor.platform.db import Database
from corridor.platform.ids import new_id
from corridor.platform.money import ASSETS


async def wallet_rows(db: Database, user: uuid.UUID) -> int:
    async with db.transaction() as session:
        counted = await session.execute(
            text("SELECT count(*) FROM wallet_accounts WHERE user_id = :user"), {"user": user}
        )
        return int(counted.scalar_one())


async def ledger_accounts(db: Database, user: uuid.UUID) -> list[ledger.Account]:
    async with db.transaction() as session:
        return await ledger.list_accounts(session, owner_id=user)


async def test_provisioning_gives_a_wallet_for_every_asset_in_asset_order(db: Database) -> None:
    user = new_id()

    async with db.transaction() as session:
        provisioned = await wallets.provision(session, user)

    assert [wallet.asset for wallet in provisioned] == sorted(ASSETS)
    assert {wallet.user_id for wallet in provisioned} == {user}


async def test_each_wallet_names_the_users_available_and_held_ledger_accounts(db: Database) -> None:
    user = new_id()

    async with db.transaction() as session:
        provisioned = await wallets.provision(session, user)

    accounts = {(a.asset_code, a.kind): a.id for a in await ledger_accounts(db, user)}
    assert len(accounts) == 2 * len(ASSETS)
    for wallet in provisioned:
        assert wallet.available_account_id == accounts[wallet.asset, AccountKind.USER_AVAILABLE]
        assert wallet.held_account_id == accounts[wallet.asset, AccountKind.USER_HELD]


async def test_provisioning_again_changes_nothing_and_returns_the_same_wallets(
    db: Database,
) -> None:
    user = new_id()
    async with db.transaction() as session:
        first = await wallets.provision(session, user)

    async with db.transaction() as session:
        second = await wallets.provision(session, user)

    assert second == first
    assert await wallet_rows(db, user) == len(ASSETS)
    assert len(await ledger_accounts(db, user)) == 2 * len(ASSETS)


async def test_provisioning_twice_in_one_transaction_is_harmless(db: Database) -> None:
    user = new_id()

    async with db.transaction() as session:
        first = await wallets.provision(session, user)
        second = await wallets.provision(session, user)

    assert second == first
    assert await wallet_rows(db, user) == len(ASSETS)


async def test_20_concurrent_provisions_of_one_user_create_each_wallet_once(db: Database) -> None:
    user = new_id()

    async def provision(session: AsyncSession) -> list[wallets.Wallet]:
        return await wallets.provision(session, user)

    results = await asyncio.gather(*(db.run(provision) for _ in range(20)))

    assert all(result == results[0] for result in results)
    assert [wallet.asset for wallet in results[0]] == sorted(ASSETS)
    assert await wallet_rows(db, user) == len(ASSETS)
    assert len(await ledger_accounts(db, user)) == 2 * len(ASSETS)


async def test_two_users_do_not_share_an_account(db: Database) -> None:
    async with db.transaction() as session:
        mine = await wallets.provision(session, new_id())
        theirs = await wallets.provision(session, new_id())

    def ids(found: list[wallets.Wallet]) -> set[uuid.UUID]:
        return {w.available_account_id for w in found} | {w.held_account_id for w in found}

    assert len(ids(mine)) == len(ids(theirs)) == 2 * len(ASSETS)
    assert ids(mine).isdisjoint(ids(theirs))
