"""The rate source: a seeded walk of three prices, from which every pair is derived.

The walk takes one step for each whole second since it started, so the rates at any moment
depend on the seed and on how long the simulator has been running, and on nothing else: not
on how the clock got there, and not on anything else the simulator did meanwhile.
"""

import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal
from typing import Final

from corridor_sim.clock import format_time
from corridor_sim.errors import ApiError

ASSETS: Final = ("USD", "MXN", "BRL", "USDC")

# Prices are whole numbers of 10**-12 of a unit, and a step is integer arithmetic on them.
# A walk built on floats would depend on the platform's C library for its logarithms, and
# two machines with the same seed could disagree in the last place.
_SCALE: Final = 10**12

# Units of each asset that one USD buys when the walk starts.
_OPENING: Final = {"MXN": 17_250_000_000_000, "BRL": 5_100_000_000_000, "USDC": 1_000_000_000_000}

# How many standard deviations of a step make the whole price: 5000 is 2 basis points a
# step, 20000 is half a basis point.
_STEPS_PER_UNIT: Final = {"MXN": 5_000, "BRL": 5_000, "USDC": 20_000}

# A step's size is the number of set bits among 256 random bits, less the 128 expected: a
# binomial draw with a standard deviation of exactly 8, and as good as normal.
_BITS: Final = 256
_DEVIATION: Final = 8

_STEP: Final = timedelta(seconds=1)
_SIX_PLACES: Final = Decimal("0.000001")
_RATES: Final = Context(prec=60, rounding=ROUND_HALF_EVEN)

# A pinned mid: a plain decimal string with at most six places, as a mid is written.
_MID: Final = re.compile(r"[0-9]{1,7}(?:\.[0-9]{1,6})?")
_LARGEST_MID: Final = Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class Rate:
    """How many units of ``quote`` one unit of ``base`` buys, mid-market."""

    base: str
    quote: str
    mid: Decimal
    as_of: datetime


class RateWalk:
    def __init__(self, seed: int, start: datetime) -> None:
        # A generator of its own, so that nothing else the simulator draws moves the walk.
        self._random = random.Random(f"corridor-sim:{seed}:fx")  # noqa: S311 - a reproducible market, not a secret
        self._start = start
        self._steps = 0
        self._prices = dict(_OPENING)
        self._pinned: dict[tuple[str, str], Decimal] = {}
        self.frozen = False

    @property
    def as_of(self) -> datetime:
        """When the rates were last updated: the time of the walk's latest step."""
        return self._start + self._steps * _STEP

    def advance_to(self, now: datetime) -> None:
        """Take every step that is due by ``now``. A frozen walk takes none."""
        if self.frozen:
            return
        due = (now - self._start) // _STEP
        while self._steps < due:
            for asset, steps_per_unit in _STEPS_PER_UNIT.items():
                deviations = self._random.getrandbits(_BITS).bit_count() - _BITS // 2
                price = self._prices[asset]
                self._prices[asset] = price + price * deviations // (_DEVIATION * steps_per_unit)
            self._steps += 1

    def freeze(self, frozen: bool, now: datetime) -> None:
        """Stop the rates and ``as_of`` where they are, or let them go again.

        The market did not stop while the feed did: unfreezing takes every step that was
        missed, so the rates are once more what the seed and the elapsed time make them.
        """
        self.frozen = False
        self.advance_to(now)
        self.frozen = frozen

    def rate(self, base: str, quote: str) -> Rate:
        _check_pair(base, quote)
        pinned = self._pinned.get((base, quote))
        if pinned is not None:
            return Rate(base, quote, pinned, self.as_of)
        mid = _RATES.divide(Decimal(self._price(quote)), Decimal(self._price(base)))
        return Rate(base, quote, mid.quantize(_SIX_PLACES, context=_RATES), self.as_of)

    def pin(self, base: str, quote: str, mid: object) -> Rate:
        """Fix a pair at ``mid``, and its reverse at the reciprocal, from now on.

        Only the value is fixed. ``as_of`` keeps following the walk, so a pinned rate stays
        fresh; a stale rate is what freezing produces.
        """
        _check_pair(base, quote)
        value = _parse_mid(mid)
        self._pinned[base, quote] = value
        self._pinned[quote, base] = _RATES.divide(Decimal(1), value).quantize(
            _SIX_PLACES, context=_RATES
        )
        return self.rate(base, quote)

    def _price(self, asset: str) -> int:
        return _SCALE if asset == "USD" else self._prices[asset]


def rate_document(rate: Rate) -> dict[str, object]:
    return {
        "base": rate.base,
        "quote": rate.quote,
        "mid": format(rate.mid, "f"),
        "as_of": format_time(rate.as_of),
    }


def _check_pair(base: str, quote: str) -> None:
    if base not in ASSETS or quote not in ASSETS or base == quote:
        raise ApiError(
            404, "unknown_pair", "A pair is two different assets out of USD, MXN, BRL and USDC."
        )


def _parse_mid(value: object) -> Decimal:
    match = _MID.fullmatch(value) if isinstance(value, str) else None
    mid = Decimal(match[0]).quantize(_SIX_PLACES, context=_RATES) if match is not None else None
    # The bounds keep the reciprocal, at six places, a rate too: neither zero nor endless.
    if mid is None or not _SIX_PLACES <= mid <= _LARGEST_MID:
        raise ApiError(
            422,
            "invalid_rate",
            'A mid is a decimal string with at most six places, from "0.000001" to "1000000".',
        )
    return mid
