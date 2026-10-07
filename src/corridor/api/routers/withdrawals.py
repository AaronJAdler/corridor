"""Withdrawal endpoints: ask for money to be sent out, call a request back, and read them.

A request is answered ``202``: the funds are reserved and the withdrawal is recorded, in
the transaction that records the idempotency key, and sending it to the provider is the
worker's. What became of it is read back from the withdrawal itself.
"""

import uuid
from datetime import datetime
from typing import Annotated, Self

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse

from corridor import agents, payments
from corridor.api.deps import Db, SettingsDep, require
from corridor.api.idempotency import IdempotencyKey, StoredResponse, run_idempotent, to_response
from corridor.api.middleware import route_template
from corridor.api.ratelimit import money_rate_limit, money_recall_rate_limit
from corridor.api.routers.approvals import AwaitingApprovalResponse, awaiting_approval
from corridor.api.schemas import Text
from corridor.identity import Principal, Scope
from corridor.platform.ids import new_id
from corridor.platform.logging import get_logger
from corridor.platform.money import format_amount, parse_amount
from corridor.platform.pagination import DEFAULT_LIMIT, Page

log = get_logger(__name__)

# Every route here is limited by who is acting, as well as by where the request came from.
# The limit is named on each route because canceling has one of its own.
router = APIRouter(prefix="/v1/withdrawals", tags=["withdrawals"])
_LIMITED = [Depends(money_rate_limit)]

WithdrawalCreator = Annotated[Principal, Depends(require(Scope.WITHDRAWALS_CREATE))]
WithdrawalReader = Annotated[Principal, Depends(require(Scope.WITHDRAWALS_READ))]

# Far above any real value. These bound what a request can make the server read; what an
# amount or an address may be is decided by the code that uses it.
_MAX_FIELD_LENGTH = 320


class WithdrawalRequest(BaseModel):
    # An unknown field is refused rather than dropped: a client that misspells "to_address"
    # learns of it before anything is held.
    model_config = ConfigDict(extra="forbid", frozen=True)

    asset: Annotated[Text, Field(max_length=_MAX_FIELD_LENGTH)]
    # A decimal string in major units. A JSON number is refused: it would have been
    # through a float before it got here.
    amount: Annotated[Text, Field(max_length=_MAX_FIELD_LENGTH)]
    # One of the two: a saved beneficiary for a bank asset, an address for a stablecoin.
    beneficiary_id: uuid.UUID | None = None
    to_address: Annotated[Text | None, Field(max_length=_MAX_FIELD_LENGTH)] = None


class WithdrawalResponse(BaseModel):
    id: uuid.UUID
    status: payments.WithdrawalStatus
    asset: str
    # Decimal strings in major units, with exactly the asset's decimal places. ``amount``
    # is what is sent out; the wallet is debited ``amount`` plus ``fee``.
    amount: str
    fee: str
    kind: payments.FlowKind
    beneficiary_id: uuid.UUID | None
    to_address: str | None
    # Why a failed withdrawal failed, as the provider named it.
    failure_reason: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, withdrawal: payments.Withdrawal) -> Self:
        return cls(
            id=withdrawal.id,
            status=withdrawal.status,
            asset=withdrawal.asset,
            amount=format_amount(withdrawal.amount, withdrawal.asset),
            fee=format_amount(withdrawal.fee, withdrawal.asset),
            kind=withdrawal.kind,
            beneficiary_id=withdrawal.beneficiary_id,
            to_address=withdrawal.to_address,
            failure_reason=withdrawal.failure_reason,
            created_at=withdrawal.created_at,
            updated_at=withdrawal.updated_at,
        )


class WithdrawalPageResponse(BaseModel):
    items: list[WithdrawalResponse]
    # Send it back as ``cursor`` for the next page. Null on the last page.
    next_cursor: str | None


@router.post(
    "",
    status_code=202,
    response_model=WithdrawalResponse,
    summary="Withdraw to a saved bank account or to an address",
    dependencies=_LIMITED,
    responses={
        202: {
            "model": WithdrawalResponse | AwaitingApprovalResponse,
            "description": "The withdrawal, with its funds reserved. Or, for an agent that"
            " asked for more than its owner lets it send unseen, the request the owner"
            " has been asked to approve: nothing is reserved.",
        }
    },
)
async def request_withdrawal(
    request: Request,
    body: WithdrawalRequest,
    principal: WithdrawalCreator,
    db: Db,
    settings: SettingsDep,
    key: IdempotencyKey,
) -> JSONResponse:
    # Before the key is read: a request that could never be performed does not use it up.
    amount = parse_amount(body.amount, body.asset)

    async def work(session: AsyncSession) -> StoredResponse:
        if principal.agent_id is not None:
            # After the key's lock and before anything is reserved: the owner's policy
            # says whether this agent may send this much there, and whether now.
            intent = agents.WithdrawalIntent(
                asset=body.asset,
                amount=amount,
                beneficiary_id=body.beneficiary_id,
                to_address=body.to_address,
            )
            decision = await agents.check_policy(
                session, principal, "withdrawal", body.asset, amount, intent.destination
            )
            if decision.requires_approval:
                return awaiting_approval(
                    await agents.request_approval(session, principal, intent, settings=settings)
                )
        withdrawal = await payments.request_withdrawal(
            session,
            principal,
            # Made here, inside the work: a replay returns the stored response and never
            # reaches this line, so one key names one withdrawal.
            withdrawal_id=new_id(),
            asset=body.asset,
            amount=amount,
            beneficiary_id=body.beneficiary_id,
            to_address=body.to_address,
            settings=settings,
        )
        return StoredResponse(202, WithdrawalResponse.of(withdrawal).model_dump(mode="json"), {})

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
    log.info("withdrawal.requested", status=stored.status_code, replayed=replayed)
    return to_response(stored, replayed, request)


@router.get("", summary="The withdrawals requested, newest first", dependencies=_LIMITED)
async def list_withdrawals(
    principal: WithdrawalReader,
    db: Db,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> WithdrawalPageResponse:
    async def work(session: AsyncSession) -> Page[payments.Withdrawal]:
        return await payments.list_withdrawals(session, principal, cursor=cursor, limit=limit)

    page = await db.run(work)
    return WithdrawalPageResponse(
        items=[WithdrawalResponse.of(withdrawal) for withdrawal in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/{withdrawal_id}", summary="One withdrawal", dependencies=_LIMITED)
async def get_withdrawal(
    withdrawal_id: uuid.UUID, principal: WithdrawalReader, db: Db
) -> WithdrawalResponse:
    withdrawal = await db.run(
        lambda session: payments.get_withdrawal(session, principal, withdrawal_id)
    )
    return WithdrawalResponse.of(withdrawal)


@router.post(
    "/{withdrawal_id}/cancel",
    summary="Call back a withdrawal that has not been sent",
    # Not the limit of the routes above: that one refuses a write while Redis is down.
    dependencies=[Depends(money_recall_rate_limit)],
)
async def cancel_withdrawal(
    withdrawal_id: uuid.UUID, principal: WithdrawalCreator, db: Db
) -> WithdrawalResponse:
    # No idempotency key: a second cancellation finds the withdrawal canceled and is
    # refused, so repeating the request cannot release anything twice.
    withdrawal = await db.run(
        lambda session: payments.cancel_withdrawal(session, principal, withdrawal_id)
    )
    log.info("withdrawal.canceled", withdrawal_id=str(withdrawal_id))
    return WithdrawalResponse.of(withdrawal)
