"""Posting: the one place a balance changes.

Every function takes the caller's session and runs inside the caller's transaction. Nothing
here commits. See section 4 of the architecture for the model and section 5 for the locks.
"""

import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, cast

from sqlalchemy import RowMapping, Table, bindparam, case, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from corridor.ledger.errors import (
    ConflictingEntry,
    EntryNotFound,
    InsufficientFunds,
    InvalidEntry,
    LedgerError,
    UnknownAccount,
)
from corridor.ledger.models import AccountBalance, JournalEntry, LedgerAccount, Posting
from corridor.ledger.types import (
    CHART,
    Account,
    AccountKind,
    Direction,
    EntryDraft,
    PostedEntry,
    PostedPosting,
    PostingDraft,
    StatementLine,
)
from corridor.platform.clock import utcnow
from corridor.platform.ids import new_id
from corridor.platform.money import MAX_MINOR_UNITS, get_asset

# Core tables. The ledger writes with explicit statements and never through the ORM's unit
# of work, so what reaches the database is exactly what is written here.
_accounts = cast(Table, LedgerAccount.__table__)
_balances = cast(Table, AccountBalance.__table__)
_entries = cast(Table, JournalEntry.__table__)
_postings = cast(Table, Posting.__table__)

REVERSAL_KIND = "reversal"


# --- accounts ------------------------------------------------------------------------------


async def open_account(
    session: AsyncSession,
    kind: AccountKind,
    asset_code: str,
    *,
    owner_id: uuid.UUID | None = None,
    provider: str | None = None,
) -> Account:
    """Open an account, or return the one that already exists for this identity.

    Safe to call any number of times, concurrently: there is one account per kind, asset,
    owner and provider, and the database enforces it.
    """
    spec = CHART[kind]
    _check_identity(kind, owner_id, provider)
    get_asset(asset_code)

    inserted = await session.execute(
        pg_insert(_accounts)
        .values(
            id=new_id(),
            asset_code=asset_code,
            kind=kind.value,
            category=spec.category,
            normal_side=spec.normal_side.value,
            owner_id=owner_id,
            provider=provider,
            is_constrained=spec.constrained,
            created_at=utcnow(),
        )
        .on_conflict_do_nothing(constraint="uq_ledger_accounts_identity")
        .returning(_accounts.c.id)
    )
    account_id = inserted.scalar_one_or_none()
    if account_id is not None and spec.constrained:
        await session.execute(
            insert(_balances).values(
                account_id=account_id, balance=0, last_posting_seq=0, updated_at=utcnow()
            )
        )

    account = await find_account(session, kind, asset_code, owner_id=owner_id, provider=provider)
    if account is None:  # pragma: no cover - the insert above either created it or found it
        raise LedgerError(f"account {kind}/{asset_code} vanished while being opened")
    return account


async def find_account(
    session: AsyncSession,
    kind: AccountKind,
    asset_code: str,
    *,
    owner_id: uuid.UUID | None = None,
    provider: str | None = None,
) -> Account | None:
    rows = await session.execute(
        select(_accounts).where(
            _accounts.c.kind == kind.value,
            _accounts.c.asset_code == asset_code,
            # Spelled out rather than IS NOT DISTINCT FROM, which cannot use the index.
            _accounts.c.owner_id.is_(None)
            if owner_id is None
            else _accounts.c.owner_id == owner_id,
            _accounts.c.provider.is_(None)
            if provider is None
            else _accounts.c.provider == provider,
        )
    )
    row = rows.mappings().one_or_none()
    return _account(row) if row is not None else None


async def get_account(session: AsyncSession, account_id: uuid.UUID) -> Account:
    return (await _load_accounts(session, [account_id]))[account_id]


async def list_accounts(session: AsyncSession, *, owner_id: uuid.UUID) -> list[Account]:
    """Every account that belongs to one owner, in a stable order."""
    rows = await session.execute(
        select(_accounts)
        .where(_accounts.c.owner_id == owner_id)
        .order_by(_accounts.c.asset_code, _accounts.c.kind)
    )
    return [_account(row) for row in rows.mappings()]


def _check_identity(kind: AccountKind, owner_id: uuid.UUID | None, provider: str | None) -> None:
    scope = CHART[kind].scope
    wanted = {"user": (True, False), "provider": (False, True), "system": (False, False)}[scope]
    if (owner_id is not None, provider is not None) != wanted:
        raise InvalidEntry(
            f"a {kind} account belongs to "
            + {"user": "a user", "provider": "a provider", "system": "the system"}[scope]
            + ": pass "
            + {"user": "owner_id only", "provider": "provider only", "system": "neither"}[scope]
        )


