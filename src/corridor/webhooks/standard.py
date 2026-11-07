"""Verifying a delivery signed by the Standard Webhooks scheme, as Increase signs its own.

Three headers: ``webhook-id``, the event's id; ``webhook-timestamp``, Unix seconds; and
``webhook-signature``, a space-separated list of ``<version>,<base64>``. A ``v1`` signature is
the HMAC-SHA256, keyed with the endpoint's shared secret, of
``<webhook-id>.<webhook-timestamp>.<raw request body>``. During a rotation the header carries
one signature for each secret. See ``docs/adr/0020-increase-bank-rail-adapter.md``.

As in ``signature``, everything works on the body exactly as it arrived, every comparison is
in constant time, and the timestamp is judged only once the signature has matched.
"""

import base64
import hashlib
import hmac
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Final

from corridor.webhooks.signature import Refusal

ID_HEADER: Final = "webhook-id"
TIMESTAMP_HEADER: Final = "webhook-timestamp"
SIGNATURE_HEADER: Final = "webhook-signature"
HEADERS: Final = (ID_HEADER, TIMESTAMP_HEADER, SIGNATURE_HEADER)

# What an event id may be. It is signed, so these bounds protect the code that reads it, not
# the signature: an id with a period in it would make the signed bytes ambiguous to read.
_ID: Final = re.compile(r"[A-Za-z0-9_-]{1,255}", re.ASCII)
_TIMESTAMP: Final = re.compile(r"[0-9]{1,12}", re.ASCII)
_VERSION: Final = "v1"
_DIGEST_BYTES: Final = hashlib.sha256().digest_size
# A rotation has two secrets. A header with far more signatures than that is not one to try.
MAX_SIGNATURES: Final = 8


def _one(headers: Mapping[str, Sequence[str]], name: str) -> str | None:
    values = headers.get(name, ())
    return values[0] if len(values) == 1 else None


def parse_signatures(header: str) -> list[bytes] | None:
    """The ``v1`` digests of a ``webhook-signature`` header, or None if it is not one.

    A signature of a version this code does not know is passed over, as the scheme asks, so a
    sender may add one. A ``v1`` that is not a whole SHA-256 digest in base64 refuses the
    header: it is not a signature to compare with.
    """
    entries = header.split(" ")
    if not header or len(entries) > MAX_SIGNATURES:
        return None
    digests: list[bytes] = []
    for entry in entries:
        version, separator, encoded = entry.partition(",")
        if not separator or not version:
            return None
        if version != _VERSION:
            continue
        try:
            digest = base64.b64decode(encoded, validate=True)
        except ValueError:
            return None
        if len(digest) != _DIGEST_BYTES:
            return None
        digests.append(digest)
    return digests or None


def check(
    *,
    secrets: Sequence[bytes],
    headers: Mapping[str, Sequence[str]],
    body: bytes,
    now: datetime,
    tolerance_seconds: int,
) -> Refusal | None:
    """Judge one delivery. Returns None if it is to be believed, and why not otherwise.

    ``headers`` holds every value each of the three headers had; anything but exactly one of
    each is refused. ``secrets`` is every secret active for the provider.
    """
    if not secrets:
        return Refusal.NOT_CONFIGURED
    webhook_id, timestamp, signature = (_one(headers, name) for name in HEADERS)
    if webhook_id is None or timestamp is None or signature is None:
        return Refusal.MALFORMED
    if _ID.fullmatch(webhook_id) is None or _TIMESTAMP.fullmatch(timestamp) is None:
        return Refusal.MALFORMED
    candidates = parse_signatures(signature)
    if candidates is None:
        return Refusal.MALFORMED

    signed = f"{webhook_id}.{timestamp}.".encode() + body
    matched = False
    for secret in secrets:
        expected = hmac.new(secret, signed, hashlib.sha256).digest()
        for candidate in candidates:
            # Every pair is compared, without stopping at a match, so that how long this
            # takes says nothing about which secret or which signature matched.
            matched |= hmac.compare_digest(expected, candidate)
    if not matched:
        return Refusal.MISMATCH

    if abs(now.timestamp() - int(timestamp)) > tolerance_seconds:
        return Refusal.OUTSIDE_TOLERANCE
    return None
