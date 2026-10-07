"""Admin endpoints for risk: the deny list and the limits.

Every route here needs an administrator. What an admin reads or changes is written to the
audit log in the transaction that serves it.
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal, Self

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import audit, identity, risk
from corridor.api.deps import AdminPrincipal, Db
from corridor.api.schemas import Text
from corridor.platform.errors import Conflict, InvalidRequest
from corridor.platform.logging import get_logger
from corridor.platform.money import format_amount, parse_amount
from corridor.platform.pagination import DEFAULT_LIMIT, Page

log = get_logger(__name__)

router = APIRouter(prefix="/v1/admin/risk", tags=["admin"])

# Far above any real value. These bound what a request can make the server read.
_MAX_VALUE_LENGTH = 320
_MAX_NOTE_LENGTH = 500
# What limits are written in.
_USD = "USD"


class AgentLimitNotSettable(Conflict):
    """An admin tried to set an agent's limits. They are the caps of the policy its owner
    set: a rule written here would be replaced by the owner's next change, and until then
    the policy the owner sees would not be the one that is enforced."""

    code = "agent_limit_not_settable"
    title = "Agent limits are set by the owner"

    def __init__(self) -> None:
        super().__init__("An agent's limits are set by its owner's policy, and only there.")


class _Request(BaseModel):
    # An unknown field is refused rather than dropped: these change who may be paid, and
    # how much may move.
    model_config = ConfigDict(extra="forbid", frozen=True)


class DenylistRequest(_Request):
    kind: risk.PartyKind
    # A name, an address or an account number, as it is written. It is stored, and
    # compared, in the form screening reduces it to.
    value: Annotated[Text, Field(min_length=1, max_length=_MAX_VALUE_LENGTH)]
    # What screening answers for this party: refuse the movement, or hold it for review.
    outcome: Literal["deny", "review"]
    note: Annotated[Text, Field(max_length=_MAX_NOTE_LENGTH)] | None = None


class DenylistEntryResponse(BaseModel):
    id: uuid.UUID
    kind: risk.PartyKind
    # In the form screening compares it in.
    value: str
    outcome: Literal["deny", "review"]
    note: str | None
    created_at: datetime

    @classmethod
    def of(cls, entry: risk.DenylistEntry) -> Self:
        return cls(
            id=entry.id,
            kind=entry.kind,
            value=entry.value,
            outcome=entry.outcome,
            note=entry.note,
            created_at=entry.created_at,
        )


class DenylistPageResponse(BaseModel):
    items: list[DenylistEntryResponse]
    # Send it back as ``cursor`` for the next page. Null on the last page.
    next_cursor: str | None


class LimitRequest(_Request):
    scope: risk.LimitScope
    # Exactly the one its scope names.
    tier: Annotated[StrictInt, Field(ge=0, le=2)] | None = None
    user_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None
    # The kind of movement the rule is for. Null, or left out, for every kind.
    kind: risk.MovementKind | None = None
    # Decimal strings in US dollars. Both are required, and null sets no limit of that
    # sort: leaving a limit off is a decision, and is written out as one.
    per_transaction_usd: Annotated[Text, Field(max_length=_MAX_VALUE_LENGTH)] | None
    daily_usd: Annotated[Text, Field(max_length=_MAX_VALUE_LENGTH)] | None

    @model_validator(mode="after")
    def _names_its_subject(self) -> Self:
        subjects = {"tier": self.tier, "user": self.user_id, "agent": self.agent_id}
        named = {name for name, value in subjects.items() if value is not None}
        if named != {self.scope}:
            raise ValueError(
                f"a rule of scope {self.scope} names its {self.scope} and nothing else"
            )
        return self


class LimitResponse(BaseModel):
    id: uuid.UUID
    scope: risk.LimitScope
    tier: int | None
    user_id: uuid.UUID | None
    agent_id: uuid.UUID | None
    kind: risk.MovementKind | None
    # Decimal strings in US dollars. Null where the rule sets no limit of that sort.
    per_transaction_usd: str | None
    daily_usd: str | None

    @classmethod
    def of(cls, limit: risk.Limit) -> Self:
        return cls(
            id=limit.id,
            scope=limit.scope,
            tier=limit.tier,
            user_id=limit.user_id,
            agent_id=limit.agent_id,
            kind=limit.kind,
            per_transaction_usd=_dollars(limit.per_tx_usd),
            daily_usd=_dollars(limit.daily_usd),
        )


class LimitPageResponse(BaseModel):
    items: list[LimitResponse]
    # Send it back as ``cursor`` for the next page. Null on the last page.
    next_cursor: str | None


def _dollars(cents: int | None) -> str | None:
    return None if cents is None else format_amount(cents, _USD)


def _cents(dollars: str | None) -> int | None:
    # Zero is a limit: it lets nothing through.
    return None if dollars is None else parse_amount(dollars, _USD, allow_zero=True)


@router.post("/denylist", status_code=201, summary="List a party, or change what is said of it")
async def add_to_denylist(
    body: DenylistRequest, principal: AdminPrincipal, db: Db
) -> DenylistEntryResponse:
    # No idempotency key: a party is listed once, and listing it again only writes the
    # same answer over the one it has.
    async def work(session: AsyncSession) -> risk.DenylistEntry:
        try:
            entry = await risk.add_to_denylist(
                session, kind=body.kind, value=body.value, outcome=body.outcome, note=body.note
            )
        except ValueError:
            # Nothing was left of the value once it was reduced to what screening compares.
            raise InvalidRequest(
                "The value has nothing in it that screening compares.", field="value"
            ) from None
        await audit.record(
            session,
            actor=audit.Actor.admin(principal.actor_id),
            action="risk.denylist_changed",
            resource_type="denylist_entry",
            resource_id=entry.id,
            # The kind and the answer, never the value: who is listed is read from the
            # list itself, by someone allowed to.
            details={"kind": entry.kind, "outcome": entry.outcome},
        )
        return entry

    entry = await db.run(work)
    log.info("risk.denylist_changed", kind=entry.kind, outcome=entry.outcome)
    return DenylistEntryResponse.of(entry)


@router.get("/denylist", summary="The deny list, newest first")
async def list_denylist(
    principal: AdminPrincipal,
    db: Db,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> DenylistPageResponse:
    async def work(session: AsyncSession) -> Page[risk.DenylistEntry]:
        page = await risk.list_denylist(session, cursor=cursor, limit=limit)
        await audit.record(
            session,
            actor=audit.Actor.admin(principal.actor_id),
            action="risk.denylist_listed",
            resource_type="denylist_entry",
            details={"returned": len(page.items)},
        )
        return page

    page = await db.run(work)
    return DenylistPageResponse(
        items=[DenylistEntryResponse.of(entry) for entry in page.items],
        next_cursor=page.next_cursor,
    )


@router.put("/limits", summary="Set the limits of a tier or a user")
async def set_limit(body: LimitRequest, principal: AdminPrincipal, db: Db) -> LimitResponse:
    # Before the transaction: an amount that cannot be read changes nothing.
    per_tx_usd, daily_usd = _cents(body.per_transaction_usd), _cents(body.daily_usd)

    if body.scope == "agent":
        raise AgentLimitNotSettable

    async def work(session: AsyncSession) -> risk.Limit:
        if body.user_id is not None:
            # A rule for nobody would sit in the table until an id happened to match it.
            await identity.get_user(session, body.user_id)
        limit = await risk.set_limit(
            session,
            scope=body.scope,
            tier=body.tier,
            user_id=body.user_id,
            agent_id=body.agent_id,
            kind=body.kind,
            per_tx_usd=per_tx_usd,
            daily_usd=daily_usd,
        )
        await audit.record(
            session,
            actor=audit.Actor.admin(principal.actor_id),
            action="risk.limit_set",
            principal_id=body.user_id,
            resource_type="limit",
            resource_id=limit.id,
            details={
                "scope": limit.scope,
                "tier": limit.tier,
                "agent_id": None if limit.agent_id is None else str(limit.agent_id),
                "kind": limit.kind,
                "per_transaction_usd": _dollars(limit.per_tx_usd),
                "daily_usd": _dollars(limit.daily_usd),
            },
        )
        return limit

    limit = await db.run(work)
    log.info("risk.limit_set", scope=limit.scope, kind=limit.kind)
    return LimitResponse.of(limit)


@router.get("/limits", summary="Every limit rule, newest first")
async def list_limits(
    principal: AdminPrincipal,
    db: Db,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> LimitPageResponse:
    async def work(session: AsyncSession) -> Page[risk.Limit]:
        page = await risk.list_limits(session, cursor=cursor, limit=limit)
        await audit.record(
            session,
            actor=audit.Actor.admin(principal.actor_id),
            action="risk.limits_listed",
            resource_type="limit",
            details={"returned": len(page.items)},
        )
        return page

    page = await db.run(work)
    return LimitPageResponse(
        items=[LimitResponse.of(found) for found in page.items], next_cursor=page.next_cursor
    )
