import pytest
from hypothesis import given
from hypothesis import strategies as st

from corridor.platform.money import (
    ASSETS,
    MAX_MINOR_UNITS,
    Asset,
    InvalidAmount,
    UnknownAsset,
    format_amount,
    get_asset,
    parse_amount,
)

assets = st.sampled_from(sorted(ASSETS.values(), key=lambda asset: asset.code))


@pytest.mark.parametrize(
    ("text", "asset", "minor"),
    [
        ("12.34", "USD", 1234),
        ("12.3", "USD", 1230),
        ("12", "USD", 1200),
        ("0.01", "USD", 1),
        ("1", "USDC", 1_000_000),
        ("0.000001", "USDC", 1),
        ("1234567.891234", "USDC", 1_234_567_891_234),
        ("007.50", "MXN", 750),
    ],
)
def test_parses_decimal_strings_exactly(text: str, asset: str, minor: int) -> None:
    assert parse_amount(text, asset) == minor


@pytest.mark.parametrize(
    "text",
    [
        "",
        " ",
        "12.345",  # more precision than USD has
        "12.",
        ".5",
        "-1.00",
        "+1.00",
        "1e3",
        "1,000.00",
        " 1.00",
        "1.00 ",
        "1.0.0",
        "NaN",
        "Infinity",
        "0x10",
        "١٢",  # digits, but not ASCII digits
        "1" * 39,
    ],
)
def test_refuses_anything_that_is_not_an_exact_amount(text: str) -> None:
    with pytest.raises(InvalidAmount):
        parse_amount(text, "USD")


def test_refuses_a_number_rather_than_a_string() -> None:
    # JSON numbers lose precision; the API contract is a string and nothing coerces.
    with pytest.raises(InvalidAmount):
        parse_amount(12.34, "USD")  # type: ignore[arg-type]


def test_zero_is_refused_unless_asked_for() -> None:
    with pytest.raises(InvalidAmount, match="greater than zero"):
        parse_amount("0.00", "USD")
    assert parse_amount("0.00", "USD", allow_zero=True) == 0


def test_the_largest_storable_amount_is_the_limit() -> None:
    usd = get_asset("USD")
    largest = format_amount(MAX_MINOR_UNITS, usd)
    assert parse_amount(largest, usd) == MAX_MINOR_UNITS
    with pytest.raises(InvalidAmount, match="too large"):
        parse_amount(format_amount(MAX_MINOR_UNITS + 1, usd), usd)


def test_an_unknown_asset_is_refused() -> None:
    with pytest.raises(UnknownAsset):
        parse_amount("1.00", "DOGE")


def test_the_refusal_of_an_unknown_asset_does_not_repeat_what_was_sent() -> None:
    # The code may come from a client or from a provider's response, and the message goes
    # back to a client and into logs.
    with pytest.raises(UnknownAsset) as refusal:
        get_asset("DOGE<script>")

    assert (refusal.value.status, refusal.value.code) == (422, "unknown_asset")
    assert refusal.value.detail == "That is not a supported asset."
    assert "DOGE" not in str(refusal.value)


@pytest.mark.parametrize(
    ("minor", "asset", "text"),
    [
        (1234, "USD", "12.34"),
        (5, "USD", "0.05"),
        (0, "USD", "0.00"),
        (-1250, "USD", "-12.50"),
        (1, "USDC", "0.000001"),
        (1_000_000, "USDC", "1.000000"),
    ],
)
def test_formats_with_exactly_the_assets_decimal_places(minor: int, asset: str, text: str) -> None:
    assert format_amount(minor, asset) == text


def test_formats_an_asset_with_no_decimal_places() -> None:
    assert format_amount(1500, Asset("JPY", 0, "fiat")) == "1500"
    assert parse_amount("1500", Asset("JPY", 0, "fiat")) == 1500


@pytest.mark.parametrize("value", [12.5, True, "12"])
def test_formatting_refuses_anything_but_an_int(value: object) -> None:
    with pytest.raises(TypeError):
        format_amount(value, "USD")  # type: ignore[arg-type]


@given(asset=assets, minor=st.integers(min_value=1, max_value=MAX_MINOR_UNITS))
def test_format_then_parse_returns_the_same_integer(asset: Asset, minor: int) -> None:
    assert parse_amount(format_amount(minor, asset), asset) == minor


@given(
    asset=assets,
    whole=st.integers(min_value=0, max_value=10**20),
    fraction_digits=st.text(alphabet="0123456789", max_size=6),
)
def test_parse_agrees_with_integer_arithmetic(
    asset: Asset, whole: int, fraction_digits: str
) -> None:
    fraction_digits = fraction_digits[: asset.decimals]
    text = f"{whole}.{fraction_digits}" if fraction_digits else str(whole)
    expected = whole * 10**asset.decimals + int(fraction_digits.ljust(asset.decimals, "0") or 0)

    assert parse_amount(text, asset, allow_zero=True) == expected


@given(
    asset=assets, extra=st.integers(min_value=1, max_value=6), digit=st.sampled_from("0123456789")
)
def test_one_decimal_place_too_many_is_always_refused(asset: Asset, extra: int, digit: str) -> None:
    text = "1." + digit * (asset.decimals + extra)
    with pytest.raises(InvalidAmount):
        parse_amount(text, asset)
