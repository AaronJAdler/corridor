"""The arithmetic of a conversion: exact, in decimals, and never in the customer's favour."""

from decimal import Decimal

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from corridor import fx
from corridor.fx import AmountTooSmall
from corridor.platform.money import ASSETS

ASSET_CODES = sorted(ASSETS)

amounts = st.integers(min_value=1, max_value=10**15)
# A mid as a provider writes one: at most six places, from a millionth to a million.
mids = st.integers(min_value=1, max_value=10**12).map(lambda n: Decimal(n) / Decimal(10**6))
spreads = st.integers(min_value=0, max_value=1000)
pairs = st.permutations(ASSET_CODES).map(lambda codes: (codes[0], codes[1]))


def bought(amount: int, sell: str, buy: str, rate: Decimal) -> int:
    """What the amount buys, where an amount too small to buy anything buys nothing."""
    try:
        return fx.buy_amount(amount, sell, buy, rate)
    except AmountTooSmall:
        return 0


@pytest.mark.parametrize(
    ("sell_minor", "sell", "buy", "rate", "expected"),
    [
        # 100.00 USD at 0.995 is 99.5 USDC, exactly.
        (100_00, "USD", "USDC", "0.995", 99_500_000),
        # 1.000001 USDC at 0.995 is 0.99500099... USD: 99 cents, and the rest is not paid.
        (1_000_001, "USDC", "USD", "0.995", 99),
        # 1000.00 MXN at 0.057681145 is 57.681145 USD.
        (1_000_00, "MXN", "USD", "0.057681145", 57_68),
        # 0.005 USDC is half a cent: rounding to nearest would pay a cent for it.
        (1_995_000, "USDC", "USD", "1", 1_99),
        (100_00, "USD", "MXN", "17.16375", 1_716_37),
        (1, "USD", "USDC", "1", 10_000),
        (10**30, "USD", "MXN", "17.16375", 1716375 * 10**25),
    ],
)
def test_the_buy_amount_is_the_product_rounded_down_to_the_buy_assets_minor_unit(
    sell_minor: int, sell: str, buy: str, rate: str, expected: int
) -> None:
    assert fx.buy_amount(sell_minor, sell, buy, Decimal(rate)) == expected


def test_the_customer_rate_is_the_mid_less_the_spread() -> None:
    assert fx.customer_rate(Decimal("17.25"), 50) == Decimal("17.16375")
    assert fx.customer_rate(Decimal("17.25"), 0) == Decimal("17.25")
    assert fx.customer_rate(Decimal("0.057971"), 1000) == Decimal("0.0521739")


def test_the_customer_rate_keeps_every_digit_of_a_long_mid() -> None:
    mid = Decimal("123456789012.123456789012")

    assert fx.customer_rate(mid, 1) == mid * Decimal("0.9999")


@pytest.mark.parametrize(("sell_minor", "sell", "buy"), [(1, "MXN", "USD"), (4_999, "USDC", "USD")])
def test_an_amount_that_buys_less_than_one_minor_unit_is_refused(
    sell_minor: int, sell: str, buy: str
) -> None:
    with pytest.raises(AmountTooSmall) as refusal:
        fx.buy_amount(sell_minor, sell, buy, Decimal("0.05"))

    assert (refusal.value.status, refusal.value.code) == (422, "amount_too_small")


def test_the_result_is_an_int() -> None:
    assert type(fx.buy_amount(100_00, "USD", "MXN", Decimal("17.16375"))) is int


@pytest.mark.parametrize("rate", [17.25, 17, "17.25"])
def test_a_rate_that_is_not_a_decimal_is_an_error(rate: object) -> None:
    with pytest.raises(TypeError):
        fx.buy_amount(100_00, "USD", "MXN", rate)  # type: ignore[arg-type]


@pytest.mark.parametrize("amount", [100.0, Decimal(100), True, "100"])
def test_an_amount_that_is_not_an_int_is_an_error(amount: object) -> None:
    with pytest.raises(TypeError):
        fx.buy_amount(amount, "USD", "MXN", Decimal("17.25"))  # type: ignore[arg-type]


@pytest.mark.parametrize("mid", [17.25, 17])
def test_a_mid_that_is_not_a_decimal_is_an_error(mid: object) -> None:
    with pytest.raises(TypeError):
        fx.customer_rate(mid, 50)  # type: ignore[arg-type]


@pytest.mark.parametrize("rate", ["0", "-1", "NaN", "Infinity"])
def test_a_rate_that_is_not_a_positive_number_is_an_error(rate: str) -> None:
    with pytest.raises(ValueError, match="rate"):
        fx.buy_amount(100_00, "USD", "MXN", Decimal(rate))


@given(amount=amounts, pair=pairs, mid=mids, spread=spreads, spread_back=spreads)
@example(amount=5_000, pair=("USDC", "USD"), mid=Decimal(1), spread=0, spread_back=0)
@example(amount=1, pair=("USD", "MXN"), mid=Decimal("0.5"), spread=0, spread_back=0)
def test_converting_there_and_back_never_returns_more_than_the_start(
    amount: int, pair: tuple[str, str], mid: Decimal, spread: int, spread_back: int
) -> None:
    start, other = pair

    there = bought(amount, start, other, fx.customer_rate(mid, spread))
    back = (
        bought(there, other, start, fx.customer_rate(Decimal(1) / mid, spread_back)) if there else 0
    )

    assert back <= amount


@given(amount=amounts, more=st.integers(min_value=0, max_value=10**15), pair=pairs, mid=mids)
def test_a_larger_amount_never_buys_less(
    amount: int, more: int, pair: tuple[str, str], mid: Decimal
) -> None:
    sell, buy = pair
    rate = fx.customer_rate(mid, 50)

    assert bought(amount, sell, buy, rate) <= bought(amount + more, sell, buy, rate)


@given(amount=amounts, pair=pairs, mid=mids, spread=spreads)
def test_the_result_is_never_a_float(
    amount: int, pair: tuple[str, str], mid: Decimal, spread: int
) -> None:
    sell, buy = pair
    rate = fx.customer_rate(mid, spread)

    assert type(rate) is Decimal
    assert type(bought(amount, sell, buy, rate)) is int


@pytest.mark.parametrize(
    ("rate", "text"),
    [
        ("17.163750000", "17.16375"),
        ("20.39750", "20.3975"),
        ("100", "100"),
        ("0.000001", "0.000001"),
    ],
)
def test_a_rate_is_written_as_a_plain_decimal_without_padding(rate: str, text: str) -> None:
    assert fx.format_rate(Decimal(rate)) == text
