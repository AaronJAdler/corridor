"""Keyset pagination: pages, page sizes and the opaque cursors that link one page to the next.

A cursor names a position in one particular list. It carries what kind of list that is and
which one (an account's entries, say), so a cursor handed out for one list is refused by
every other instead of being read as a position in it.
"""

import base64
import json
from dataclasses import dataclass
from typing import Final

from corridor.platform.errors import InvalidRequest

DEFAULT_LIMIT: Final = 50
MAX_LIMIT: Final = 200

# Far longer than any cursor this module produces. It bounds the work a hostile string can
# ask for before it is refused.
_MAX_CURSOR_LENGTH: Final = 512

_FIELDS: Final = frozenset({"k", "s", "p"})


class InvalidCursor(InvalidRequest):
    """The cursor is not one this list handed out. It never says which check failed."""

    code = "invalid_cursor"
    title = "Invalid cursor"

    def __init__(self) -> None:
        super().__init__("The cursor is not valid for this list. Start again without one.")


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: tuple[T, ...]
    # What to send back for the page after this one. None when this is the last page.
    next_cursor: str | None


def clamp_limit(limit: int) -> int:
    """The page size to serve for the size that was asked for.

    Asking for more than the maximum gets the maximum, so a client can ask for "as many as
    you give" without knowing the number. Asking for less than one is a mistake.
    """
    if limit < 1:
        raise InvalidRequest("limit must be at least 1.", field="limit")
    return min(limit, MAX_LIMIT)


def encode_cursor(*, kind: str, scope: str, position: int | str) -> str:
    """A cursor for ``position`` in the list of ``kind`` that belongs to ``scope``."""
    payload = json.dumps({"k": kind, "s": scope, "p": position}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_cursor(text: str, *, kind: str, scope: str) -> int | str:
    """The position a cursor names, provided it is a cursor for this list.

    Raises ``InvalidCursor`` for anything that is not a cursor, and for a cursor of another
    kind or another scope. What the position must look like is the caller's to check.
    """
    if not text or len(text) > _MAX_CURSOR_LENGTH:
        raise InvalidCursor
    try:
        # Strict: by default the decoder skips characters outside the alphabet.
        raw = base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=True)
        payload = json.loads(raw)
    except ValueError, RecursionError:
        raise InvalidCursor from None

    if not isinstance(payload, dict) or payload.keys() != _FIELDS:
        raise InvalidCursor
    position = payload["p"]
    # A bool is an int in Python, and is not a position.
    if isinstance(position, bool) or not isinstance(position, int | str):
        raise InvalidCursor
    if payload["k"] != kind or payload["s"] != scope:
        raise InvalidCursor
    return position
