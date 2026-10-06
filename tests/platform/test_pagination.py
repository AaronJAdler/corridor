"""Cursors and page sizes: what a list endpoint hands out and what it accepts back."""

import base64
import dataclasses
import json

import pytest

from corridor.platform.errors import InvalidRequest
from corridor.platform.pagination import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    InvalidCursor,
    Page,
    clamp_limit,
    decode_cursor,
    encode_cursor,
)


def raw(payload: object) -> str:
    """A cursor carrying exactly ``payload``, as a client forging one would build it."""
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


@pytest.mark.parametrize("position", [1, 42, 2**63 - 1, "0199b7c2-6f0e-7b1a-9d53-2c1f4e8a7b10"])
def test_a_cursor_decodes_to_the_position_it_was_made_from(position: int | str) -> None:
    cursor = encode_cursor(kind="entries", scope="account-1", position=position)

    assert decode_cursor(cursor, kind="entries", scope="account-1") == position


def test_a_cursor_is_safe_to_put_in_a_query_string() -> None:
    cursor = encode_cursor(kind="entries", scope="account/1?&=", position=7)

    assert cursor.replace("-", "").replace("_", "").isalnum()


def test_a_cursor_is_the_documented_encoding() -> None:
    cursor = encode_cursor(kind="entries", scope="account-1", position=7)

    assert (
        decode_cursor(
            raw({"k": "entries", "s": "account-1", "p": 7}), kind="entries", scope="account-1"
        )
        == 7
    )
    assert json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))) == {
        "k": "entries",
        "s": "account-1",
        "p": 7,
    }


def test_a_cursor_for_another_scope_is_refused() -> None:
    cursor = encode_cursor(kind="entries", scope="account-1", position=7)

    with pytest.raises(InvalidCursor):
        decode_cursor(cursor, kind="entries", scope="account-2")


def test_a_cursor_of_another_kind_is_refused() -> None:
    cursor = encode_cursor(kind="transfers", scope="account-1", position=7)

    with pytest.raises(InvalidCursor):
        decode_cursor(cursor, kind="entries", scope="account-1")


@pytest.mark.parametrize(
    "text",
    [
        "",
        "!!!",
        "not a cursor",
        "abc",
        "e30",  # {}
        raw("entries"),
        raw(["entries", "account-1", 7]),
        raw({"k": "entries", "s": "account-1"}),
        raw({"k": "entries", "p": 7}),
        raw({"s": "account-1", "p": 7}),
        raw({"k": "entries", "s": "account-1", "p": 7, "x": 1}),
        raw({"k": "entries", "s": "account-1", "p": 7.5}),
        raw({"k": "entries", "s": "account-1", "p": True}),
        raw({"k": "entries", "s": "account-1", "p": None}),
        raw({"k": "entries", "s": "account-1", "p": [7]}),
        raw({"k": 1, "s": "account-1", "p": 7}),
        raw({"k": "entries", "s": ["account-1"], "p": 7}),
        base64.urlsafe_b64encode(b"\xff\xfe\xfd").decode(),
        base64.urlsafe_b64encode(b'{"k": "entries", "s": "account-1", "p": 7').decode(),
        # Characters outside the alphabet are an error, not something to skip over.
        raw({"k": "entries", "s": "account-1", "p": 7}) + "*",
        " " + raw({"k": "entries", "s": "account-1", "p": 7}),
        "[" * 400,
        raw({"k": "entries", "s": "account-1", "p": "x" * 2000}),
    ],
)
def test_a_malformed_cursor_is_refused(text: str) -> None:
    with pytest.raises(InvalidCursor):
        decode_cursor(text, kind="entries", scope="account-1")


def test_a_refused_cursor_is_a_422_with_its_own_code_and_does_not_echo_the_input() -> None:
    with pytest.raises(InvalidCursor) as refusal:
        decode_cursor("not a cursor", kind="entries", scope="account-1")

    assert (refusal.value.status, refusal.value.code) == (422, "invalid_cursor")
    assert isinstance(refusal.value, InvalidRequest)
    assert "not a cursor" not in str(refusal.value.detail)


def test_the_default_and_the_largest_page_are_what_the_api_documents() -> None:
    assert (DEFAULT_LIMIT, MAX_LIMIT) == (50, 200)


@pytest.mark.parametrize(
    ("asked", "given"), [(1, 1), (50, 50), (200, 200), (201, 200), (10**9, 200)]
)
def test_a_limit_is_capped_at_the_largest_page(asked: int, given: int) -> None:
    assert clamp_limit(asked) == given


@pytest.mark.parametrize("limit", [0, -1, -200])
def test_a_limit_below_one_is_refused(limit: int) -> None:
    with pytest.raises(InvalidRequest) as refusal:
        clamp_limit(limit)

    assert refusal.value.status == 422


def test_a_page_cannot_be_changed_after_it_is_built() -> None:
    page = Page(items=(1, 2), next_cursor=None)

    with pytest.raises(dataclasses.FrozenInstanceError):
        page.next_cursor = "x"  # type: ignore[misc]
