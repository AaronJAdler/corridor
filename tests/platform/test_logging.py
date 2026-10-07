import dataclasses
import json
import logging
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import BaseModel

from corridor.platform.logging import REDACTED, configure_logging, get_logger, redact, scrub

# A value for sensitive-looking keys in these tests. It protects nothing.
NOT_A_SECRET = "hunter2-value"  # pragma: allowlist secret


def _redacted(**event: object) -> dict[str, object]:
    return dict(redact(None, "info", dict(event)))


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "new_password",
        "refresh_token",
        "access_token",
        "Authorization",
        "api_key",
        "apiKey",
        "private_key",
        "webhook_secret",
        "account_number",
        "routing_number",
        "clabe",
        "database_url",
    ],
)
def test_a_sensitive_key_never_keeps_its_value(key: str) -> None:
    assert _redacted(**{key: NOT_A_SECRET}) == {key: REDACTED}


def test_sensitive_keys_are_found_inside_nested_structures() -> None:
    event = _redacted(
        request={"headers": {"authorization": "x", "accept": "json"}, "items": [{"token": "y"}]}
    )
    assert event == {
        "request": {
            "headers": {"authorization": REDACTED, "accept": "json"},
            "items": [{"token": REDACTED}],
        }
    }


@pytest.mark.parametrize(
    "value",
    [
        "eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.c2lnbmF0dXJlLWJ5dGVz",  # pragma: allowlist secret
        "ck_ab12cd34_0123456789abcdefghijklmnop",  # pragma: allowlist secret
        "Bearer abcdefghijklmnop",
        "postgresql://corridor_app:not-a-real-password@db.internal:5432/corridor",  # pragma: allowlist secret
        "-----BEGIN EC PRIVATE KEY-----\nMHcCAQEE\n-----END EC PRIVATE KEY-----",  # pragma: allowlist secret
    ],
)
def test_a_secret_shaped_value_is_removed_whatever_its_key(value: str) -> None:
    event = _redacted(event=f"request failed: {value}", note=value)
    assert value not in json.dumps(event)
    assert REDACTED in str(event["event"])


def test_ordinary_values_pass_through_untouched() -> None:
    event = {
        "event": "transfer.created",
        "amount": "12.50",
        "asset": "USD",
        "attempt": 3,
        "ok": True,
        "status": "completed",
        "reason": "unknown_email",
        "error": "RuntimeError: the provider is down",
        "error_type": "RuntimeError",
        "topic": "transfer.completed",
        "job": "outbox.purge_finished",
        "deleted": 4,
        "transfer_id": "01a113ba-7f79-741b-ae61-74c5dbe22853",
        "account_id": "01a113ba-7f79-741b-ae61-74c5dbe22854",
        "account_ids": ["01a113ba-7f79-741b-ae61-74c5dbe22854"],
        "entry_id": "01a113ba-7f79-741b-ae61-74c5dbe22855",
        "user_id": None,
        "session_id": "01a113ba-7f79-741b-ae61-74c5dbe22856",
        "request_id": "01a113ba-7f79-741b-ae61-74c5dbe22857",
        "passed": 12,
        "bypass": "no",
        "signal": "SIGTERM",
        "design": "v2",
        "hashtag": "#corridor",
        "accounting_period": "2026-01",
        "url": "https://provider.example/v1/payouts/po_123?page=2&limit=50",
        "detail": "Available balance is 0.00 USD; 1.00 USD is required.",
        "note": "a basic understanding of the status: ok, amount=12.50",
    }
    assert _redacted(**event) == event


@pytest.mark.parametrize(
    "key",
    [
        "pwd",
        "db_pwd",
        "pass",
        "user_pass",
        "jwt",
        "jwt_assertion",
        "pem",
        "key_pem",
        "hash",
        "hash_value",
        "body_digest",
        "digest",
        "sig",
        "webhook_sig",
        "x-sig",
        "account",
        "account_number",
        "bank_account",
        "accountHolder",
        "acct_no",
        "acctNo",
        "routing",
        "routing_no",
        "pix",
        "PIX",
    ],
)
def test_a_key_with_a_sensitive_word_never_keeps_its_value(key: str) -> None:
    assert _redacted(**{key: NOT_A_SECRET}) == {key: REDACTED}
    assert scrub({"outer": {key: NOT_A_SECRET}}) == {"outer": {key: REDACTED}}


@pytest.mark.parametrize(
    "key", ["account_id", "account_ids", "entry_id", "passed", "passenger", "signal", "pixel"]
)
def test_a_key_that_only_resembles_a_sensitive_word_keeps_its_value(key: str) -> None:
    assert _redacted(**{key: "01a113ba"}) == {key: "01a113ba"}


