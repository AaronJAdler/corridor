"""Deposit endpoints: where to send money, and what has arrived.

A deposit is never created here. It begins at the provider, and Corridor learns of it by
webhook; these endpoints only read.
"""

import uuid
from datetime import datetime
from typing import Annotated, Self

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import payments
from corridor.api.container import Container
from corridor.api.deps import Db, get_container, require
from corridor.api.ratelimit import money_rate_limit
from corridor.identity import Principal, Scope
from corridor.platform.money import format_amount
from corridor.platform.pagination import DEFAULT_LIMIT, Page

# Every route here is limited by who is acting, as well as by where the request came from.
instructions_router = APIRouter(
    prefix="/v1/deposit-instructions", tags=["deposits"], dependencies=[Depends(money_rate_limit)]
)
router = APIRouter(prefix="/v1/deposits", tags=["deposits"])

DepositReader = Annotated[Principal, Depends(require(Scope.DEPOSITS_READ))]

# Far above any real asset code. It bounds what a request can make the server read.
_MAX_ASSET_LENGTH = 32


class InstructionResponse(BaseModel):
    asset: str
    kind: payments.FlowKind
    # For a bank asset: the rail, the bank and the account to pay into. For a stablecoin:
    # the network and the address to send to.
    details: dict[str, str]

    @classmethod
    def of(cls, instruction: payments.DepositInstruction) -> Self:
        return cls(
            asset=instruction.asset, kind=instruction.kind, details=dict(instruction.details)
        )


@instructions_router.get("", summary="Where to send an asset to deposit it")
async def get_deposit_instruction(
    asset: Annotated[str, Query(max_length=_MAX_ASSET_LENGTH)],
    principal: DepositReader,
    container: Annotated[Container, Depends(get_container)],
) -> InstructionResponse:
    instruction = await payments.get_deposit_instruction(
        container.db, principal, asset, bank=container.bank, custody=container.custody
    )
    return InstructionResponse.of(instruction)


class DepositResponse(BaseModel):
    id: uuid.UUID
    asset: str
    # A decimal string in major units, with exactly the asset's decimal places.
    amount: str
    kind: payments.FlowKind
    # Only a completed deposit is in the wallet. A pending one has been seen on its chain
    # and is not final; a returned one was taken back by the bank that sent it.
    status: payments.DepositStatus
    # Set for an on-chain deposit.
    tx_hash: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, deposit: payments.Deposit) -> Self:
        return cls(
            id=deposit.id,
            asset=deposit.asset,
            amount=format_amount(deposit.amount, deposit.asset),
            kind=deposit.kind,
            status=deposit.status,
            tx_hash=deposit.tx_hash,
            created_at=deposit.created_at,
            updated_at=deposit.updated_at,
        )


class DepositPageResponse(BaseModel):
    items: list[DepositResponse]
    # Send it back as ``cursor`` for the next page. Null on the last page.
    next_cursor: str | None


@router.get("", summary="The deposits received, newest first")
async def list_deposits(
    principal: DepositReader,
    db: Db,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> DepositPageResponse:
    async def work(session: AsyncSession) -> Page[payments.Deposit]:
        return await payments.list_deposits(session, principal, cursor=cursor, limit=limit)

    page = await db.run(work)
    return DepositPageResponse(
        items=[DepositResponse.of(deposit) for deposit in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/{deposit_id}", summary="One deposit")
async def get_deposit(deposit_id: uuid.UUID, principal: DepositReader, db: Db) -> DepositResponse:
    deposit = await db.run(lambda session: payments.get_deposit(session, principal, deposit_id))
    return DepositResponse.of(deposit)
