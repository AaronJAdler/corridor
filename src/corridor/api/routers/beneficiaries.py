"""Beneficiary endpoints: save a bank account to withdraw to, and list the saved ones.

The account number arrives in the request body, goes to the bank, and is gone. It is a
``SecretStr`` here so that a body which ends up in a log line, an error or a debugger shows
asterisks where the number was.
"""

import uuid
from datetime import datetime
from typing import Annotated, Self

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import payments
from corridor.api.container import Container
from corridor.api.deps import Db, get_container, require
from corridor.api.idempotency import IdempotencyKey
from corridor.api.schemas import DisplayName, Text
from corridor.identity import Principal, Scope
from corridor.platform.logging import get_logger
from corridor.platform.pagination import DEFAULT_LIMIT, Page

log = get_logger(__name__)

router = APIRouter(prefix="/v1/beneficiaries", tags=["withdrawals"])

BeneficiaryWriter = Annotated[Principal, Depends(require(Scope.BENEFICIARIES_WRITE))]
BeneficiaryReader = Annotated[Principal, Depends(require(Scope.BENEFICIARIES_READ))]

# Far above any real value. These bound what a request can make the server read; what an
# account number must look like is the bank's to say.
_MAX_FIELD_LENGTH = 64


class BeneficiaryRequest(BaseModel):
    # An unknown field is refused rather than dropped: a client that misspells one learns
    # of it at once.
    model_config = ConfigDict(extra="forbid", frozen=True)

    asset: Annotated[Text, Field(max_length=_MAX_FIELD_LENGTH)]
    holder_name: DisplayName
    # The account number, the CLABE or the PIX key, whichever the asset's rail uses.
    account_number: Annotated[SecretStr, Field(min_length=1, max_length=_MAX_FIELD_LENGTH)]
    # For USD only.
    routing_number: Annotated[
        SecretStr | None, Field(min_length=1, max_length=_MAX_FIELD_LENGTH)
    ] = None


class BeneficiaryResponse(BaseModel):
    id: uuid.UUID
    asset: str
    holder_name: str
    # The last characters of the account, for the user to recognise it by.
    account_mask: str
    created_at: datetime

    @classmethod
    def of(cls, beneficiary: payments.Beneficiary) -> Self:
        return cls(
            id=beneficiary.id,
            asset=beneficiary.asset,
            holder_name=beneficiary.holder_name,
            account_mask=beneficiary.account_mask,
            created_at=beneficiary.created_at,
        )


class BeneficiaryPageResponse(BaseModel):
    items: list[BeneficiaryResponse]
    # Send it back as ``cursor`` for the next page. Null on the last page.
    next_cursor: str | None


@router.post("", status_code=201, summary="Save a bank account to withdraw to")
async def create_beneficiary(
    body: BeneficiaryRequest,
    principal: BeneficiaryWriter,
    container: Annotated[Container, Depends(get_container)],
    key: IdempotencyKey,
) -> BeneficiaryResponse:
    # The key is not recorded here, as a money-moving request's is: it is handed to the
    # bank, which answers a repeat with the account it registered the first time. Storing
    # a response would mean opening a transaction around the call to the bank.
    beneficiary = await payments.create_beneficiary(
        container.db,
        principal,
        bank=container.bank,
        asset=body.asset,
        holder_name=body.holder_name,
        account_number=body.account_number.get_secret_value(),
        routing_number=(
            body.routing_number.get_secret_value() if body.routing_number is not None else None
        ),
        idempotency_key=key,
    )
    log.info("beneficiary.created", beneficiary_id=str(beneficiary.id), asset=beneficiary.asset)
    return BeneficiaryResponse.of(beneficiary)


@router.get("", summary="The saved bank accounts, newest first")
async def list_beneficiaries(
    principal: BeneficiaryReader,
    db: Db,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> BeneficiaryPageResponse:
    async def work(session: AsyncSession) -> Page[payments.Beneficiary]:
        return await payments.list_beneficiaries(session, principal, cursor=cursor, limit=limit)

    page = await db.run(work)
    return BeneficiaryPageResponse(
        items=[BeneficiaryResponse.of(beneficiary) for beneficiary in page.items],
        next_cursor=page.next_cursor,
    )