def _account(row: RowMapping) -> Account:
    return Account(
        id=row["id"],
        asset_code=row["asset_code"],
        kind=AccountKind(row["kind"]),
        normal_side=Direction(row["normal_side"]),
        constrained=row["is_constrained"],
        owner_id=row["owner_id"],
        provider=row["provider"],
    )


async def _load_accounts(
    session: AsyncSession, account_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, Account]:
    wanted = set(account_ids)
    rows = await session.execute(select(_accounts).where(_accounts.c.id.in_(wanted)))
    found = {row["id"]: _account(row) for row in rows.mappings()}
    missing = wanted - found.keys()
    if missing:
        raise UnknownAccount(f"no ledger account {sorted(str(account) for account in missing)}")
    return found


# --- posting -------------------------------------------------------------------------------


async def post_entry(session: AsyncSession, draft: EntryDraft) -> PostedEntry:
    """Post a journal entry, atomically, at most once per business event.

    The steps, in the order that matters:

    1. Validate the draft. Nothing is locked or written for an entry that cannot be right.
    2. Lock the cached balances of the constrained accounts, in ascending account id.
    3. Look for an entry already posted for this event. It is looked for *after* the locks,
       because a concurrent transaction posting the same event holds the same locks.
    4. Work out the new balances and refuse, before writing, if any would be negative.
    5. Insert the entry, its postings and the new balances.

    Every check comes before the first write, so a refusal has written nothing and leaves
    the caller's transaction usable: a handler can record the refusal and commit.
    """
    _check_shape(draft)
    accounts = await _load_accounts(session, (posting.account_id for posting in draft.postings))
    _check_balanced(draft, accounts)

    constrained = [account.id for account in accounts.values() if account.constrained]
    balances = await _lock_balances(session, constrained)

    existing = await find_entry(session, draft.source_type, draft.source_id, draft.kind)
    if existing is not None:
        return _replayed(existing, draft)

    new_balances = _apply(draft.postings, accounts, balances)

    now = utcnow()
    inserted = await session.execute(
        pg_insert(_entries)
        .values(
            id=new_id(),
            kind=draft.kind,
            source_type=draft.source_type,
            source_id=draft.source_id,
            metadata=dict(draft.metadata),
            reverses_entry_id=draft.reverses_entry_id,
            posted_at=now,
        )
        .on_conflict_do_nothing(constraint="uq_journal_entries_source")
        .returning(_entries.c.id)
    )
    entry_id = inserted.scalar_one_or_none()
    if entry_id is None:
        # A concurrent transaction posted this event and shares no constrained account
        # with us, so no lock made us wait for it. The insert did wait, on the unique
        # index, and that transaction has now committed.
        existing = await find_entry(session, draft.source_type, draft.source_id, draft.kind)
        if existing is None:  # pragma: no cover - the conflicting row is committed
            raise LedgerError("an entry conflicted on its source and then could not be found")
        return _replayed(existing, draft)

    written = await session.execute(
        insert(_postings)
        .values(
            [
                {
                    "entry_id": entry_id,
                    "account_id": posting.account_id,
                    "asset_code": accounts[posting.account_id].asset_code,
                    "direction": posting.direction.value,
                    "amount": posting.amount,
                    "balance_after": new_balances.get(posting.account_id),
                }
                for posting in draft.postings
            ]
        )
        .returning(_postings.c.seq, _postings.c.account_id)
    )
    seq_of = {row.account_id: row.seq for row in written}

    if new_balances:
        await session.execute(
            update(_balances)
            .where(_balances.c.account_id == bindparam("for_account"))
            .values(
                balance=bindparam("new_balance"),
                last_posting_seq=bindparam("new_seq"),
                updated_at=now,
            ),
            [
                {
                    "for_account": account_id,
                    "new_balance": balance,
                    "new_seq": seq_of[account_id],
                }
                for account_id, balance in new_balances.items()
            ],
        )

    return PostedEntry(
        id=entry_id,
        kind=draft.kind,
        source_type=draft.source_type,
        source_id=draft.source_id,
        metadata=dict(draft.metadata),
        reverses_entry_id=draft.reverses_entry_id,
        posted_at=now,
        postings=tuple(
            sorted(
                (
                    PostedPosting(
                        seq=seq_of[posting.account_id],
                        account_id=posting.account_id,
                        asset_code=accounts[posting.account_id].asset_code,
                        direction=posting.direction,
                        amount=posting.amount,
                        balance_after=new_balances.get(posting.account_id),
                    )
                    for posting in draft.postings
                ),
                key=lambda posted: posted.seq,
            )
        ),
        created=True,
    )


