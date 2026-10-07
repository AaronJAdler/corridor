"""Builders for payment tests: people with wallets, money in them, and a transfer in a line."""

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import ledger, payments, wallets
from corridor.identity import Principal, User
from corridor.ledger import AccountKind
from corridor.payments import Transfer
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.ids import new_id
from tests.identity.support import add_user
from tests.support.ledger import fund


async def add_person(session: AsyncSession, name: str) -> User:
    """A registered user with a wallet in every asset, as registration over HTTP leaves one."""
    user = await add_user(session, name)
    await wallets.provision(session, user.id)
    return user


def acting_as(user: User) -> Principal:
    """The principal of the user's own session."""
    return Principal.for_user(user.id, user.role, new_id())


def agent_of(user: User, *scopes: str) -> Principal:
    """The principal of an agent acting for the user with only these scopes."""
    return Principal(
        user_id=user.id,
        actor_type="agent",
        actor_id=new_id(),
        role="user",
        scopes=frozenset(scopes),
        session_id=None,
    )


async def deposit(db: Database, user: User, amount: int, asset: str = "USD") -> None:
    async with db.transaction() as session:
        wallet = await wallets.get_wallet(session, user.id, asset)
        await fund(session, wallet.available_account_id, amount, asset)


async def available(db: Database, user: User, asset: str = "USD") -> int:
    async with db.transaction() as session:
        wallet = await wallets.get_wallet(session, user.id, asset)
        return await ledger.get_balance(session, wallet.available_account_id)


async def fee_revenue(db: Database, asset: str = "USD") -> int:
    """What Corridor has earned in fees in an asset. Zero if it has never charged one."""
    async with db.transaction() as session:
        account = await ledger.find_account(session, AccountKind.FEE_REVENUE, asset)
        return 0 if account is None else await ledger.get_balance(session, account.id)


async def send(
    db: Database,
    settings: Settings,
    sender: User,
    recipient: User | str,
    amount: int,
    *,
    asset: str = "USD",
    memo: str | None = None,
    principal: Principal | None = None,
    transfer_id: uuid.UUID | None = None,
) -> Transfer:
    """One transfer in a transaction of its own, as the HTTP handler runs one."""
    async with db.transaction() as session:
        return await payments.create_transfer(
            session,
            principal or acting_as(sender),
            transfer_id=transfer_id or new_id(),
            recipient=recipient if isinstance(recipient, str) else str(recipient.id),
            asset=asset,
            amount=amount,
            memo=memo,
            settings=settings,
        )


async def count(db: Database, table: str) -> int:
    async with db.transaction() as session:
        return int((await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one())  # noqa: S608


async def rows(db: Database, statement: str, **parameters: Any) -> list[dict[str, Any]]:
    async with db.transaction() as session:
        found = await session.execute(text(statement), parameters)
        return [dict(row) for row in found.mappings()]
