"""FX endpoints: ask for a quote, convert it, and read a conversion.

A quote needs a rate, and a rate may need a call to the rate source, so it is fetched
before any transaction is opened. A conversion moves money, so it carries an
``Idempotency-Key``: the key, the conversion and everything it writes commit together.
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse

from corridor import fx
from corridor.api.container import Container
from corridor.api.deps import Db, Redis, SettingsDep, get_container, require
from corridor.api.idempotency import IdempotencyKey, StoredResponse, run_idempotent, to_response
from corridor.api.middleware import route_template
from corridor.api.schemas import Text
from corridor.identity import Principal, Scope
from corridor.platform.ids import new_id
from corridor.platform.logging import get_logger
from corridor.platform.money import format_amount, parse_amount

log = get_logger(__name__)

router = APIRouter(prefix="/v1/fx", tags=["fx"])

FxReader = Annotated[Principal, Depends(require(Scope.FX_READ))]
FxConverter = Annotated[Principal, Depends(require(Scope.FX_CONVERT))]

# Far above any real value. This bounds what a request can make the server read; what an
# asset or an amount may be is decided by the code that uses it.
_MAX_FIELD_LENGTH = 320


class QuoteRequest(BaseModel):
    # An unknown field is refused rather than dropped: a client that sends a rate of its
    # own learns that it was not used.
    model_config = ConfigDict(extra="forbid", frozen=True)

    sell_asset: Annotated[Text, Field(max_length=_MAX_FIELD_LENGTH)]
    buy_asset: Annotated[Text, Field(max_length=_MAX_FIELD_LENGTH)]
    # A decimal string in major units of the sell asset. A JSON number is refused: it
    # would have been through a float before it got here.
    sell_amount: Annotated[Text, Field(max_length=_MAX_FIELD_LENGTH)]


class QuoteResponse(BaseModel):
    id: uuid.UUID
    sell_asset: str
    # Decimal strings in major units, each with exactly its asset's decimal places.
    sell_amount: str
    buy_asset: str
    buy_amount: str
    # Units of the buy asset for one unit of the sell asset, after the spread.
    rate: str
    expires_at: datetime


class ConversionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    quote_id: uuid.UUID


class ConversionResponse(BaseModel):
    id: uuid.UUID
    quote_id: uuid.UUID
    sell_asset: str
    sell_amount: str
    buy_asset: str
    buy_amount: str
    rate: str
    created_at: datetime


def _render(conversion: fx.Conversion) -> ConversionResponse:
    return ConversionResponse(
        id=conversion.id,
        quote_id=conversion.quote_id,
        sell_asset=conversion.sell_asset,
        sell_amount=format_amount(conversion.sell_amount, conversion.sell_asset),
        buy_asset=conversion.buy_asset,
        buy_amount=format_amount(conversion.buy_amount, conversion.buy_asset),
        rate=fx.format_rate(conversion.rate),
        created_at=conversion.created_at,
    )


@router.post(
    "/quotes",
    status_code=201,
    summary="Ask what an amount of one asset buys of another",
)
async def create_quote(
    body: QuoteRequest,
    principal: FxReader,
    container: Annotated[Container, Depends(get_container)],
    db: Db,
    redis: Redis,
    settings: SettingsDep,
) -> QuoteResponse:
    # Before a rate is asked for: a request that could never be quoted costs no call.
    fx.check_pair(body.sell_asset, body.buy_asset)
    sell_amount = parse_amount(body.sell_amount, body.sell_asset)
    if container.rates is None:
        raise fx.RateUnavailable

    # A network call, perhaps: made here, with no transaction open.
    rate = await fx.get_mid(
        redis, container.rates, body.sell_asset, body.buy_asset, settings=settings
    )

    async def work(session: AsyncSession) -> fx.Quote:
        return await fx.create_quote(
            session,
            principal,
            sell_asset=body.sell_asset,
            buy_asset=body.buy_asset,
            sell_amount=sell_amount,
            rate=rate,
            settings=settings,
        )

    quote = await db.run(work)
    log.info("fx.quoted", sell_asset=quote.sell_asset, buy_asset=quote.buy_asset)
    return QuoteResponse(
        id=quote.id,
        sell_asset=quote.sell_asset,
        sell_amount=format_amount(quote.sell_amount, quote.sell_asset),
        buy_asset=quote.buy_asset,
        buy_amount=format_amount(quote.buy_amount, quote.buy_asset),
        rate=fx.format_rate(quote.rate),
        expires_at=quote.expires_at,
    )


@router.post(
    "/conversions",
    status_code=201,
    response_model=ConversionResponse,
    summary="Convert a quote",
)
async def create_conversion(
    request: Request,
    body: ConversionRequest,
    principal: FxConverter,
    db: Db,
    key: IdempotencyKey,
) -> JSONResponse:
    async def work(session: AsyncSession) -> StoredResponse:
        conversion = await fx.convert(
            session,
            principal,
            quote_id=body.quote_id,
            # Made here, inside the work: a replay returns the stored response and never
            # reaches this line, so one key names one conversion.
            conversion_id=new_id(),
        )
        return StoredResponse(201, _render(conversion).model_dump(mode="json"), {})

    stored, replayed = await run_idempotent(
        db,
        actor_id=principal.actor_id,
        key=key,
        method=request.method,
        route=route_template(request.scope),
        body=await request.body(),
        work=work,
    )
    log.info("fx.conversion_requested", status=stored.status_code, replayed=replayed)
    return to_response(stored, replayed, request)


@router.get("/conversions/{conversion_id}", summary="One conversion the user made")
async def get_conversion(
    conversion_id: uuid.UUID, principal: FxReader, db: Db
) -> ConversionResponse:
    async def work(session: AsyncSession) -> ConversionResponse:
        return _render(await fx.get_conversion(session, principal, conversion_id))

    return await db.run(work)
