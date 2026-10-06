"""The simulator's own money conversion: decimal strings in, exact decimals out."""

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from corridor_sim.errors import ApiError
from corridor_sim.money import SCALES, add, format_amount, parse_amount, subtract, total

assets = st.sampled_from(sorted(SCALES))


def exactly(minor: int, scale: int) -> Decimal:
    """``minor`` units at ``scale`` decimal places, built without any arithmetic: the
    default decimal context would round a long one."""
    return Decimal(f"{minor}E-{scale}")


@pytest.mark.parametrize(
    ("text", "asset", "formatted"),
    [
        ("12.34", "USD", "12.34"),
        ("12.3", "USD", "12.30"),
        ("12", "USD", "12.00"),
        ("0.01", "USD", "0.01"),
        ("007.50", "MXN", "7.50"),
        ("5", "BRL", "5.00"),
        ("1", "USDC", "1.000000"),
        ("0.000001", "USDC", "0.000001"),
        ("1234567.891234", "USDC", "1234567.891234"),
    ],
)
def test_an_amount_is_parsed_exactly_and_formatted_with_the_assets_places(
    text: str, asset: str, formatted: str
) -> None:
    amount = parse_amount(text, asset)

    assert isinstance(amount, Decimal)
    assert amount == Decimal(text)
    assert format_amount(amount, asset) == formatted


@pytest.mark.parametrize(
    "text",
    [
        "",
        " ",
        "12.",
        ".5",
        "-1.00",
        "+1.00",
        "1e3",
        "1E3",
        "1,000.00",
        "1_000",
        " 1.00",
        "1.00 ",
        "1.00\n",
        "1.0.0",
        "NaN",
        "Infinity",
        "0x10",
        "١٢",  # digits, but not ASCII digits
        "\uff11\uff12",  # full-width digits
        "9" * 41,
    ],
)
def test_a_string_that_is_not_a_plain_decimal_is_refused(text: str) -> None:
    with pytest.raises(ApiError) as refusal:
        parse_amount(text, "USD")

    assert (refusal.value.status, refusal.value.code) == (422, "invalid_amount")


@pytest.mark.parametrize(
    "value", [12.34, 12, True, None, Decimal("1.00"), ["1.00"], {"amount": "1.00"}, b"1.00"]
)
def test_anything_but_a_string_is_refused(value: object) -> None:
    # A JSON number has already lost its exactness by the time it is parsed.
    with pytest.raises(ApiError) as refusal:
        parse_amount(value, "USD")

    assert (refusal.value.status, refusal.value.code) == (422, "invalid_amount")


@pytest.mark.parametrize(("text", "asset"), [("0", "USD"), ("0.00", "USD"), ("0.000000", "USDC")])
def test_zero_is_refused(text: str, asset: str) -> None:
    with pytest.raises(ApiError) as refusal:
        parse_amount(text, asset)

    assert refusal.value.code == "invalid_amount"


@pytest.mark.parametrize(
    ("text", "asset"),
    [
        ("12.345", "USD"),
        ("1.000", "USD"),
        ("0.001", "MXN"),
        ("7.123", "BRL"),
        ("1.0000001", "USDC"),
    ],
)
def test_more_decimal_places_than_the_asset_has_is_refused(text: str, asset: str) -> None:
    with pytest.raises(ApiError) as refusal:
        parse_amount(text, asset)

    assert refusal.value.code == "invalid_amount"


@given(asset=assets, extra=st.integers(1, 6), digit=st.sampled_from("0123456789"))
def test_one_decimal_place_too_many_is_always_refused(asset: str, extra: int, digit: str) -> None:
    with pytest.raises(ApiError):
        parse_amount("1." + digit * (SCALES[asset] + extra), asset)


@given(asset=assets, minor=st.integers(1, 10**30))
def test_formatting_then_parsing_returns_the_same_amount(asset: str, minor: int) -> None:
    amount = exactly(minor, SCALES[asset])

    text = format_amount(amount, asset)

    assert len(text.partition(".")[2]) == SCALES[asset]
    assert parse_amount(text, asset) == amount


@pytest.mark.parametrize(
    ("amount", "asset", "text"),
    [
        (Decimal("149.75"), "USD", "149.75"),
        (Decimal("-12.5"), "USD", "-12.50"),
        (Decimal("0"), "USD", "0.00"),
        (Decimal("-0.00"), "USD", "0.00"),
        (Decimal("0.15"), "USDC", "0.150000"),
        (Decimal("1E+3"), "MXN", "1000.00"),
    ],
)
def test_formatting_uses_exactly_the_assets_places(amount: Decimal, asset: str, text: str) -> None:
    assert format_amount(amount, asset) == text


@pytest.mark.parametrize("value", [12.5, 12, "12.50", True])
def test_formatting_refuses_anything_but_a_decimal(value: object) -> None:
    with pytest.raises(TypeError):
        format_amount(value, "USD")  # type: ignore[arg-type]


def test_formatting_refuses_an_amount_finer_than_the_asset() -> None:
    # Nothing in the books can hold a third decimal place of USD; rounding it away for
    # display would hide whatever put it there.
    with pytest.raises(ArithmeticError):
        format_amount(Decimal("1.005"), "USD")


def test_arithmetic_is_exact_far_beyond_the_default_precision() -> None:
    # The default decimal context keeps 28 digits and would round these sums silently.
    huge = parse_amount("9" * 40 + ".99", "USD")
    cent = parse_amount("0.01", "USD")

    assert format_amount(add(huge, cent), "USD") == "1" + "0" * 40 + ".00"
    assert format_amount(subtract(huge, cent), "USD") == "9" * 40 + ".98"
    assert subtract(add(huge, cent), huge) == cent
    assert total([huge, cent, cent, huge]) == Decimal("2" + "0" * 40)
    assert total([]) == Decimal(0)


@given(
    minors=st.lists(st.integers(-(10**35), 10**35), max_size=20),
    scale=st.sampled_from(sorted(set(SCALES.values()))),
)
def test_a_total_agrees_with_integer_arithmetic(minors: list[int], scale: int) -> None:
    amounts = [exactly(minor, scale) for minor in minors]

    assert total(amounts) == exactly(sum(minors), scale)
