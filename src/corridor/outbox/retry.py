"""The retry policy: how long a failed event waits before it is tried again."""

import random
from typing import Final

BASE_SECONDS: Final = 2
CAP_SECONDS: Final = 300


def next_delay(attempt: int, *, rng: random.Random) -> float:
    """Seconds to wait after the ``attempt``-th failure (1 after the first).

    The ceiling doubles from two seconds with each attempt, up to five minutes. The delay
    is drawn uniformly between zero and the ceiling ("full jitter"): events that failed
    together, because a provider was down, would otherwise all come back at the same
    instant and knock it down again.
    """
    if attempt < 1:
        raise ValueError(f"an attempt is counted from 1, not {attempt}")
    ceiling = min(CAP_SECONDS, BASE_SECONDS * 2 ** (attempt - 1))
    return rng.uniform(0, ceiling)
