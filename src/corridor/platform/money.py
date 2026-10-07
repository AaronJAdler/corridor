"""Money: assets, and the only conversions between API strings and ledger integers.

An amount inside the system is an ``int`` count of the asset's smallest unit. An amount at
the API boundary is a decimal string in major units. There is no floating point on any path
between the two.
"""

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

from corridor.platform.errors import InvalidRequest

AssetKind = Literal["fiat", "stablecoin"]


@dataclass(frozen=True, slots=True)
class Asset:
    code: str
    decimals: int
    kind: AssetKind

    @property
    def scale(self) -> int:
        """Minor units in one major unit."""
        scale: int = 10**self.decimals
        return scale


# Mirrored by the ``assets`` table, which the ledger migration seeds. A test compares the two.
ASSETS: Final = MappingProxyType(
    {
        "USD": Asset("USD", 2, "fiat"),
        "MXN": Asset("MXN", 2, "fiat"),
        "BRL": Asset("BRL", 2, "fiat"),
        "USDC": Asset("USDC", 6, "stablecoin"),
    }
)

# The largest value NUMERIC(38,0) holds.
MAX_MINOR_UNITS: Final = 10**38 - 1

# Digits, optionally a point and more digits. No sign, exponent, separator or whitespace.
# The length bounds keep a hostile string from becoming an enormous integer.
_AMOUNT: Final = re.compile(r"(?P<whole>[0-9]{1,38})(?:\.(?P<fraction>[0-9]{1,38}))?", re.ASCII)


class UnknownAsset(InvalidRequest):
    code = "unknown_asset"
    title = "Unknown asset"


class InvalidAmount(InvalidRequest):
    code = "invalid_amount"
    title = "Invalid amount"


def get_asset(code: str) -> Asset:
    try:
        return ASSETS[code]
    except KeyError:
        # What was asked for is not repeated: it may be anything a client or a provider sent.
        raise UnknownAsset("That is not a supported asset.") from None


def parse_amount(text: str, asset: Asset | str, *, allow_zero: bool = False) -> int:
    """Convert a decimal string in major units to minor units, exactly.

    Refuses anything that would need rounding: ``"1.005"`` is not a USD amount.
    """
    asset = get_asset(asset) if isinstance(asset, str) else asset
    match = _AMOUNT.fullmatch(text) if isinstance(text, str) else None
    if match is None:
        raise InvalidAmount('An amount is a decimal string such as "12.50".')

    fraction = match["fraction"] or ""
    if len(fraction) > asset.decimals:
        raise InvalidAmount(f"{asset.code} has {asset.decimals} decimal places.")

    minor = int(match["whole"]) * asset.scale + int(fraction.ljust(asset.decimals, "0") or 0)
    if minor > MAX_MINOR_UNITS:
        raise InvalidAmount("The amount is too large.")
    if minor == 0 and not allow_zero:
        raise InvalidAmount("The amount must be greater than zero.")
    return minor


def format_amount(minor: int, asset: Asset | str) -> str:
    """Convert minor units to a decimal string with exactly the asset's decimal places."""
    asset = get_asset(asset) if isinstance(asset, str) else asset
    if isinstance(minor, bool) or not isinstance(minor, int):
        raise TypeError(f"minor units are an int, not {type(minor).__name__}")

    sign = "-" if minor < 0 else ""
    whole, fraction = divmod(abs(minor), asset.scale)
    if asset.decimals == 0:
        return f"{sign}{whole}"
    return f"{sign}{whole}.{fraction:0{asset.decimals}d}"
