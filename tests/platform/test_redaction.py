"""Nothing secret reaches the log: not from the API doing what it is meant to, and not from
a line of code that logs what it should not have.

Everything here is read from what the process wrote, the way an operator or a log store
would see it. The assertions are about the whole output and not about one field of one
line, because a secret that turns up anywhere in it has leaked.
"""

import dataclasses
import logging
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request

from corridor.identity import Scope
from corridor.platform.config import Settings
from corridor.platform.logging import REDACTED, configure_logging, get_logger
from corridor.providers import SimBank, SimCustody
from tests.agents.support import create_agent, issue_key, key_headers
from tests.support.auth import PASSWORD, RegisteredUser, register_user

# The simulated providers, for the route that is handed an account number.
from tests.support.providers import (  # noqa: F401
    ACCOUNT_NUMBER,
    CLABE,
    ROUTING_NUMBER,
    bank,
    custody,
    provider_settings,
    sim,
    wire,
)
from tests.webhooks.helpers import BANK_SECRET, envelope, sign, with_secrets

log = get_logger("tests.redaction")

# What stands for a database password in a connection URL here. It protects nothing.
URL_PASSWORD = "not-a-real-database-password-0001"  # pragma: allowlist secret


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return with_secrets(settings)


@pytest.fixture
def output(capsys: pytest.CaptureFixture[str]) -> Iterator[Any]:
    """Everything the process writes while the test runs, through the real pipeline."""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    # As create_app configures it, and again here in case another test has left the root
    # logger with handlers of its own.
    configure_logging("INFO", "json")
    capsys.readouterr()

    def read() -> str:
        captured = capsys.readouterr()
        return captured.out + captured.err

    try:
        yield read
    finally:
        root.handlers, root.level = saved_handlers, saved_level


def assert_absent(written: str, **secrets: str) -> None:
    assert written.strip(), "nothing was logged, so nothing was shown to be kept out"
    leaked = [name for name, value in secrets.items() if value in written]
    assert leaked == []


# --- the API doing what it is meant to ---------------------------------------------------------


async def test_a_session_from_registration_to_logout_logs_no_password_and_no_token(
    client: httpx.AsyncClient, output: Any
) -> None:
    maria = await register_user(client)
    await client.post("/v1/auth/login", json={"email": maria.email, "password": PASSWORD + "x"})
    await client.get("/v1/me", headers=maria.headers)
    refreshed = await client.post("/v1/auth/refresh", json={"refresh_token": maria.refresh_token})
    await client.post("/v1/auth/logout", headers=maria.headers)
    # A credential that is refused is as secret as one that is accepted.
    await client.get("/v1/me", headers={"Authorization": "Bearer not-a-real-token-0123456789"})

    assert_absent(
        output(),
        password=PASSWORD,
        access_token=maria.access_token,
        refresh_token=maria.refresh_token,
        new_access_token=refreshed.json()["access_token"],
        new_refresh_token=refreshed.json()["refresh_token"],
        refused_credential="not-a-real-token-0123456789",
        # The signature alone is enough to forge nothing, and is still not logged.
        signature=maria.access_token.rsplit(".", 1)[1],
    )


async def test_issuing_and_using_an_agent_key_logs_no_key(
    client: httpx.AsyncClient, output: Any
) -> None:
    maria = await register_user(client)
    agent = await create_agent(client, maria)
    issued = await issue_key(client, maria, agent["id"], Scope.WALLET_READ)
    key: str = issued["key"]

    used = await client.get("/v1/wallets", headers=key_headers(key))
    refused = await client.get("/v1/transfers", headers=key_headers(key))

    assert (used.status_code, refused.status_code) == (200, 403)
    assert_absent(output(), key=key, secret_part=key.rsplit("_", 1)[1])


async def test_saving_a_bank_account_logs_no_account_number(
    app: FastAPI,
    client: httpx.AsyncClient,
    bank: SimBank,  # noqa: F811
    custody: SimCustody,  # noqa: F811
    output: Any,
) -> None:
    wire(app, bank, custody)
    maria = await register_user(client)

    async def save(asset: str, account_number: str, **more: str) -> httpx.Response:
        return await client.post(
            "/v1/beneficiaries",
            json={
                "asset": asset,
                "holder_name": "Maria Silva",
                "account_number": account_number,
                **more,
            },
            headers={**maria.headers, "Idempotency-Key": f"redaction-{asset}-{account_number}"},
        )

    saved = await save("USD", ACCOUNT_NUMBER, routing_number=ROUTING_NUMBER)
    mexican = await save("MXN", CLABE)
    # One the bank refuses: the refusal must not quote what it was sent.
    refused = await save("USD", "12", routing_number=ROUTING_NUMBER)
    # One the schema refuses: neither must the validation error.
    malformed = await save("USD", ACCOUNT_NUMBER + "9" * 80, routing_number=ROUTING_NUMBER)

    assert [r.status_code for r in (saved, mexican, refused, malformed)] == [201, 201, 422, 422]
    assert_absent(
        output(), account_number=ACCOUNT_NUMBER, routing_number=ROUTING_NUMBER, clabe=CLABE
    )
    for response in (saved, mexican, refused, malformed):
        assert ACCOUNT_NUMBER not in response.text
        assert CLABE not in response.text


