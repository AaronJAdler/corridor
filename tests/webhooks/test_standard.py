"""Verifying an Increase delivery by the Standard Webhooks scheme.

The signer in ``tests.webhooks.helpers`` is written from the text of Increase's webhooks
guide, not from the verifier, so the two agreeing says the verifier reads the guide as it is
written.
"""

import base64
import hashlib
import hmac
from collections.abc import Mapping, Sequence
from datetime import timedelta

import pytest

from corridor.platform.clock import utcnow
from corridor.webhooks import standard
from corridor.webhooks.signature import Refusal
from tests.webhooks.helpers import (
    INCREASE_NEXT_SECRET,
    INCREASE_SECRET,
    UNKNOWN_SECRET,
    increase_event,
    standard_headers,
    standard_signature,
    unix,
)

TOLERANCE = 300
BODY = increase_event()


def judge(
    headers: Mapping[str, str | Sequence[str]],
    body: bytes = BODY,
    secrets: tuple[str, ...] = (INCREASE_SECRET,),
) -> Refusal | None:
    listed = {
        name: [value] if isinstance(value, str) else list(value) for name, value in headers.items()
    }
    return standard.check(
        secrets=[secret.encode() for secret in secrets],
        headers=listed,
        body=body,
        now=utcnow(),
        tolerance_seconds=TOLERANCE,
    )


def test_a_delivery_signed_as_the_guide_describes_is_believed() -> None:
    assert judge(standard_headers(BODY)) is None


def test_during_a_rotation_a_header_with_one_signature_per_secret_is_believed() -> None:
    headers = standard_headers(BODY, secrets=(INCREASE_NEXT_SECRET, INCREASE_SECRET))

    assert judge(headers) is None
    assert judge(headers, secrets=(INCREASE_SECRET,)) is None
    assert judge(standard_headers(BODY), secrets=(INCREASE_NEXT_SECRET, INCREASE_SECRET)) is None


def test_no_secret_configured_refuses_everything() -> None:
    assert judge(standard_headers(BODY), secrets=()) is Refusal.NOT_CONFIGURED


def test_another_secret_does_not_verify() -> None:
    assert judge(standard_headers(BODY, secrets=(UNKNOWN_SECRET,))) is Refusal.MISMATCH


def test_a_body_changed_by_one_byte_does_not_verify() -> None:
    headers = standard_headers(BODY)

    assert judge(headers, body=BODY.replace(b"created", b"updated")) is Refusal.MISMATCH
    assert judge(headers, body=BODY + b" ") is Refusal.MISMATCH


def test_the_id_and_the_timestamp_are_signed_with_the_body() -> None:
    headers = standard_headers(BODY, webhook_id="event_1")

    assert judge({**headers, "webhook-id": "event_2"}) is Refusal.MISMATCH
    later = str(int(headers["webhook-timestamp"]) + 1)
    assert judge({**headers, "webhook-timestamp": later}) is Refusal.MISMATCH


def test_the_simulators_scheme_over_the_same_bytes_does_not_verify() -> None:
    """``t.body`` with the timestamp in its own header is the simulator's scheme. Increase's
    puts the id first, so a digest of the one is never the other's."""
    headers = standard_headers(BODY)
    t = headers["webhook-timestamp"]
    digest = hmac.new(INCREASE_SECRET.encode(), f"{t}.".encode() + BODY, hashlib.sha256).digest()

    signed = {**headers, "webhook-signature": "v1," + base64.b64encode(digest).decode()}
    assert judge(signed) is Refusal.MISMATCH


@pytest.mark.parametrize("missing", ["webhook-id", "webhook-timestamp", "webhook-signature"])
def test_a_delivery_without_one_of_the_three_headers_is_malformed(missing: str) -> None:
    headers = {k: v for k, v in standard_headers(BODY).items() if k != missing}

    assert judge(headers) is Refusal.MALFORMED


@pytest.mark.parametrize("repeated", ["webhook-id", "webhook-timestamp", "webhook-signature"])
def test_a_header_sent_twice_is_malformed_whichever_value_is_right(repeated: str) -> None:
    headers: dict[str, str | list[str]] = dict(standard_headers(BODY))
    value = headers[repeated]
    assert isinstance(value, str)
    headers[repeated] = [value, value]

    assert judge(headers) is Refusal.MALFORMED


@pytest.mark.parametrize(
    "signature",
    [
        "",
        "v1",
        "v1,",
        ",abc",
        "v1,not base64!",
        "v1," + base64.b64encode(b"short").decode(),
        "v1:" + base64.b64encode(b"\0" * 32).decode(),
        " ".join(["v2,AAAA"] * (standard.MAX_SIGNATURES + 1)),
        # Only versions this code does not know: nothing to compare with.
        "v1a,AAAA",
    ],
)
def test_a_signature_header_that_is_not_one_is_malformed(signature: str) -> None:
    assert judge({**standard_headers(BODY), "webhook-signature": signature}) is Refusal.MALFORMED


def test_a_signature_of_an_unknown_version_is_passed_over() -> None:
    headers = standard_headers(BODY)
    beside = "v1a," + base64.b64encode(b"\0" * 32).decode() + " " + headers["webhook-signature"]

    assert judge({**headers, "webhook-signature": beside}) is None


@pytest.mark.parametrize("identifier", ["", "event.1", "event 1", "e" * 256, "évent"])
def test_an_id_that_is_not_a_plain_identifier_is_malformed(identifier: str) -> None:
    headers = standard_headers(BODY)
    t = headers["webhook-timestamp"]
    signed = {
        "webhook-id": identifier,
        "webhook-timestamp": t,
        "webhook-signature": standard_signature(INCREASE_SECRET, identifier, t, BODY),
    }

    assert judge(signed) is Refusal.MALFORMED


@pytest.mark.parametrize("timestamp", ["", "-1", "1.5", "1e9", "1" * 13, " 1"])
def test_a_timestamp_that_is_not_unix_seconds_is_malformed(timestamp: str) -> None:
    signed = {
        "webhook-id": "event_1",
        "webhook-timestamp": timestamp,
        "webhook-signature": standard_signature(INCREASE_SECRET, "event_1", timestamp, BODY),
    }

    assert judge(signed) is Refusal.MALFORMED


@pytest.mark.parametrize("offset", [TOLERANCE + 1, -(TOLERANCE + 1)])
def test_a_genuine_delivery_from_too_long_ago_or_too_far_ahead_is_refused(offset: int) -> None:
    moment = unix(utcnow() - timedelta(seconds=offset))

    assert judge(standard_headers(BODY, timestamp=moment)) is Refusal.OUTSIDE_TOLERANCE


def test_a_delivery_at_the_edge_of_the_tolerance_is_believed() -> None:
    moment = unix(utcnow() - timedelta(seconds=TOLERANCE - 1))

    assert judge(standard_headers(BODY, timestamp=moment)) is None


def test_an_old_timestamp_on_a_forged_signature_is_a_mismatch_and_not_a_late_delivery() -> None:
    """The timestamp is judged only once the signature has matched, so a forger learns
    nothing about the tolerance."""
    moment = unix(utcnow() - timedelta(days=1))

    headers = standard_headers(BODY, timestamp=moment, secrets=(UNKNOWN_SECRET,))
    assert judge(headers) is Refusal.MISMATCH
