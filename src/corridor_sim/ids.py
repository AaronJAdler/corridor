"""Generated values: identifiers, account numbers, addresses and hashes.

Everything the simulator invents is drawn from one generator seeded from the settings, so
two runs with the same seed and the same calls invent the same values.
"""

import random
import string
from typing import Final

# The lower-case base32 alphabet: a to z, then 2 to 7.
_BASE32: Final = string.ascii_lowercase + "234567"


class IdFactory:
    def __init__(self, seed: int) -> None:
        self._random = random.Random(f"corridor-sim:{seed}:ids")  # noqa: S311 - reproducible on purpose; nothing drawn here is a secret
        self._issued: set[str] = set()

    def new(self, prefix: str) -> str:
        """An opaque identifier: its type prefix and twelve hex characters, never repeated."""
        while True:
            candidate = f"{prefix}{self._random.getrandbits(48):012x}"
            if candidate not in self._issued:
                self._issued.add(candidate)
                return candidate

    def hex(self, length: int) -> str:
        return f"{self._random.getrandbits(4 * length):0{length}x}"

    def digits(self, length: int) -> str:
        return "".join(str(self._random.randrange(10)) for _ in range(length))

    def base32(self, length: int) -> str:
        return "".join(_BASE32[self._random.getrandbits(5)] for _ in range(length))
