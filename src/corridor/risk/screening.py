"""Screening: whether Corridor deals with the other party to a movement, and the reviews
of the movements it would not let through unseen.

The deny list is a table of names, addresses and account numbers, each stored in the one
form screening compares in. An entry answers ``deny`` or ``review``; a party that is not
on the list is ``clear``.
"""

import re
import unicodedata
import uuid
from typing import Any, Final, Literal, cast

from sqlalchemy import RowMapping, Select, Table, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from corridor.platform.clock import utcnow
from corridor.platform.ids import new_id
from corridor.platform.logging import get_logger
from corridor.risk.errors import ReviewAlreadyResolved, ReviewNotFound
from corridor.risk.models import DenylistRow, ReviewRow
from corridor.risk.types import PartyKind, Review, ScreeningOutcome, SubjectType

log = get_logger(__name__)

# Core tables: every statement against them is written out below.
_denylist = cast(Table, DenylistRow.__table__)
_reviews = cast(Table, ReviewRow.__table__)

_PARTY_KINDS: Final = frozenset({"name", "address", "account"})
_WHITESPACE: Final = re.compile(r"\s+")
_NOT_ALPHANUMERIC: Final = re.compile(r"[\W_]+")


def normalise(kind: PartyKind, value: str) -> str:
    """The form a party is listed and looked up in, so that a difference in case, spacing
    or punctuation does not get a listed party past the list.

    A name is compared without regard to case, to the width or composition of its
    characters, or to how its words are spaced. An address is compared without regard to
    case. An account number is compared by its letters and digits alone.
    """
    if kind not in _PARTY_KINDS:
        raise ValueError(f"screening does not know a party of kind {kind!r}")
    text = unicodedata.normalize("NFKC", value)
    if kind == "name":
        return _WHITESPACE.sub(" ", text).strip().casefold()
    if kind == "address":
        return text.strip().casefold()
    return _NOT_ALPHANUMERIC.sub("", text).upper()


async def screen_party(session: AsyncSession, *, kind: PartyKind, value: str) -> ScreeningOutcome:
    """Whether a party may be dealt with: ``clear``, or what the deny list says of it."""
    normalised = normalise(kind, value)
    if not normalised:
        # Nothing was given, so there is nothing that could be on a list.
        return "clear"
    found = await session.execute(
        select(_denylist.c.outcome).where(
            _denylist.c.kind == kind, _denylist.c.value_normalised == normalised
        )
    )
    outcome: Literal["deny", "review"] | None = found.scalar_one_or_none()
    if outcome is None:
        return "clear"
    # The kind and the answer, never the value: a listed name is not for the logs.
    log.info("risk.party_screened", kind=kind, outcome=outcome)
    return outcome


async def add_to_denylist(
    session: AsyncSession,
    *,
    kind: PartyKind,
    value: str,
    outcome: Literal["deny", "review"],
    note: str | None = None,
) -> None:
    """List a party, or change what the list says of one that is on it already."""
    if outcome not in ("deny", "review"):
        raise ValueError(f"a listed party is denied or reviewed, not {outcome!r}")
    normalised = normalise(kind, value)
    if not normalised:
        raise ValueError("a listed party has a value")
    await session.execute(
        pg_insert(_denylist)
        .values(
            id=new_id(),
            kind=kind,
            value_normalised=normalised,
            outcome=outcome,
            note=note,
            created_at=utcnow(),
        )
        .on_conflict_do_update(
            constraint="uq_risk_denylist_kind_value_normalised",
            set_={"outcome": outcome, "note": note},
        )
    )


async def open_review(
    session: AsyncSession,
    *,
    subject_type: SubjectType,
    subject_id: uuid.UUID,
    outcome: Literal["deny", "review"],
    user_id: uuid.UUID | None = None,
) -> Review:
    """Record that a movement waits for an operator, and why. A movement has one review:
    asking again returns it as it stands, and never reopens one that was resolved."""
    await session.execute(
        pg_insert(_reviews)
        .values(
            id=new_id(),
            subject_type=subject_type,
            subject_id=subject_id,
            user_id=user_id,
            outcome=outcome,
            status="open",
            created_at=utcnow(),
            resolved_at=None,
        )
        .on_conflict_do_nothing(constraint="uq_risk_reviews_subject_type_subject_id")
    )
    found = await session.execute(_of(subject_type, subject_id))
    return _review(found.mappings().one())


async def find_review(
    session: AsyncSession, subject_type: SubjectType, subject_id: uuid.UUID
) -> Review | None:
    found = await session.execute(_of(subject_type, subject_id))
    row = found.mappings().one_or_none()
    return _review(row) if row is not None else None


async def is_cleared(
    session: AsyncSession, subject_type: SubjectType, subject_id: uuid.UUID
) -> bool:
    """Whether a movement may go ahead as far as screening is concerned: it was never put
    under review, or an operator cleared it. An open review and a rejected one both say no.
    """
    review = await find_review(session, subject_type, subject_id)
    return review is None or review.status == "cleared"


async def resolve_review(
    session: AsyncSession,
    *,
    subject_type: SubjectType,
    subject_id: uuid.UUID,
    cleared: bool,
) -> Review:
    """An operator's decision on an open review: cleared, or rejected. It is made once."""
    # One statement, so two operators deciding at the same moment cannot both decide.
    updated = await session.execute(
        update(_reviews)
        .where(
            _reviews.c.subject_type == subject_type,
            _reviews.c.subject_id == subject_id,
            _reviews.c.status == "open",
        )
        .values(status="cleared" if cleared else "rejected", resolved_at=utcnow())
        .returning(_reviews)
    )
    row = updated.mappings().one_or_none()
    if row is None:
        if await find_review(session, subject_type, subject_id) is None:
            raise ReviewNotFound
        raise ReviewAlreadyResolved
    return _review(row)


def _of(subject_type: SubjectType, subject_id: uuid.UUID) -> Select[Any]:
    return select(_reviews).where(
        _reviews.c.subject_type == subject_type, _reviews.c.subject_id == subject_id
    )


def _review(row: RowMapping) -> Review:
    return Review(
        id=row["id"],
        subject_type=row["subject_type"],
        subject_id=row["subject_id"],
        user_id=row["user_id"],
        outcome=row["outcome"],
        status=row["status"],
        created_at=row["created_at"],
        resolved_at=row["resolved_at"],
    )
