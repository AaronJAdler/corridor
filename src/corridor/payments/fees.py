"""What Corridor charges for a movement. Integer arithmetic only: a fee is money."""

from typing import Final

from corridor.platform.config import Settings

_BASIS_POINTS: Final = 10_000


def transfer_fee(amount: int, settings: Settings) -> int:
    """The fee on a transfer of ``amount`` minor units, charged to the sender on top of it.

    Basis points of the amount, rounded down so that rounding never charges more than the
    configured rate, and never less than the configured minimum.
    """
    proportional = amount * settings.transfer_fee_bps // _BASIS_POINTS
    return max(settings.transfer_fee_min_minor, proportional)