def test_a_yes_or_no_under_a_sensitive_key_is_kept() -> None:
    # Whether a signature was valid is the point of the line, and a boolean holds no secret.
    event = _redacted(signature_valid=False, password_changed=True, signature=NOT_A_SECRET)

    assert event == {"signature_valid": False, "password_changed": True, "signature": REDACTED}


ARGON2_HASH = "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$RdescudvJCsgt3ub+b+dWRWJTmaaJObG"  # pragma: allowlist secret
HEX_64 = (
    "5257a869e7ecebeda32affa62cdca3fa51cad7e77a0e56ff536d0ce8e108d8bd"  # pragma: allowlist secret
)


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        (
            "redis://:not-a-real-password@cache.internal:6379/0",
            "not-a-real-password",
        ),  # pragma: allowlist secret
        (
            "postgresql://app:not/a/real/password@db.internal/corridor",
            "not/a/real/password",
        ),  # pragma: allowlist secret
        (
            "postgresql://app:not/a/real/password@db.internal/corridor",
            "password@",
        ),  # pragma: allowlist secret
        (f"GET https://provider.example/v1/rates?api_key={NOT_A_SECRET}&base=USD", NOT_A_SECRET),
        (f"GET https://provider.example/v1/rates?base=USD&token={NOT_A_SECRET}", NOT_A_SECRET),
        (f"stored {ARGON2_HASH} for the user", "$argon2id$"),
        (f"stored {ARGON2_HASH} for the user", "RdescudvJCsgt3ub"),
        (f"LoginRequest(email='maria@example.com', password='{NOT_A_SECRET}')", NOT_A_SECRET),
        (f"password={NOT_A_SECRET}", NOT_A_SECRET),
        (f'{{"email": "maria@example.com", "password": "{NOT_A_SECRET}"}}', NOT_A_SECRET),
        (f'body was "{{\\"password\\": \\"{NOT_A_SECRET}\\"}}"', NOT_A_SECRET),
        (f"Tokens(access='a', refresh_token='{NOT_A_SECRET}')", NOT_A_SECRET),
        (f"pwd: {NOT_A_SECRET}", NOT_A_SECRET),
        (f'cut short at "password": "{NOT_A_SECRET}', NOT_A_SECRET),
        (f"acct_no={NOT_A_SECRET}; sig={HEX_64}", NOT_A_SECRET),
        ("Authorization: BEARER abcdefghijklmnop", "abcdefghijklmnop"),
        ("header was bEaReR abcdefghijklmnop", "abcdefghijklmnop"),
        (
            "Authorization: Basic bWFyaWE6aHVudGVyMg==",
            "bWFyaWE6aHVudGVyMg==",
        ),  # pragma: allowlist secret
        (
            "header was BASIC bWFyaWE6aHVudGVyMg==",
            "bWFyaWE6aHVudGVyMg==",
        ),  # pragma: allowlist secret
        (f"X-Signature was t=1700000000,v1={HEX_64}", HEX_64),
        (f"expected v1={HEX_64}", HEX_64),
        (
            "key ck_abc_0123456789abcdefghijklmnop was refused",
            "0123456789abcdefghijklmnop",
        ),  # pragma: allowlist secret
        (
            "key ck_live-eu_0123456789abcdefghijklmnop was refused",
            "0123456789abcdefghijklmnop",
        ),  # pragma: allowlist secret
    ],
)
def test_a_secret_inside_a_string_is_removed(text: str, secret: str) -> None:
    for scrubbed in (scrub(text), _redacted(event=text)["event"], _redacted(note=text)["note"]):
        assert secret not in scrubbed
        assert REDACTED in scrubbed


def test_what_surrounds_a_secret_inside_a_string_is_kept() -> None:
    assert scrub(f"LoginRequest(email='maria@example.com', password='{NOT_A_SECRET}')") == (
        f"LoginRequest(email='maria@example.com', password={REDACTED})"
    )
    assert scrub(f"GET /v1/rates?api_key={NOT_A_SECRET}&base=USD") == (
        f"GET /v1/rates?api_key={REDACTED}&base=USD"
    )
    assert scrub("Authorization: Bearer abcdefghijklmnop") == f"Authorization: {REDACTED}"


def test_an_argon2_hash_is_removed_under_any_key() -> None:
    event = _redacted(stored=ARGON2_HASH, user={"credential_check": [ARGON2_HASH]})

    assert "argon2" not in json.dumps(event)


