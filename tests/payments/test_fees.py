"""What a transfer and a withdrawal cost: basis points of the amount, rounded down, and
never less than the minimum configured for the asset."""

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from corridor.payments import fees
from corridor.platform.config import Settings
from corridor.platform.money import MAX_MINOR_UNITS, format_amount

REQUIRED: dict[str, Any] = {
    "database_url": "postgresql+asyncpg://app:example@db.invalid/corridor",  # pragma: allowlist secret
    "redis_url": "redis://cache.invalid:6379/0",
}

amounts = st.integers(1, MAX_MINOR_UNITS)
rates = st.integers(0, 1000)


def configured(bps: int = 0, minimum: int = 0) -> Settings:
    """Settings with a transfer fee, its minimum given in USD cents."""
    return Settings(
        _env_file=None,
        transfer_fee_bps=bps,
        transfer_min_fee={"USD": format_amount(minimum, "USD")},
        **REQUIRED,
    )


def transfer_fee(amount: int, settings: Settings) -> int:
    return fees.transfer_fee(amount, "USD", settings)


def test_transfers_are_free_unless_a_fee_is_configured() -> None:
    settings = Settings(_env_file=None, **REQUIRED)

    assert (settings.transfer_fee_bps, settings.transfer_min_fee) == (0, {})
    assert transfer_fee(1_000_00, settings) == 0


@pytest.mark.parametrize(
    ("amount", "bps", "fee"),
    [
        (100_00, 100, 1_00),
        (100_00, 25, 25),
        (1_99, 100, 1),  # 1.99 cents of fee: the fraction is dropped, never rounded up
        (99, 100, 0),
        (1, 1000, 0),
        (10, 1000, 1),
    ],
)
def test_the_fee_is_basis_points_of_the_amount_rounded_down(
    amount: int, bps: int, fee: int
) -> None:
    assert transfer_fee(amount, configured(bps)) == fee


def test_the_minimum_applies_when_the_percentage_is_smaller() -> None:
    assert transfer_fee(10_00, configured(bps=100, minimum=30)) == 30


def test_the_percentage_applies_when_it_is_larger_than_the_minimum() -> None:
    assert transfer_fee(100_00, configured(bps=100, minimum=30)) == 1_00


def test_a_minimum_alone_is_a_flat_fee() -> None:
    assert transfer_fee(100_00, configured(minimum=15)) == 15


@given(amount=amounts, bps=rates)
def test_the_fee_is_the_exact_floor_and_never_exceeds_the_rate(amount: int, bps: int) -> None:
    fee = transfer_fee(amount, configured(bps))

    assert type(fee) is int
    # The largest whole number of minor units that is not more than amount * bps / 10000,
    # stated without a division so that the check cannot share a rounding error.
    assert fee * 10_000 <= amount * bps < (fee + 1) * 10_000


@given(amount=amounts, bps=rates, minimum=st.integers(0, 10**9))
def test_the_fee_with_a_minimum_is_the_larger_of_the_two(
    amount: int, bps: int, minimum: int
) -> None:
    fee = transfer_fee(amount, configured(bps, minimum))

    assert type(fee) is int
    assert fee == max(minimum, transfer_fee(amount, configured(bps)))


def test_an_amount_beyond_what_a_float_holds_is_charged_exactly() -> None:
    amount = 10**37 + 1

    assert transfer_fee(amount, configured(bps=1)) == 10**33


@pytest.mark.parametrize(
    "bad",
    [
        {"transfer_fee_bps": -1},
        {"transfer_fee_bps": 1001},
        {"withdrawal_fee_bps": -1},
        {"withdrawal_fee_bps": 1001},
        {"transfer_min_fee": {"USD": "-1.00"}},
        {"transfer_min_fee": {"USD": "0.005"}},
        {"transfer_min_fee": {"EUR": "1.00"}},
        {"withdrawal_min_fee": {"USD": "a quarter"}},
        {"withdrawal_min_fee": {"USD": "1e2"}},
        {"withdrawal_min_fee": {"USDC": "0.0000001"}},
        {"withdrawal_min_fee": {"usd": "0.25"}},
    ],
)
def test_a_fee_setting_out_of_range_stops_the_process_from_starting(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **REQUIRED, **bad)


# --- the transfer minimum is per asset -------------------------------------------------------


def test_a_transfer_minimum_is_in_the_asset_it_is_configured_for() -> None:
    settings = Settings(_env_file=None, transfer_min_fee={"USD": "0.30", "USDC": "0.5"}, **REQUIRED)

    assert fees.transfer_fee(10_00, "USD", settings) == 30
    assert fees.transfer_fee(10_000_000, "USDC", settings) == 500_000
    # Nothing is configured for pesos, and a minimum in dollars is not one in pesos.
    assert fees.transfer_fee(10_00, "MXN", settings) == 0


# --- withdrawals -----------------------------------------------------------------------------


def withdrawing(bps: int = 0, **more: Any) -> Settings:
    return Settings(_env_file=None, withdrawal_fee_bps=bps, **more, **REQUIRED)


def test_a_withdrawal_has_a_least_fee_in_each_asset_that_has_a_payout_cost() -> None:
    settings = Settings(_env_file=None, **REQUIRED)

    assert settings.withdrawal_min_fee == {"USD": "0.25", "MXN": "5.00", "USDC": "0.15"}
    assert fees.withdrawal_fee(100_00, "USD", settings) == 25
    assert fees.withdrawal_fee(100_00, "MXN", settings) == 5_00
    assert fees.withdrawal_fee(100_000_000, "USDC", settings) == 150_000
    assert fees.withdrawal_fee(100_00, "BRL", settings) == 0


@pytest.mark.parametrize(
    ("amount", "fee"),
    [
        (1, 25),  # one cent out still costs a payout
        (16_66, 25),  # 24.99 cents of proportional fee: the minimum is more
        (16_67, 25),
        (17_00, 25),
        (17_34, 26),
        (100_00, 1_50),
    ],
)
def test_the_withdrawal_fee_is_the_larger_of_the_percentage_and_the_minimum(
    amount: int, fee: int
) -> None:
    assert fees.withdrawal_fee(amount, "USD", withdrawing(150)) == fee


def test_a_withdrawal_with_no_minimum_configured_pays_the_percentage_alone() -> None:
    settings = withdrawing(150, withdrawal_min_fee={})

    assert fees.withdrawal_fee(1, "USD", settings) == 0
    assert fees.withdrawal_fee(100_00, "USD", settings) == 1_50


def test_the_withdrawal_minimums_are_read_from_the_environment_as_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CORRIDOR_WITHDRAWAL_MIN_FEE", '{"USD": "1.00"}')

    settings = Settings(_env_file=None, **REQUIRED)

    assert fees.withdrawal_fee(10_00, "USD", settings) == 1_00
    assert fees.withdrawal_fee(10_00, "MXN", settings) == 0


@given(amount=amounts, bps=rates, minimum=st.integers(0, 10**9))
def test_the_withdrawal_fee_is_never_less_than_either_part(
    amount: int, bps: int, minimum: int
) -> None:
    settings = withdrawing(bps, withdrawal_min_fee={"USD": format_amount(minimum, "USD")})

    fee = fees.withdrawal_fee(amount, "USD", settings)

    assert type(fee) is int
    assert fee == max(minimum, amount * bps // 10_000)
