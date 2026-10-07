"""The purge of idempotency keys, run by the worker against a table that is the API's.

This is the one deliberate exception to "tables are private to their module". The table
belongs to ``corridor.api``, but the purge is scheduled work, and the API process must not
run scheduled work: it would run once per API replica and stop when the API is scaled to
nothing. The worker cannot import the API, which sits beside it in the layering, so the
statement is written out here as plain SQL and touches nothing but the row's age.
"""

from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession


async def purge_idempotency_keys(session: AsyncSession, *, older_than: datetime) -> int:
    """Delete the keys created before ``older_than``, and say how many.

    After this a purged key is a new request again, which is why keys are kept for as long
    as a client could reasonably still be retrying.
    """
    deleted = await session.execute(
        text("DELETE FROM idempotency_keys WHERE created_at < :cutoff"), {"cutoff": older_than}
    )
    return cast(CursorResult[Any], deleted).rowcount
