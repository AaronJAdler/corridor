"""Which adapter the factory builds for each port, and what it refuses."""

import pytest
from pydantic import SecretStr

from corridor.platform.config import INCREASE_SANDBOX_URL, Settings
from corridor.providers import (
    ProviderMisconfigured,
    SimBank,
    SimCustody,
    SimRates,
    build_bank_rail,
    build_custodian,
    build_rate_source,
)

# Made up. Nothing here opens a connection.
KEY = SecretStr("placeholder-provider-key-" + "0" * 12)  # pragma: allowlist secret
STORES: dict[str, object] = {
    "_env_file": None,
    "environment": "test",
    "database_url": SecretStr("postgresql://unused.invalid/unused"),
    "redis_url": SecretStr("redis://unused.invalid/0"),
}


async def test_the_simulator_is_what_each_port_gets_by_default() -> None:
    settings = Settings(
        **STORES,  # type: ignore[arg-type]
        bank_rail_url="http://sim.invalid",
        custody_url="http://sim.invalid",
        fx_rates_url="http://sim.invalid",
        bank_rail_api_key=KEY,
        custody_api_key=KEY,
        fx_rates_api_key=KEY,
    )

    bank, custody, rates = (
        build_bank_rail(settings),
        build_custodian(settings),
        build_rate_source(settings),
    )
    try:
        assert isinstance(bank, SimBank)
        assert isinstance(custody, SimCustody)
        assert isinstance(rates, SimRates)
        assert (bank.name, custody.name, rates.name) == ("simbank", "simcustody", "simfx")
    finally:
        for adapter in (bank, custody, rates):
            assert adapter is not None
            await adapter.aclose()


def test_a_provider_with_no_address_is_left_out() -> None:
    settings = Settings(**STORES)  # type: ignore[arg-type]

    assert build_bank_rail(settings) is None
    assert build_custodian(settings) is None
    assert build_rate_source(settings) is None


def sandbox_settings(address: str) -> Settings:
    """Settings that name the sandbox with an address no validator was asked about.

    ``model_construct`` skips validation, which is how a settings object could come to hold
    what the loader would have refused.
    """
    return Settings.model_construct(
        bank_rail_provider="increase_sandbox",
        increase_base_url=address,
        increase_api_key=KEY,
        bank_rail_url=None,
    )


@pytest.mark.parametrize(
    "address", ["https://api.increase.com", "https://sandbox.increase.com/", "http://localhost"]
)
def test_the_factory_refuses_another_address_even_when_the_settings_were_not_checked(
    address: str,
) -> None:
    with pytest.raises(ProviderMisconfigured) as failure:
        build_bank_rail(sandbox_settings(address))

    assert "not the Increase sandbox address" in str(failure.value)


def test_the_sandbox_mode_never_falls_back_to_the_simulator() -> None:
    # Until the adapter exists, choosing the sandbox is an error and not a quiet simulator,
    # even when the simulator's address is also set.
    settings = Settings.model_construct(
        bank_rail_provider="increase_sandbox",
        increase_base_url=INCREASE_SANDBOX_URL,
        increase_api_key=KEY,
        bank_rail_url="http://sim.invalid",
        bank_rail_api_key=KEY,
    )

    with pytest.raises(ProviderMisconfigured) as failure:
        build_bank_rail(settings)

    assert "not implemented yet" in str(failure.value)
