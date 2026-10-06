"""The rate source adapter for the simulated FX feed, ``/fx/v1`` of the provider contract."""

from decimal import Decimal
from typing import Final

import httpx
from pydantic import AwareDatetime, Field

from corridor.platform.config import Settings
from corridor.providers.http import (
    Document,
    ProviderClient,
    ResponseMismatch,
    in_utc,
    require_echo,
    segment,
)
from corridor.providers.types import Rate


class _RateDocument(Document):
    base: str
    quote: str
    # A plain decimal string: no sign, no exponent, nothing a float has touched. The bound
    # on its length keeps a hostile string from becoming an enormous number.
    mid: str = Field(pattern=r"^[0-9]{1,12}(\.[0-9]{1,12})?$")
    as_of: AwareDatetime


class SimRates:
    """Implements ``RateSource``."""

    name: Final = "simfx"

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self._http = ProviderClient(
            provider=self.name,
            base_url=settings.fx_rates_url,
            api_key=settings.fx_rates_api_key,
            timeout_seconds=settings.provider_timeout_seconds,
            client=client,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get_rate(self, base: str, quote: str) -> Rate:
        def convert(document: _RateDocument) -> Rate:
            require_echo("base", base, document.base)
            require_echo("quote", quote, document.quote)
            mid = Decimal(document.mid)
            # A rate of nothing would price every conversion at zero.
            if mid <= 0:
                raise ResponseMismatch("'mid' is not a positive rate")
            return Rate(
                base=document.base, quote=document.quote, mid=mid, as_of=in_utc(document.as_of)
            )

        return await self._http.get(
            "get_rate", f"/fx/v1/rates/{segment(base)}/{segment(quote)}", _RateDocument, convert
        )