@pytest.mark.parametrize("pairs", [list, tuple], ids=["list", "tuple"])
def test_a_sequence_of_name_and_value_pairs_is_redacted_by_name(pairs: type) -> None:
    headers = pairs(
        [pairs(["accept", "json"]), pairs(["password", NOT_A_SECRET]), ("x-sig", NOT_A_SECRET)]
    )

    assert scrub(headers) == [["accept", "json"], ["password", REDACTED], ["x-sig", REDACTED]]
    assert _redacted(headers=headers) == {"headers": scrub(headers)}


@dataclasses.dataclass(frozen=True)
class Credentials:
    email: str
    password: str
    attempts: int = 0


class Envelope(BaseModel):
    recipient: str
    account_number: str
    inner: Credentials


def test_a_dataclass_is_read_as_a_mapping_and_redacted_by_field() -> None:
    credentials = Credentials("maria@example.com", NOT_A_SECRET, 2)

    assert scrub(credentials) == {
        "email": "maria@example.com",
        "password": REDACTED,
        "attempts": 2,
    }
    assert _redacted(who=credentials) == {"who": scrub(credentials)}


def test_a_pydantic_model_is_read_as_a_mapping_and_redacted_by_field() -> None:
    envelope = Envelope(
        recipient="joao",
        account_number="000123456789",
        inner=Credentials("maria@example.com", NOT_A_SECRET),
    )

    assert scrub([envelope]) == [
        {
            "recipient": "joao",
            "account_number": REDACTED,
            "inner": {"email": "maria@example.com", "password": REDACTED, "attempts": 0},
        }
    ]


def test_an_exception_passed_as_a_value_is_logged_as_its_scrubbed_text() -> None:
    event = _redacted(error=RuntimeError(f"refused: password={NOT_A_SECRET}"))

    assert event == {"error": f"RuntimeError('refused: password={REDACTED}')"}


def test_the_configured_pipeline_redacts_structlog_and_stdlib_records(
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        configure_logging("INFO", "json")
        get_logger("test").info("login.failed", password=NOT_A_SECRET, user="maria")
        logging.getLogger("third.party").warning("connecting with Bearer abcdefghijklmnop")
        get_logger("test").debug("below.the.level")
    finally:
        root.handlers, root.level = saved_handlers, saved_level

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["event"] for line in lines] == ["login.failed", f"connecting with {REDACTED}"]
    assert lines[0]["password"] == REDACTED
    assert lines[0]["user"] == "maria"
    assert lines[0]["level"] == "info"
    assert lines[0]["timestamp"].endswith("Z")


def logged(capsys: pytest.CaptureFixture[str], emit: Callable[[], None]) -> list[dict[str, Any]]:
    """What ``emit`` writes through the configured pipeline, as parsed lines."""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        configure_logging("INFO", "json")
        capsys.readouterr()
        emit()
    finally:
        root.handlers, root.level = saved_handlers, saved_level
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


@pytest.mark.parametrize(
    ("message", "arguments"),
    [
        ("the password is %s", (NOT_A_SECRET,)),
        ("connecting to %s with secret %s", ("db.internal", NOT_A_SECRET)),
        ("token %(value)s was refused", ({"value": NOT_A_SECRET},)),
        ("sig %s did not match after %d tries", (NOT_A_SECRET, 3)),
    ],
    ids=["one", "several", "named", "mixed-types"],
)
def test_a_stdlib_record_whose_format_names_a_sensitive_word_has_its_arguments_removed(
    capsys: pytest.CaptureFixture[str], message: str, arguments: tuple[Any, ...]
) -> None:
    [line] = logged(capsys, lambda: logging.getLogger("third.party").warning(message, *arguments))

    assert NOT_A_SECRET not in json.dumps(line)
    assert REDACTED in line["event"]
    assert "positional_args" not in line


def test_a_stdlib_record_with_an_ordinary_format_keeps_its_arguments(
    capsys: pytest.CaptureFixture[str],
) -> None:
    [line] = logged(
        capsys,
        lambda: logging.getLogger("third.party").warning("retrying %s in %d seconds", "po_123", 4),
    )

    assert line["event"] == "retrying po_123 in 4 seconds"
    assert "positional_args" not in line


def test_the_text_of_a_logged_exception_is_scrubbed(capsys: pytest.CaptureFixture[str]) -> None:
    def fail_and_log() -> None:
        try:
            raise RuntimeError(f"provider said password={NOT_A_SECRET} for {ARGON2_HASH}")
        except RuntimeError:
            get_logger("test").exception("provider.failed")

    [line] = logged(capsys, fail_and_log)

    assert line["event"] == "provider.failed"
    assert "RuntimeError" in line["exception"]
    assert f"password={REDACTED}" in line["exception"]
    assert NOT_A_SECRET not in json.dumps(line)
    assert "argon2" not in json.dumps(line)
