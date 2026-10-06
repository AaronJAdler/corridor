"""Statements: the entries on a wallet's available balance, newest first, a page at a time.

Pages are cut on the posting ``seq``. For a user's account that is the order of commits, so
an entry posted after a page was read is newer than everything on it and belongs to no later
page: paging neither repeats an entry nor skips one, whatever arrives in between.
"""

import uuid
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from corridor import ledger
from corridor.platform.pagination import (
    DEFAULT_LIMIT,
    InvalidCursor,
    Page,
    clamp_limit,
    decode_cursor,
    encode_cursor,
)
from corridor.wallets.service import get_wallet
from corridor.wallets.types import StatementEntry

CURSOR_KIND: Final = "wallet_entries"

# The largest value the ``seq`` column holds. A position beyond it is not one this module
# handed out, and binding it would be a database error instead of a refusal.
_MAX_SEQ: Final = 2**63 - 1


async def list_entries(
    session: AsyncSession,
    user_id: uuid.UUID,
    asset: str,
    *,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> Page[StatementEntry]:
    """One page of the user's statement in an asset, starting after ``cursor``."""
    limit = clamp_limit(limit)
    wallet = await get_wallet(session, user_id, asset)
    account_id = wallet.available_account_id
    # The cursor is tied to the account, so one from another user's statement, or from this
    # user's statement in another asset, is refused instead of being read as a place in this one.
    scope = str(account_id)
    before_seq = _position(cursor, scope) if cursor is not None else None

    # One more than the page, to learn whether anything follows it without a second query.
    lines = await ledger.statement(session, account_id, before_seq=before_seq, limit=limit + 1)
    shown = lines[:limit]
    more = len(lines) > limit
    return Page(
        items=tuple(_entry(line, asset) for line in shown),
        next_cursor=(
            encode_cursor(kind=CURSOR_KIND, scope=scope, position=shown[-1].seq) if more else None
        ),
    )


def _position(cursor: str, scope: str) -> int:
    position = decode_cursor(cursor, kind=CURSOR_KIND, scope=scope)
    if not isinstance(position, int) or not 0 < position <= _MAX_SEQ:
        raise InvalidCursor
    return position


def _entry(line: ledger.StatementLine, asset: str) -> StatementEntry:
    if line.balance_after is None:  # pragma: no cover - an available account is constrained
        raise ledger.LedgerError(f"posting {line.seq} on a wallet account has no balance")
    return StatementEntry(
        seq=line.seq,
        entry_id=line.entry_id,
        kind=line.entry_kind,
        asset=asset,
        direction=line.direction,
        amount=line.amount,
        balance_after=line.balance_after,
        posted_at=line.posted_at,
    )
