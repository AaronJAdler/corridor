"""The arithmetic of a conversion.

Everything here is exact decimal arithmetic on integers and ``Decimal``s. A float is
refused wherever one could get in, and the one rounding there is goes down, so a conversion
can take a fraction of a minor unit from the customer and can never give one away.
"""

from decimal import ROUND_DOWN, ROUND_FLOOR, Decimal, localcontext
from typing import Final

from corridor.fx.errors import AmountTooSmall
from corridor.platform.money import get_asset

# Far more digits than any product here has: the largest amount has 38, and a rate made
# from a provider's mid and a spread has under 30. If something longer ever arrives, the
# context truncates it, which is still down.
_PRECISION: Final = 120

_BASIS_POINTS: Final = Decimal(10_000)


def customer_rate(mid: Decimal, spread_bps: int) -> Decimal:
    """The rate the customer gets: the mid-market rate less the spread."""
    if not isinstance(mid, Decimal):
        raise TypeError(f"a rate is a Decimal, not {type(mid).__name__}")
    if isinstance(spread_bps, bool) or not isinstance(spread_bps, int):
        raise TypeError(f"a spread is an int of basis points, not {type(spread_bps).__name__}")
    with localcontext(prec=_PRECISION, rounding=ROUND_DOWN):
        return mid * (1 - Decimal(spread_bps) / _BASIS_POINTS)


def format_rate(rate: Decimal) -> str:
    """A rate as a plain decimal string, without the zeros a provider pads its mid with."""
    with localcontext(prec=_PRECISION, rounding=ROUND_DOWN):
        return format(rate.normalize(), "f")


def buy_amount(sell_minor: int, sell_asset: str, buy_asset: str, customer_rate: Decimal) -> int:
    """What ``sell_minor`` of one asset buys of the other at ``customer_rate``, in minor
    units, rounded down.

    ``customer_rate`` is units of the buy asset for one unit of the sell asset, both major.
    """
    if isinstance(sell_minor, bool) or not isinstance(sell_minor, int):
        raise TypeError(f"an amount is an int of minor units, not {type(sell_minor).__name__}")
    if not isinstance(customer_rate, Decimal):
        raise TypeError(f"a rate is a Decimal, not {type(customer_rate).__name__}")
    if not customer_rate.is_finite() or customer_rate <= 0:
        raise ValueError("a rate is a positive number")

    sell, buy = get_asset(sell_asset), get_asset(buy_asset)
    with localcontext(prec=_PRECISION, rounding=ROUND_DOWN):
        # Dividing by a power of ten only moves the point, so nothing is lost before the
        # one rounding at the end.
        exact = Decimal(sell_minor) / sell.scale * customer_rate * buy.scale
        bought = int(exact.to_integral_value(rounding=ROUND_FLOOR))
    if bought <= 0:
        raise AmountTooSmall
    return bought
