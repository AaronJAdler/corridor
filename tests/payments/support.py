"""Builders for payment tests: people with wallets, money in them, and a transfer in a line."""

import uuid
from typing import Any, NoReturn

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import ledger, payments, risk, wallets
from corridor.identity import Principal, User
from corridor.ledger import AccountKind
from corridor.payments import Beneficiary, DepositInstruction, Transfer, Withdrawal
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.ids import new_id
from corridor.providers import BankRail, Custodian
from tests.identity.support import add_user
from tests.support.ledger import fund
from tests.support.providers import ACCOUNT_NUMBER, ROUTING_NUMBER

# How many bank accounts a user may save where nothing is configured.
MAX_BENEFICIARIES: int = Settings.model_fields["max_beneficiaries_per_user"].default


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


async def lift_limits(db: Database, user: User) -> None:
    """Give one user a rule with no limits, for a test about amounts their tier would refuse
    before the code under test is reached. Everyone else keeps their tier's limits."""
    async with db.transaction() as session:
        await risk.set_limit(
            session, scope="user", user_id=user.id, per_tx_usd=None, daily_usd=None
        )


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


async def held(db: Database, user: User, asset: str = "USD") -> int:
    async with db.transaction() as session:
        wallet = await wallets.get_wallet(session, user.id, asset)
        return await ledger.get_balance(session, wallet.held_account_id)


async def balance_of(
    db: Database,
    kind: AccountKind,
    asset: str = "USD",
    *,
    provider: str | None = None,
    owner: User | None = None,
) -> int:
    """The balance of a system, provider or user account. Zero if it was never opened."""
    async with db.transaction() as session:
        account = await ledger.find_account(
            session, kind, asset, provider=provider, owner_id=owner.id if owner else None
        )
        return 0 if account is None else await ledger.get_balance(session, account.id)


async def settlement(db: Database, asset: str = "USD") -> int:
    """What Corridor's books say is at the bank."""
    return await balance_of(db, AccountKind.BANK_SETTLEMENT, asset, provider="simbank")


async def omnibus(db: Database, asset: str = "USDC") -> int:
    """What Corridor's books say is at the custodian."""
    return await balance_of(db, AccountKind.CUSTODY_OMNIBUS, asset, provider="simcustody")


async def suspense(db: Database, asset: str = "USD") -> int:
    return await balance_of(db, AccountKind.SUSPENSE, asset)


async def instruction_for(
    db: Database, user: User, asset: str, bank: BankRail, custody: Custodian
) -> DepositInstruction:
    return await payments.get_deposit_instruction(
        db, acting_as(user), asset, bank=bank, custody=custody
    )


async def entries(db: Database, source_type: str, source_id: str) -> list[dict[str, Any]]:
    """The journal entries posted for one business object, oldest first, with their
    postings as ``(account kind, direction, amount)``."""
    found = await rows(
        db,
        "SELECT e.id, e.kind FROM journal_entries e"
        " WHERE e.source_type = :source_type AND e.source_id = :source_id ORDER BY e.id",
        source_type=source_type,
        source_id=source_id,
    )
    for entry in found:
        postings = await rows(
            db,
            "SELECT a.kind, p.direction, p.amount FROM postings p"
            " JOIN ledger_accounts a ON a.id = p.account_id"
            " WHERE p.entry_id = :entry ORDER BY p.seq",
            entry=entry["id"],
        )
        entry["postings"] = [(p["kind"], p["direction"], int(p["amount"])) for p in postings]
    return found


async def user_status(db: Database, user: User) -> str:
    (row,) = await rows(db, "SELECT status FROM users WHERE id = :id", id=user.id)
    return str(row["status"])


async def add_beneficiary(
    db: Database,
    bank: BankRail,
    user: User,
    *,
    asset: str = "USD",
    account_number: str = ACCOUNT_NUMBER,
    routing_number: str | None = ROUTING_NUMBER,
    key: str | None = None,
    limit: int = MAX_BENEFICIARIES,
) -> Beneficiary:
    """A saved bank account of the user's, registered with the simulated bank."""
    return await payments.create_beneficiary(
        db,
        acting_as(user),
        bank=bank,
        asset=asset,
        holder_name="Maria Silva",
        account_number=account_number,
        routing_number=routing_number,
        idempotency_key=key or str(new_id()),
        limit=limit,
    )


async def withdraw(
    db: Database,
    settings: Settings,
    user: User,
    amount: int,
    *,
    asset: str = "USD",
    beneficiary: Beneficiary | None = None,
    to_address: str | None = None,
    principal: Principal | None = None,
    withdrawal_id: uuid.UUID | None = None,
) -> Withdrawal:
    """One withdrawal request in a transaction of its own, as the HTTP handler runs one."""
    async with db.transaction() as session:
        return await payments.request_withdrawal(
            session,
            principal or acting_as(user),
            withdrawal_id=withdrawal_id or new_id(),
            asset=asset,
            amount=amount,
            beneficiary_id=beneficiary.id if beneficiary is not None else None,
            to_address=to_address,
            settings=settings,
        )


async def withdrawal_row(db: Database, withdrawal_id: uuid.UUID) -> dict[str, Any]:
    (row,) = await rows(db, "SELECT * FROM withdrawals WHERE id = :id", id=withdrawal_id)
    return row


async def revenue_and_expense(db: Database, asset: str = "USD") -> tuple[int, int]:
    """What Corridor has earned in fees, and what providers have charged it, in an asset."""
    return (
        await balance_of(db, AccountKind.FEE_REVENUE, asset),
        await balance_of(db, AccountKind.PROVIDER_FEE_EXPENSE, asset),
    )


async def held_bank_withdrawal(
    db: Database,
    settings: Settings,
    bank: BankRail,
    user: User,
    amount: int = 100_00,
    *,
    account_number: str = ACCOUNT_NUMBER,
) -> Withdrawal:
    """A user with 500.00 USD, one saved account and one withdrawal to it, held."""
    await deposit(db, user, 500_00)
    beneficiary = await add_beneficiary(db, bank, user, account_number=account_number)
    return await withdraw(db, settings, user, amount, beneficiary=beneficiary)


async def held_chain_withdrawal(
    db: Database, settings: Settings, user: User, to_address: str, amount: int = 25_000_000
) -> Withdrawal:
    """A user with 50 USDC and one withdrawal to an address, held."""
    await deposit(db, user, 50_000_000, "USDC")
    return await withdraw(db, settings, user, amount, asset="USDC", to_address=to_address)


class WorkerDied(Exception):
    """Raised in place of a provider call, as a worker that dies there makes none."""


class _NeverAsked:
    """A provider that is never reached: the call that would ask it ends the worker."""

    name = "nobody"

    async def create_payout(self, **_arguments: Any) -> NoReturn:
        raise WorkerDied

    async def create_withdrawal(self, **_arguments: Any) -> NoReturn:
        raise WorkerDied


async def leave_submitting(db: Database, withdrawal_id: uuid.UUID) -> None:
    """Leave a held withdrawal as a worker does that dies after marking it as being sent
    and before asking the provider: ``submitting``, with nothing at the provider."""
    nobody: Any = _NeverAsked()
    try:
        await payments.submit_withdrawal(db, nobody, nobody, withdrawal_id)
    except WorkerDied:
        return
    raise AssertionError(f"withdrawal {withdrawal_id} was not held, so nothing was asked")
