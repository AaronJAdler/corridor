"""What Corridor charges for a movement. Integer arithmetic only: a fee is money."""

from collections.abc import Mapping
from typing import Final

from corridor.platform.config import Settings
from corridor.platform.money import parse_amount

_BASIS_POINTS: Final = 10_000


def transfer_fee(amount: int, asset: str, settings: Settings) -> int:
    """The fee on a transfer of ``amount`` minor units, charged to the sender on top of it.

    Basis points of the amount, rounded down so that rounding never charges more than the
    configured rate, and never less than the minimum configured for the asset.
    """
    proportional = amount * settings.transfer_fee_bps // _BASIS_POINTS
    return max(_minimum(settings.transfer_min_fee, asset), proportional)


def withdrawal_fee(amount: int, asset: str, settings: Settings) -> int:
    """What Corridor charges for a withdrawal of ``amount`` minor units, on top of it.

    Basis points of the amount, rounded down, and never less than the minimum configured
    for the asset: a small withdrawal costs a payout like any other.
    """
    proportional = amount * settings.withdrawal_fee_bps // _BASIS_POINTS
    return max(_minimum(settings.withdrawal_min_fee, asset), proportional)


def _minimum(minimums: Mapping[str, str], asset: str) -> int:
    """The least fee for an asset, in its minor units. Nothing, if none is configured."""
    text = minimums.get(asset)
    # The settings refused anything that does not parse, so this cannot fail here.
    return parse_amount(text, asset, allow_zero=True) if text is not None else 0