def _check_shape(draft: EntryDraft) -> None:
    if not draft.kind or not draft.source_type or not draft.source_id:
        raise InvalidEntry("an entry needs a kind, a source type and a source id")
    if len(draft.postings) < 2:
        raise InvalidEntry("an entry needs at least two postings")
    seen: set[uuid.UUID] = set()
    for posting in draft.postings:
        if isinstance(posting.amount, bool) or not isinstance(posting.amount, int):
            raise InvalidEntry(f"an amount is an int, not {type(posting.amount).__name__}")
        if not 0 < posting.amount <= MAX_MINOR_UNITS:
            raise InvalidEntry("a posting amount must be positive and storable")
        if posting.account_id in seen:
            # One line per account keeps balance_after unambiguous. Combine the amounts.
            raise InvalidEntry("an account may appear only once in an entry")
        seen.add(posting.account_id)


def _check_balanced(draft: EntryDraft, accounts: Mapping[uuid.UUID, Account]) -> None:
    net: dict[str, int] = defaultdict(int)
    for posting in draft.postings:
        asset = accounts[posting.account_id].asset_code
        net[asset] += posting.amount if posting.direction is Direction.DEBIT else -posting.amount
    unbalanced = sorted(asset for asset, total in net.items() if total != 0)
    if unbalanced:
        raise InvalidEntry(f"debits and credits differ in {', '.join(unbalanced)}")


