"""The transfer fee: basis points of the amount, rounded down, with an optional minimum."""

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from corridor.payments import fees
from corridor.platform.config import Settings
from corridor.platform.money import MAX_MINOR_UNITS

REQUIRED: dict[str, Any] = {
    "database_url": "postgresql+asyncpg://app:example@db.invalid/corridor",  # pragma: allowlist secret
    "redis_url": "redis://cache.invalid:6379/0",
}

amounts = st.integers(1, MAX_MINOR_UNITS)
rates = st.integers(0, 1000)


def configured(bps: int = 0, minimum: int = 0) -> Settings:
    return Settings(
        _env_file=None, transfer_fee_bps=bps, transfer_fee_min_minor=minimum, **REQUIRED
    )


def test_transfers_are_free_unless_a_fee_is_configured() -> None:
    settings = Settings(_env_file=None, **REQUIRED)

    assert (settings.transfer_fee_bps, settings.transfer_fee_min_minor) == (0, 0)
    assert fees.transfer_fee(1_000_00, settings) == 0


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
    assert fees.transfer_fee(amount, configured(bps)) == fee


def test_the_minimum_applies_when_the_percentage_is_smaller() -> None:
    assert fees.transfer_fee(10_00, configured(bps=100, minimum=30)) == 30


def test_the_percentage_applies_when_it_is_larger_than_the_minimum() -> None:
    assert fees.transfer_fee(100_00, configured(bps=100, minimum=30)) == 1_00


def test_a_minimum_alone_is_a_flat_fee() -> None:
    assert fees.transfer_fee(100_00, configured(minimum=15)) == 15


@given(amount=amounts, bps=rates)
def test_the_fee_is_the_exact_floor_and_never_exceeds_the_rate(amount: int, bps: int) -> None:
    fee = fees.transfer_fee(amount, configured(bps))

    assert type(fee) is int
    # The largest whole number of minor units that is not more than amount * bps / 10000,
    # stated without a division so that the check cannot share a rounding error.
    assert fee * 10_000 <= amount * bps < (fee + 1) * 10_000


@given(amount=amounts, bps=rates, minimum=st.integers(0, 10**9))
def test_the_fee_with_a_minimum_is_the_larger_of_the_two(
    amount: int, bps: int, minimum: int
) -> None:
    fee = fees.transfer_fee(amount, configured(bps, minimum))

    assert type(fee) is int
    assert fee == max(minimum, fees.transfer_fee(amount, configured(bps)))


def test_an_amount_beyond_what_a_float_holds_is_charged_exactly() -> None:
    amount = 10**37 + 1

    assert fees.transfer_fee(amount, configured(bps=1)) == 10**33


@pytest.mark.parametrize(
    "bad",
    [
        {"transfer_fee_bps": -1},
        {"transfer_fee_bps": 1001},
        {"transfer_fee_min_minor": -1},
    ],
)
def test_a_fee_setting_out_of_range_stops_the_process_from_starting(bad: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **REQUIRED, **bad)
