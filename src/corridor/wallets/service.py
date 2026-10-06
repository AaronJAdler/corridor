"""Wallets: which ledger accounts are a user's, and what is in them.

A wallet row only names accounts. The money is the ledger's, and every balance here is read
from it. Every function takes the caller's session and runs inside the caller's transaction.
"""

import uuid
from typing import cast

from sqlalchemy import RowMapping, Table, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import ledger
from corridor.ledger import AccountKind
from corridor.platform.clock import utcnow
from corridor.platform.money import ASSETS, get_asset
from corridor.wallets.errors import WalletNotFound
from corridor.wallets.models import WalletAccount
from corridor.wallets.types import Wallet, WalletBalance

_wallets = cast(Table, WalletAccount.__table__)


async def provision(session: AsyncSession, user_id: uuid.UUID) -> list[Wallet]:
    """Give a user a wallet in every asset, or find the ones they have.

    Safe to call any number of times, concurrently. The assets are taken in one order, so
    two transactions provisioning the same user queue on the first account instead of
    deadlocking, and the second finds everything already there.
    """
    for asset_code in sorted(ASSETS):
        available = await ledger.open_account(
            session, AccountKind.USER_AVAILABLE, asset_code, owner_id=user_id
        )
        held = await ledger.open_account(
            session, AccountKind.USER_HELD, asset_code, owner_id=user_id
        )
        await session.execute(
            pg_insert(_wallets)
            .values(
                user_id=user_id,
                asset_code=asset_code,
                available_account_id=available.id,
                held_account_id=held.id,
                created_at=utcnow(),
            )
            # Only the user's own row for the asset may already exist. A clash on an
            # account id would mean two wallets claim one account, and has to fail.
            .on_conflict_do_nothing(constraint="pk_wallet_accounts")
        )
    return await _list(session, user_id)


async def get_wallets(session: AsyncSession, user_id: uuid.UUID) -> list[WalletBalance]:
    """The user's balance in every asset they have a wallet in, in asset-code order."""
    found = await _list(session, user_id)
    balances = await ledger.get_balances(
        session,
        [account for wallet in found for account in _accounts_of(wallet)],
    )
    return [
        WalletBalance(
            asset=wallet.asset,
            available=balances[wallet.available_account_id],
            held=balances[wallet.held_account_id],
            total=balances[wallet.available_account_id] + balances[wallet.held_account_id],
            available_account_id=wallet.available_account_id,
            held_account_id=wallet.held_account_id,
        )
        for wallet in found
    ]


async def get_wallet(session: AsyncSession, user_id: uuid.UUID, asset: str) -> Wallet:
    """The user's wallet in one asset.

    Raises ``UnknownAsset`` for an asset that is not supported and ``WalletNotFound`` for a
    supported one the user has no wallet in.
    """
    get_asset(asset)
    rows = await session.execute(
        select(_wallets).where(_wallets.c.user_id == user_id, _wallets.c.asset_code == asset)
    )
    row = rows.mappings().one_or_none()
    if row is None:
        raise WalletNotFound(asset)
    return _wallet(row)


async def resolve(session: AsyncSession, user_id: uuid.UUID, asset: str) -> Wallet:
    """The accounts to post to when money moves for this user in this asset."""
    return await get_wallet(session, user_id, asset)


async def owner_of(session: AsyncSession, account_id: uuid.UUID) -> uuid.UUID | None:
    """The user whose wallet a ledger account belongs to, if it is anyone's wallet."""
    rows = await session.execute(
        select(_wallets.c.user_id).where(
            or_(
                _wallets.c.available_account_id == account_id,
                _wallets.c.held_account_id == account_id,
            )
        )
    )
    owner: uuid.UUID | None = rows.scalar_one_or_none()
    return owner


async def _list(session: AsyncSession, user_id: uuid.UUID) -> list[Wallet]:
    rows = await session.execute(
        select(_wallets).where(_wallets.c.user_id == user_id).order_by(_wallets.c.asset_code)
    )
    return [_wallet(row) for row in rows.mappings()]


def _accounts_of(wallet: Wallet) -> tuple[uuid.UUID, uuid.UUID]:
    return wallet.available_account_id, wallet.held_account_id


def _wallet(row: RowMapping) -> Wallet:
    return Wallet(
        user_id=row["user_id"],
        asset=row["asset_code"],
        available_account_id=row["available_account_id"],
        held_account_id=row["held_account_id"],
    )