async def test_receiving_a_webhook_logs_neither_its_signature_nor_its_body(
    client: httpx.AsyncClient, output: Any
) -> None:
    body = envelope("evt_redaction", "deposit.received", {"sender_name": "Joao Pereira"})
    good = sign(BANK_SECRET, body)
    forged = sign("f" * 40, body)

    accepted = await client.post(
        "/v1/webhooks/simbank", content=body, headers={"X-Signature": good}
    )
    refused = await client.post(
        "/v1/webhooks/simbank", content=body, headers={"X-Signature": forged}
    )

    assert (accepted.status_code, refused.status_code) == (200, 401)
    assert_absent(
        output(),
        secret=BANK_SECRET,
        signature=good.split("v1=")[1],
        forged_signature=forged.split("v1=")[1],
        sender="Joao Pereira",
    )


# --- a line of code that logs what it should not have --------------------------------------------


async def _careless(request: Request) -> dict[str, str]:
    """Every mistake at once: the headers, the parsed body, the raw body and a sentence
    with a secret written into it, each handed straight to the logger."""
    raw = await request.body()
    body = await request.json()
    log.info("careless.headers", headers=dict(request.headers))
    log.info("careless.header_pairs", headers=list(request.headers.items()))
    log.info("careless.body", body=body)
    log.info("careless.raw", raw=raw, text=raw.decode())
    log.info(f"careless.sentence password={body['password']} for {body['email']}")
    log.warning("careless.settings", settings=request.app.state.container.settings)
    logging.getLogger("third.party").warning(
        "sending Authorization: %s", request.headers["authorization"]
    )
    try:
        raise RuntimeError(f"refused token={body['refresh_token']}")
    except RuntimeError:
        log.exception("careless.exception")
    return {}


async def test_a_secret_handed_to_the_logger_by_mistake_is_not_written(
    app: FastAPI, client: httpx.AsyncClient, output: Any
) -> None:
    app.add_api_route("/careless", _careless, methods=["POST"])
    maria: RegisteredUser = await register_user(client)
    agent = await create_agent(client, maria)
    key: str = (await issue_key(client, maria, agent["id"], Scope.WALLET_READ))["key"]
    output()

    response = await client.post(
        "/careless",
        json={
            "email": maria.email,
            "password": PASSWORD,
            "refresh_token": maria.refresh_token,
            "api_key": key,
            "account_number": ACCOUNT_NUMBER,
            "routing_number": ROUTING_NUMBER,
            "clabe": CLABE,
        },
        headers={**maria.headers, "X-Signature": sign(BANK_SECRET, b"{}"), "Cookie": "sid=abc123"},
    )

    written = output()
    settings: Settings = app.state.container.settings
    assert response.status_code == 200
    assert_absent(
        written,
        password=PASSWORD,
        access_token=maria.access_token,
        refresh_token=maria.refresh_token,
        api_key=key,
        account_number=ACCOUNT_NUMBER,
        routing_number=ROUTING_NUMBER,
        clabe=CLABE,
        cookie="abc123",
        webhook_secret=BANK_SECRET,
        database_url=settings.database_url.get_secret_value(),
        signing_key=_secret(settings.jwt_signing_key),
        hash_key=_secret(settings.api_key_hash_key),
    )
    # The lines were written, with what was removed marked as removed, and what an
    # operator needs to find the request by still there.
    for event in ("careless.headers", "careless.body", "careless.raw", "careless.exception"):
        assert event in written
    assert REDACTED in written
    assert maria.email in written
    assert response.headers["x-request-id"] in written


def _secret(value: Any) -> str:
    assert value is not None
    return str(value.get_secret_value())


async def test_an_error_nobody_expected_is_logged_without_the_secrets_in_its_message(
    app: FastAPI, client: httpx.AsyncClient, output: Any
) -> None:
    maria = await register_user(client)

    async def crash(request: Request) -> None:
        raise RuntimeError(
            f"could not reach postgresql://app:{URL_PASSWORD}@db.internal/corridor"
            f" with Authorization: {request.headers['authorization']}"
        )

    app.add_api_route("/crash", crash)
    output()

    response = await client.get("/crash", headers=maria.headers)

    written = output()
    assert response.status_code == 500
    assert "request.unhandled_error" in written
    assert "Traceback" in written
    assert_absent(written, password=URL_PASSWORD, access_token=maria.access_token)
    assert URL_PASSWORD not in response.text


def test_the_settings_never_print_a_secret(settings: Settings) -> None:
    shown = repr(settings) + str(settings) + repr(dataclasses.asdict(_Holder(settings)))

    for secret in (
        settings.database_url,
        settings.redis_url,
        settings.jwt_signing_key,
        settings.api_key_hash_key,
        *settings.bank_rail_webhook_secrets,
    ):
        assert _secret(secret) not in shown


@dataclasses.dataclass
class _Holder:
    settings: Settings
