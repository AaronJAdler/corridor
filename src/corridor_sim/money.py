"""Amounts: the contract's decimal strings on the outside, exact decimals on the inside.

This is the simulator's own conversion, written from the contract and sharing nothing with
Corridor's, so that a mistake on one side shows up as a disagreement between the two.
There is no floating point here, and no arithmetic that can round.
"""

import re
from collections.abc import Iterable, Mapping
from decimal import ROUND_HALF_EVEN, Context, Decimal, Inexact, InvalidOperation
from types import MappingProxyType
from typing import Final

from corridor_sim.errors import ApiError

# Decimal places per asset, from the contract.
SCALES: Final[Mapping[str, int]] = MappingProxyType({"USD": 2, "MXN": 2, "BRL": 2, "USDC": 6})

ZERO: Final = Decimal(0)

# ASCII digits, optionally a point and more ASCII digits. No sign, exponent, separator or
# whitespace. The bound on the whole part only keeps a hostile string from becoming an
# enormous number.
_AMOUNT: Final = re.compile(r"[0-9]{1,40}(?:\.(?P<fraction>[0-9]+))?")

# Every operation on an amount goes through this context. The default context keeps 28
# significant digits and rounds past that without a word; this one has room for any sum of
# amounts the pattern above admits, and raises rather than lose a digit.
_EXACT: Final = Context(prec=200, rounding=ROUND_HALF_EVEN, traps=[Inexact, InvalidOperation])


def parse_amount(value: object, asset: str) -> Decimal:
    """Read an amount of ``asset`` from a request, or refuse it with ``invalid_amount``.

    The value must be a string: a JSON number has already been through a float. It must be
    positive and have no more decimal places than the asset has.
    """
    scale = SCALES[asset]
    match = _AMOUNT.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise _invalid('An amount is a decimal string such as "12.50", never a number.')
    if len(match["fraction"] or "") > scale:
        raise _invalid(f"{asset} has {scale} decimal places.")

    amount = _at_scale(Decimal(match[0]), scale)
    if amount <= ZERO:
        raise _invalid("An amount is greater than zero.")
    return amount


def format_amount(amount: Decimal, asset: str) -> str:
    """Write an amount with exactly the asset's decimal places. A balance may be negative."""
    if not isinstance(amount, Decimal):
        raise TypeError(f"an amount is a Decimal, not {type(amount).__name__}")
    value = _at_scale(amount, SCALES[asset])
    # Zero has no sign on a statement.
    return format(value.copy_abs() if value.is_zero() else value, "f")


def add(left: Decimal, right: Decimal) -> Decimal:
    return _EXACT.add(left, right)


def subtract(left: Decimal, right: Decimal) -> Decimal:
    return _EXACT.subtract(left, right)


def total(amounts: Iterable[Decimal]) -> Decimal:
    result = ZERO
    for amount in amounts:
        result = _EXACT.add(result, amount)
    return result


def _at_scale(amount: Decimal, scale: int) -> Decimal:
    # Raises Inexact if the amount has more places than the scale: nothing is rounded away.
    return amount.quantize(Decimal(1).scaleb(-scale), context=_EXACT)


def _invalid(message: str) -> ApiError:
    return ApiError(422, "invalid_amount", message)
