"""Verifying that a delivery was signed by its provider.

The provider sends ``X-Signature: t=<unix seconds>,v1=<hex>``, where ``v1`` is HMAC-SHA256,
keyed with the shared secret, over the bytes ``<t>.<raw request body>``. Everything here
works on the body exactly as it arrived: a body that was parsed and written out again is a
different sequence of bytes and would not verify.
"""

import hashlib
import hmac
import re
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Final

_TIMESTAMP: Final = re.compile(r"[0-9]{1,12}", re.ASCII)
_DIGEST: Final = re.compile(r"[0-9a-f]{64}", re.ASCII)


class Refusal(StrEnum):
    """Why a delivery was not believed. For the log: the sender is told none of this."""

    NOT_CONFIGURED = "not_configured"
    MALFORMED = "malformed"
    MISMATCH = "mismatch"
    OUTSIDE_TOLERANCE = "outside_tolerance"


def parse_header(header: str) -> tuple[str, str] | None:
    """The timestamp and the digest of an ``X-Signature`` header, both as they were written.

    The header must be exactly one ``t`` and one ``v1``. A header with a field repeated or
    a field this code does not know is refused rather than read leniently: which of two
    timestamps counts is not a question a verifier should have to answer.
    """
    fields: dict[str, str] = {}
    for part in header.split(","):
        name, separator, value = part.partition("=")
        if not separator or name in fields:
            return None
        fields[name] = value
    if fields.keys() != {"t", "v1"}:
        return None
    timestamp, digest = fields["t"], fields["v1"]
    if _TIMESTAMP.fullmatch(timestamp) is None or _DIGEST.fullmatch(digest) is None:
        return None
    return timestamp, digest


def check(
    *,
    secrets: Sequence[bytes],
    headers: Sequence[str],
    body: bytes,
    now: datetime,
    tolerance_seconds: int,
) -> Refusal | None:
    """Judge one delivery. Returns None if it is to be believed, and why not otherwise.

    ``headers`` is every ``X-Signature`` header the request carried; anything but exactly
    one is refused. ``secrets`` is every secret active for the provider: two during a
    rotation.
    """
    # Never accept an unsigned delivery: a provider without a secret is a provider that
    # is not configured, not one that is trusted.
    if not secrets:
        return Refusal.NOT_CONFIGURED
    if len(headers) != 1:
        return Refusal.MALFORMED
    parsed = parse_header(headers[0])
    if parsed is None:
        return Refusal.MALFORMED
    timestamp, digest = parsed

    # The timestamp is signed with the body, as the sender wrote it, so that an old
    # delivery cannot be given a new one.
    signed = f"{timestamp}.".encode() + body
    matched = False
    for secret in secrets:
        expected = hmac.new(secret, signed, hashlib.sha256).hexdigest()
        # Every secret is tried, without stopping at the first match, so that how long
        # this takes does not say which secret signed.
        matched |= hmac.compare_digest(expected, digest)
    if not matched:
        return Refusal.MISMATCH

    # After the signature: only a timestamp the provider really signed is worth judging.
    if abs(now.timestamp() - int(timestamp)) > tolerance_seconds:
        return Refusal.OUTSIDE_TOLERANCE
    return None
