"""The rate source adapter against the real simulator."""

from datetime import timedelta
from decimal import Decimal

import pytest

from corridor.providers import ProviderOutcomeUnknown, ProviderRejected, RateSource, SimRates
from tests.providers.conftest import START, Sim


def test_the_adapter_is_a_rate_source_named_simfx(rates: SimRates) -> None:
    port: RateSource = rates
    assert port.name == "simfx"


async def test_a_rate_is_an_exact_decimal_with_the_time_it_was_updated(
    rates: SimRates, sim: Sim
) -> None:
    await sim.control("POST", "/fx/rates", {"base": "USD", "quote": "MXN", "mid": "17.2534"})

    rate = await rates.get_rate("USD", "MXN")

    assert (rate.base, rate.quote) == ("USD", "MXN")
    assert isinstance(rate.mid, Decimal)
    assert str(rate.mid) == "17.253400"
    assert rate.as_of == START


async def test_as_of_follows_the_providers_clock(rates: SimRates, sim: Sim) -> None:
    await sim.advance(90)

    rate = await rates.get_rate("USDC", "BRL")

    assert rate.as_of == START + timedelta(seconds=90)
    assert rate.mid > 0


async def test_a_pair_the_source_does_not_serve_is_rejected(rates: SimRates) -> None:
    with pytest.raises(ProviderRejected) as refused:
        await rates.get_rate("USD", "USD")

    assert (refused.value.status, refused.value.code) == (404, "unknown_pair")
    assert (refused.value.provider, refused.value.operation) == ("simfx", "get_rate")


async def test_a_failing_rate_source_leaves_the_rate_unknown(rates: SimRates, sim: Sim) -> None:
    await sim.inject("fx.get_rate", "error")

    with pytest.raises(ProviderOutcomeUnknown):
        await rates.get_rate("USD", "MXN")
