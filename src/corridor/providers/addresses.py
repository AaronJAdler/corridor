"""The simchain address rule, so that an address is checked before anything is sent to it.

Written from the provider contract's text: 44 characters, the prefix ``sim1``, a body of
32 characters of lower-case base32, then 8 lower-case hex characters that are the first
four bytes of the SHA-256 of the body's ASCII bytes.
"""

import hashlib
from typing import Final

_PREFIX: Final = "sim1"
_BODY_LENGTH: Final = 32
_CHECKSUM_BYTES: Final = 4
_BASE32: Final = frozenset("abcdefghijklmnopqrstuvwxyz234567")


def is_valid_address(text: str) -> bool:
    """Whether ``text`` is a well-formed simchain address. Says nothing about who owns it."""
    if not isinstance(text, str) or not text.startswith(_PREFIX):
        return False
    body = text[len(_PREFIX) : len(_PREFIX) + _BODY_LENGTH]
    if not _BASE32.issuperset(body):
        return False
    # Comparing the whole tail settles the length too: a text that is too short or too
    # long has a tail that is not these eight characters. The digest is rendered in lower
    # case, so an upper-case checksum does not match it either.
    expected = hashlib.sha256(body.encode("ascii")).digest()[:_CHECKSUM_BYTES].hex()
    return text[len(_PREFIX) + _BODY_LENGTH :] == expected
