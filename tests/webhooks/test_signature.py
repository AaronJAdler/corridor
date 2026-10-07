"""Signature verification on its own: no database, no HTTP."""

from datetime import UTC, datetime, timedelta

import pytest

from corridor.webhooks.signature import Refusal, check, parse_header
from tests.webhooks.helpers import BANK_NEXT_SECRET, BANK_SECRET, CUSTODY_SECRET, sign, unix

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
BODY = b'{"id":"evt_1","type":"payout.completed"}'
DIGEST = "ab" * 32


def verdict(
    header: str | None,
    *,
    secrets: tuple[str, ...] = (BANK_SECRET,),
    body: bytes = BODY,
    now: datetime = NOW,
) -> Refusal | None:
    return check(
        secrets=[secret.encode() for secret in secrets],
        headers=[] if header is None else [header],
        body=body,
        now=now,
        tolerance_seconds=300,
    )


def test_a_header_is_its_timestamp_and_its_digest() -> None:
    assert parse_header(f"t=1768478400,v1={DIGEST}") == ("1768478400", DIGEST)


@pytest.mark.parametrize(
    "header",
    [
        "",
        "t=1768478400",
        f"v1={DIGEST}",
        f"t=1768478400,t=1768478400,v1={DIGEST}",
        f"t=1768478400,v1={DIGEST},v1={DIGEST}",
        f"t=1768478400,v1={DIGEST},v0={DIGEST}",
        f"t=1768478400, v1={DIGEST}",
        f"t=1768478400,v1={DIGEST},",
        f"t=,v1={DIGEST}",
        f"t=-1,v1={DIGEST}",
        f"t=1768478400.5,v1={DIGEST}",
        f"t=1234567890123,v1={DIGEST}",
        f"t=١٢٣,v1={DIGEST}",
        f"t=1768478400,v1={DIGEST.upper()}",
        f"t=1768478400,v1={DIGEST[:-1]}",
        f"t=1768478400,v1={DIGEST}0",
        f"t=1768478400,v1={DIGEST}\n",
        f"t=1768478400;v1={DIGEST}",
    ],
)
def test_a_header_that_is_not_exactly_one_timestamp_and_one_digest_is_refused(header: str) -> None:
    assert parse_header(header) is None


def test_a_delivery_signed_with_the_secret_is_accepted() -> None:
    assert verdict(sign(BANK_SECRET, BODY, unix(NOW))) is None


def test_a_delivery_signed_with_any_configured_secret_is_accepted() -> None:
    header = sign(BANK_NEXT_SECRET, BODY, unix(NOW))

    assert verdict(header, secrets=(BANK_SECRET, BANK_NEXT_SECRET)) is None
    assert verdict(header, secrets=(BANK_NEXT_SECRET, BANK_SECRET)) is None


def test_a_delivery_signed_with_another_secret_is_refused() -> None:
    assert verdict(sign(CUSTODY_SECRET, BODY, unix(NOW))) is Refusal.MISMATCH


def test_a_body_changed_after_signing_is_refused() -> None:
    header = sign(BANK_SECRET, BODY, unix(NOW))

    assert verdict(header, body=BODY + b" ") is Refusal.MISMATCH


def test_the_timestamp_is_part_of_what_is_signed() -> None:
    signed = sign(BANK_SECRET, BODY, unix(NOW))
    digest = signed.partition(",v1=")[2]

    # Still inside the tolerance, so only the signature can refuse it.
    assert verdict(f"t={unix(NOW) + 1},v1={digest}") is Refusal.MISMATCH


def test_no_configured_secret_refuses_everything() -> None:
    assert verdict(sign("", BODY, unix(NOW)), secrets=()) is Refusal.NOT_CONFIGURED


def test_a_missing_header_is_refused() -> None:
    assert verdict(None) is Refusal.MALFORMED


def test_two_signature_headers_are_refused() -> None:
    header = sign(BANK_SECRET, BODY, unix(NOW))

    refusal = check(
        secrets=[BANK_SECRET.encode()],
        headers=[header, header],
        body=BODY,
        now=NOW,
        tolerance_seconds=300,
    )

    assert refusal is Refusal.MALFORMED


@pytest.mark.parametrize("offset", [-300, 300])
def test_a_timestamp_at_the_edge_of_the_tolerance_is_accepted(offset: int) -> None:
    header = sign(BANK_SECRET, BODY, unix(NOW) + offset)

    assert verdict(header) is None


@pytest.mark.parametrize("offset", [-301, 301])
def test_a_correctly_signed_timestamp_outside_the_tolerance_is_refused(offset: int) -> None:
    header = sign(BANK_SECRET, BODY, unix(NOW) + offset)

    assert verdict(header) is Refusal.OUTSIDE_TOLERANCE


def test_the_signature_is_judged_before_the_timestamp() -> None:
    stale = unix(NOW - timedelta(hours=1))

    assert verdict(sign(CUSTODY_SECRET, BODY, stale)) is Refusal.MISMATCH
