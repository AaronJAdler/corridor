"""Transfer endpoints: send money to another user, and read what was sent and received.

Creating a transfer is the first request that moves money, so it is the first that must
carry an ``Idempotency-Key``. The key, the transfer and everything the transfer writes
commit in one transaction; a retry gets the stored answer and moves nothing.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Self

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse

from corridor import agents, identity, payments
from corridor.api.deps import Db, SettingsDep, require
from corridor.api.idempotency import IdempotencyKey, StoredResponse, run_idempotent, to_response
from corridor.api.middleware import route_template
from corridor.api.ratelimit import money_rate_limit
from corridor.api.routers.approvals import AwaitingApprovalResponse, awaiting_approval
from corridor.api.schemas import Text
from corridor.identity import Principal, Scope
from corridor.platform.ids import new_id
from corridor.platform.logging import get_logger
from corridor.platform.money import format_amount, parse_amount
from corridor.platform.pagination import DEFAULT_LIMIT, Page

log = get_logger(__name__)

# Every route here is limited by who is acting, as well as by where the request came from.
router = APIRouter(
    prefix="/v1/transfers", tags=["transfers"], dependencies=[Depends(money_rate_limit)]
)

TransferCreator = Annotated[Principal, Depends(require(Scope.TRANSFERS_CREATE))]
TransferReader = Annotated[Principal, Depends(require(Scope.TRANSFERS_READ))]

# Far above any real value. These bound what a request can make the server read; what a
# recipient, an amount or a memo may be is decided by the code that uses it.
_MAX_FIELD_LENGTH = 320
_MAX_MEMO_FIELD_LENGTH = 4096


class TransferRequest(BaseModel):
    # An unknown field is refused rather than dropped: a client that misspells "memo"
    # learns of it before the money has moved without one.
    model_config = ConfigDict(extra="forbid", frozen=True)

    # A handle, with or without the @, an email address or a user id.
    recipient: Annotated[Text, Field(min_length=1, max_length=_MAX_FIELD_LENGTH)]
    asset: Annotated[Text, Field(max_length=_MAX_FIELD_LENGTH)]
    # A decimal string in major units. A JSON number is refused: it would have been
    # through a float before it got here.
    amount: Annotated[Text, Field(max_length=_MAX_FIELD_LENGTH)]
    memo: Annotated[Text | None, Field(max_length=_MAX_MEMO_FIELD_LENGTH)] = None


class PartyResponse(BaseModel):
    id: uuid.UUID
    handle: str

    @classmethod
    def of(cls, user: identity.User) -> Self:
        return cls(id=user.id, handle=user.handle)


class TransferResponse(BaseModel):
    id: uuid.UUID
    status: payments.TransferStatus
    sender: PartyResponse
    recipient: PartyResponse
    asset: str
    # Decimal strings in major units, with exactly the asset's decimal places. The
    # recipient received ``amount``; the sender paid ``amount`` plus ``fee``.
    amount: str
    fee: str
    memo: str | None
    created_at: datetime


class TransferPageResponse(BaseModel):
    items: list[TransferResponse]
    # Send it back as ``cursor`` for the next page. Null on the last page.
    next_cursor: str | None


async def _render(
    session: AsyncSession, transfers: Sequence[payments.Transfer]
) -> list[TransferResponse]:
    """The transfers as the API shows them, with each side's handle looked up once."""
    users = await identity.get_users(
        session, {party for t in transfers for party in (t.sender_id, t.recipient_id)}
    )
    return [
        TransferResponse(
            id=transfer.id,
            status=transfer.status,
            sender=PartyResponse.of(users[transfer.sender_id]),
            recipient=PartyResponse.of(users[transfer.recipient_id]),
            asset=transfer.asset,
            amount=format_amount(transfer.amount, transfer.asset),
            fee=format_amount(transfer.fee, transfer.asset),
            memo=transfer.memo,
            created_at=transfer.created_at,
        )
        for transfer in transfers
    ]


@router.post(
    "",
    status_code=201,
    response_model=TransferResponse,
    summary="Send money to another user",
    responses={
        202: {
            "model": AwaitingApprovalResponse,
            "description": "An agent asked for more than its owner lets it send unseen."
            " Nothing has moved; the owner has been asked.",
        }
    },
)
async def create_transfer(
    request: Request,
    body: TransferRequest,
    principal: TransferCreator,
    db: Db,
    settings: SettingsDep,
    key: IdempotencyKey,
) -> JSONResponse:
    # Before the key is read: a request that could never be performed does not use it up.
    amount = parse_amount(body.amount, body.asset)

    async def work(session: AsyncSession) -> StoredResponse:
        if principal.agent_id is not None:
            # After the key's lock and before anything is moved: the owner's policy says
            # whether this agent may pay this recipient this much, and whether now.
            decision = await agents.check_policy(
                session,
                principal,
                "transfer",
                body.asset,
                amount,
                agents.Recipient("user", body.recipient),
            )
            if decision.requires_approval:
                intent = agents.TransferIntent(
                    recipient=body.recipient, asset=body.asset, amount=amount, memo=body.memo
                )
                return awaiting_approval(
                    await agents.request_approval(session, principal, intent, settings=settings)
                )
        transfer = await payments.create_transfer(
            session,
            principal,
            # Made here, inside the work: a replay returns the stored response and never
            # reaches this line, so one key names one transfer.
            transfer_id=new_id(),
            recipient=body.recipient,
            asset=body.asset,
            amount=amount,
            memo=body.memo,
            settings=settings,
        )
        (rendered,) = await _render(session, [transfer])
        return StoredResponse(201, rendered.model_dump(mode="json"), {})

    stored, replayed = await run_idempotent(
        db,
        actor_id=principal.actor_id,
        key=key,
        method=request.method,
        route=route_template(request.scope),
        path=request.url.path,
        body=await request.body(),
        work=work,
    )
    log.info("transfer.requested", status=stored.status_code, replayed=replayed)
    return to_response(stored, replayed, request)


@router.get("", summary="The transfers sent and received, newest first")
async def list_transfers(
    principal: TransferReader,
    db: Db,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> TransferPageResponse:
    async def work(session: AsyncSession) -> TransferPageResponse:
        page: Page[payments.Transfer] = await payments.list_transfers(
            session, principal, cursor=cursor, limit=limit
        )
        return TransferPageResponse(
            items=await _render(session, page.items), next_cursor=page.next_cursor
        )

    return await db.run(work)


@router.get("/{transfer_id}", summary="One transfer the user sent or received")
async def get_transfer(
    transfer_id: uuid.UUID, principal: TransferReader, db: Db
) -> TransferResponse:
    async def work(session: AsyncSession) -> TransferResponse:
        transfer = await payments.get_transfer(session, principal, transfer_id)
        (rendered,) = await _render(session, [transfer])
        return rendered

    return await db.run(work)