async def _lock_balances(
    session: AsyncSession, account_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Lock and read the cached balances, always in ascending account id.

    Two transactions that touch the same pair of accounts therefore queue rather than
    deadlock, whichever direction each is moving money in.
    """
    if not account_ids:
        return {}
    rows = await session.execute(
        select(_balances.c.account_id, _balances.c.balance)
        .where(_balances.c.account_id.in_(account_ids))
        .order_by(_balances.c.account_id)
        .with_for_update()
    )
    balances = {row.account_id: row.balance for row in rows}
    missing = set(account_ids) - balances.keys()
    if missing:
        raise LedgerError(f"constrained account without a balance row: {sorted(map(str, missing))}")
    return balances


def _apply(
    postings: Sequence[PostingDraft],
    accounts: Mapping[uuid.UUID, Account],
    balances: Mapping[uuid.UUID, int],
) -> dict[uuid.UUID, int]:
    """The balance of each constrained account after these postings. Raises if any is negative."""
    after: dict[uuid.UUID, int] = {}
    for posting in postings:
        account = accounts[posting.account_id]
        if not account.constrained:
            continue
        current = balances[account.id]
        increases = posting.direction is account.normal_side
        result = current + posting.amount if increases else current - posting.amount
        if result < 0:
            raise InsufficientFunds(
                account_id=account.id,
                asset_code=account.asset_code,
                balance=current,
                required=posting.amount,
            )
        after[account.id] = result
    return after


def _replayed(existing: PostedEntry, draft: EntryDraft) -> PostedEntry:
    """The event was posted before. Return that entry, provided it is the same entry."""
    recorded = {(p.account_id, p.direction, p.amount) for p in existing.postings}
    requested = {(p.account_id, p.direction, p.amount) for p in draft.postings}
    if recorded != requested:
        raise ConflictingEntry(
            f"{draft.source_type} {draft.source_id} ({draft.kind}) is already posted as entry "
            f"{existing.id} with different postings"
        )
    return existing


# --- reading -------------------------------------------------------------------------------


async def find_entry(
    session: AsyncSession, source_type: str, source_id: str, kind: str
) -> PostedEntry | None:
    """The entry posted for a business event, if there is one."""
    rows = await session.execute(
        select(_entries).where(
            _entries.c.source_type == source_type,
            _entries.c.source_id == source_id,
            _entries.c.kind == kind,
        )
    )
    row = rows.mappings().one_or_none()
    return await _entry(session, row) if row is not None else None


async def get_entry(session: AsyncSession, entry_id: uuid.UUID) -> PostedEntry:
    rows = await session.execute(select(_entries).where(_entries.c.id == entry_id))
    row = rows.mappings().one_or_none()
    if row is None:
        raise EntryNotFound(f"no journal entry {entry_id}")
    return await _entry(session, row)


async def _entry(session: AsyncSession, row: RowMapping) -> PostedEntry:
    postings = await session.execute(
        select(_postings).where(_postings.c.entry_id == row["id"]).order_by(_postings.c.seq)
    )
    return PostedEntry(
        id=row["id"],
        kind=row["kind"],
        source_type=row["source_type"],
        source_id=row["source_id"],
        metadata=row["metadata"],
        reverses_entry_id=row["reverses_entry_id"],
        posted_at=row["posted_at"],
        postings=tuple(
            PostedPosting(
                seq=posting["seq"],
                account_id=posting["account_id"],
                asset_code=posting["asset_code"],
                direction=Direction(posting["direction"]),
                amount=posting["amount"],
                balance_after=posting["balance_after"],
            )
            for posting in postings.mappings()
        ),
        created=False,
    )


async def get_balance(session: AsyncSession, account_id: uuid.UUID) -> int:
    return (await get_balances(session, [account_id]))[account_id]


async def get_balances(
    session: AsyncSession, account_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Current balances: the cached value for a constrained account, the sum of its postings
    for any other."""
    accounts = await _load_accounts(session, account_ids)
    cached = [account.id for account in accounts.values() if account.constrained]
    derived = [account.id for account in accounts.values() if not account.constrained]

    balances: dict[uuid.UUID, int] = {}
    if cached:
        rows = await session.execute(
            select(_balances.c.account_id, _balances.c.balance).where(
                _balances.c.account_id.in_(cached)
            )
        )
        balances.update({row.account_id: row.balance for row in rows})
    if derived:
        balances.update(await derive_balances(session, derived))
    return balances


async def derive_balances(
    session: AsyncSession, account_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Balances computed from postings alone, ignoring the cache."""
    wanted = list(account_ids)
    signed = case(
        (_postings.c.direction == _accounts.c.normal_side, _postings.c.amount),
        else_=-_postings.c.amount,
    )
    rows = await session.execute(
        select(_accounts.c.id, func.sum(signed).label("balance"))
        .select_from(_accounts.outerjoin(_postings, _postings.c.account_id == _accounts.c.id))
        .where(_accounts.c.id.in_(wanted))
        .group_by(_accounts.c.id)
    )
    return {row.id: row.balance or 0 for row in rows}


async def statement(
    session: AsyncSession, account_id: uuid.UUID, *, before_seq: int | None = None, limit: int = 50
) -> list[StatementLine]:
    """An account's postings, newest first, starting below ``before_seq``."""
    query = (
        select(
            _postings.c.seq,
            _postings.c.entry_id,
            _postings.c.direction,
            _postings.c.amount,
            _postings.c.balance_after,
            _entries.c.kind,
            _entries.c.source_type,
            _entries.c.source_id,
            _entries.c.metadata,
            _entries.c.posted_at,
        )
        .join(_entries, _entries.c.id == _postings.c.entry_id)
        .where(_postings.c.account_id == account_id)
        .order_by(_postings.c.seq.desc())
        .limit(limit)
    )
    if before_seq is not None:
        query = query.where(_postings.c.seq < before_seq)
    rows = await session.execute(query)
    return [
        StatementLine(
            seq=row.seq,
            entry_id=row.entry_id,
            entry_kind=row.kind,
            source_type=row.source_type,
            source_id=row.source_id,
            metadata=row.metadata,
            direction=Direction(row.direction),
            amount=row.amount,
            balance_after=row.balance_after,
            posted_at=row.posted_at,
        )
        for row in rows
    ]


# --- reversal ------------------------------------------------------------------------------


async def reverse_entry(
    session: AsyncSession, entry_id: uuid.UUID, *, metadata: Mapping[str, Any] | None = None
) -> PostedEntry:
    """Post the mirror image of an entry. History is corrected by adding to it.

    Reversing the same entry again returns the reversal that already exists. A reversal that
    would take a constrained account below zero raises ``InsufficientFunds``, like any entry.
    """
    original = await get_entry(session, entry_id)
    if original.kind == REVERSAL_KIND:
        raise InvalidEntry("a reversal is not reversed; post the original movement again")

    return await post_entry(
        session,
        EntryDraft(
            kind=REVERSAL_KIND,
            # The source is the original entry, so the unique source makes this idempotent.
            source_type="journal_entry",
            source_id=str(original.id),
            postings=tuple(
                PostingDraft(
                    posting.account_id,
                    Direction.CREDIT if posting.direction is Direction.DEBIT else Direction.DEBIT,
                    posting.amount,
                )
                for posting in original.postings
            ),
            metadata=dict(metadata or {}),
            reverses_entry_id=original.id,
        ),
    )
