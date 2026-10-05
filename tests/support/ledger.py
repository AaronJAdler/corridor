"""Builders for ledger tests: accounts and funded balances in a line or two."""

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from corridor import ledger
from corridor.ledger import AccountKind, EntryDraft, credit, debit
from corridor.platform.ids import new_id

PROVIDER = "simbank"


@dataclass(frozen=True)
class UserAccounts:
    owner: uuid.UUID
    asset: str
    available: uuid.UUID
    held: uuid.UUID


async def open_user(
    session: AsyncSession, asset: str = "USD", *, owner: uuid.UUID | None = None
) -> UserAccounts:
    owner = owner or new_id()
    available = await ledger.open_account(
        session, AccountKind.USER_AVAILABLE, asset, owner_id=owner
    )
    held = await ledger.open_account(session, AccountKind.USER_HELD, asset, owner_id=owner)
    return UserAccounts(owner=owner, asset=asset, available=available.id, held=held.id)


async def system_account(session: AsyncSession, kind: AccountKind, asset: str = "USD") -> uuid.UUID:
    provider = PROVIDER if ledger.CHART[kind].scope == "provider" else None
    return (await ledger.open_account(session, kind, asset, provider=provider)).id


async def fund(
    session: AsyncSession, account: uuid.UUID, amount: int, asset: str = "USD"
) -> ledger.PostedEntry:
    """Credit a user account from the bank settlement account, as a deposit would."""
    settlement = await system_account(session, AccountKind.BANK_SETTLEMENT, asset)
    return await ledger.post_entry(
        session,
        EntryDraft(
            kind="deposit",
            source_type="test_deposit",
            source_id=str(new_id()),
            postings=(debit(settlement, amount), credit(account, amount)),
        ),
    )


async def funded_user(session: AsyncSession, amount: int, asset: str = "USD") -> UserAccounts:
    user = await open_user(session, asset)
    await fund(session, user.available, amount, asset)
    return user


def transfer_draft(
    sender: uuid.UUID, recipient: uuid.UUID, amount: int, *, source_id: str | None = None
) -> EntryDraft:
    return EntryDraft(
        kind="transfer",
        source_type="test_transfer",
        source_id=source_id or str(new_id()),
        postings=(debit(sender, amount), credit(recipient, amount)),
    )
