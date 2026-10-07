"""Transfers over HTTP: the first path on which a request moves money."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from corridor import identity, wallets
from corridor.api.app import create_app
from corridor.api.deps import get_principal
from corridor.api.errors import PROBLEM_CONTENT_TYPE
from corridor.identity import Principal, Scope
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.ids import new_id
from tests.agents.support import give_open_policy
from tests.support.auth import RegisteredUser, register_user
from tests.support.ledger import fund

URL = "/v1/transfers"


async def deposit(db: Database, user: RegisteredUser, amount: int, asset: str = "USD") -> None:
    async with db.transaction() as session:
        wallet = await wallets.get_wallet(session, uuid.UUID(user.id), asset)
        await fund(session, wallet.available_account_id, amount, asset)


async def available(client: httpx.AsyncClient, user: RegisteredUser, asset: str = "USD") -> str:
    response = await client.get("/v1/wallets", headers=user.headers)
    assert response.status_code == 200, response.text
    return next(w["available"] for w in response.json()["wallets"] if w["asset"] == asset)


async def count(db: Database, table: str) -> int:
    async with db.transaction() as session:
        return int((await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one())  # noqa: S608


def body_for(recipient: RegisteredUser, amount: str = "30.00", **more: Any) -> dict[str, Any]:
    return {"recipient": f"@{recipient.user['handle']}", "asset": "USD", "amount": amount, **more}


async def post(
    client: httpx.AsyncClient,
    sender: RegisteredUser,
    body: dict[str, Any],
    *,
    key: str | None = "key-1",
) -> httpx.Response:
    headers = dict(sender.headers)
    if key is not None:
        headers["Idempotency-Key"] = key
    return await client.post(URL, json=body, headers=headers)


def assert_problem(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == PROBLEM_CONTENT_TYPE
    assert response.json()["code"] == code


def agent_for(user: RegisteredUser, *scopes: str) -> Principal:
    return Principal(
        user_id=uuid.UUID(user.id),
        actor_type="agent",
        actor_id=new_id(),
        role="user",
        scopes=frozenset(scopes),
        session_id=None,
    )


@pytest.fixture
async def maria(client: httpx.AsyncClient, db: Database) -> RegisteredUser:
    """A user with 100.00 USD."""
    user = await register_user(client, handle="maria")
    await deposit(db, user, 100_00)
    return user


@pytest.fixture
async def joao(client: httpx.AsyncClient) -> RegisteredUser:
    return await register_user(client, handle="joao")


async def test_a_transfer_is_created_and_the_money_has_moved(
    client: httpx.AsyncClient, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    response = await post(client, maria, body_for(joao, memo="for lunch"))

    assert response.status_code == 201, response.text
    assert response.headers["content-type"] == "application/json"
    assert "idempotent-replayed" not in response.headers
    body = response.json()
    assert set(body) == {
        "id",
        "status",
        "sender",
        "recipient",
        "asset",
        "amount",
        "fee",
        "memo",
        "created_at",
    }
    uuid.UUID(body["id"])
    assert body["status"] == "completed"
    assert body["sender"] == {"id": maria.id, "handle": "maria"}
    assert body["recipient"] == {"id": joao.id, "handle": "joao"}
    assert (body["asset"], body["amount"], body["fee"]) == ("USD", "30.00", "0.00")
    assert body["memo"] == "for lunch"
    assert await available(client, maria) == "70.00"
    assert await available(client, joao) == "30.00"


async def test_amounts_are_decimal_strings_in_the_assets_own_scale(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    await deposit(db, maria, 5_000_000, "USDC")

    response = await post(client, maria, {"recipient": joao.id, "asset": "USDC", "amount": "1.5"})

    assert response.status_code == 201, response.text
    assert (response.json()["amount"], response.json()["fee"]) == ("1.500000", "0.000000")
    assert await available(client, joao, "USDC") == "1.500000"


async def test_the_fee_is_shown_and_charged(
    settings: Settings, db: Database, client: httpx.AsyncClient
) -> None:
    charging = create_app(settings.model_copy(update={"transfer_fee_bps": 100}))
    async with charging.router.lifespan_context(charging):
        transport = httpx.ASGITransport(app=charging, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://fee.test") as http:
            sender = await register_user(http)
            recipient = await register_user(http)
            await deposit(db, sender, 100_00)

            response = await post(http, sender, body_for(recipient, "30.00"))

            assert response.status_code == 201, response.text
            assert (response.json()["amount"], response.json()["fee"]) == ("30.00", "0.30")
            assert await available(http, sender) == "69.70"
            assert await available(http, recipient) == "30.00"


async def test_a_transfer_can_be_read_back_by_both_sides(
    client: httpx.AsyncClient, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    created = (await post(client, maria, body_for(joao))).json()

    for side in (maria, joao):
        response = await client.get(f"{URL}/{created['id']}", headers=side.headers)
        assert response.status_code == 200, response.text
        assert response.json() == created


async def test_a_transfer_is_in_both_sides_lists(
    client: httpx.AsyncClient, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    created = (await post(client, maria, body_for(joao))).json()

    for side in (maria, joao):
        response = await client.get(URL, headers=side.headers)
        assert response.status_code == 200, response.text
        assert response.json() == {"items": [created], "next_cursor": None}


async def test_a_request_without_an_idempotency_key_is_refused_and_moves_nothing(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    response = await post(client, maria, body_for(joao), key=None)

    assert_problem(response, 400, "idempotency_key_required")
    assert await count(db, "transfers") == 0
    assert await available(client, maria) == "100.00"


async def test_a_retry_with_the_same_key_gets_the_first_answer_and_moves_nothing_more(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    first = await post(client, maria, body_for(joao))
    again = await post(client, maria, body_for(joao))

    assert (first.status_code, again.status_code) == (201, 201)
    assert again.json() == first.json()
    assert again.headers["idempotent-replayed"] == "true"
    assert await count(db, "transfers") == 1
    assert await available(client, maria) == "70.00"


async def test_50_identical_requests_at_once_make_one_transfer_and_get_one_answer(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    responses = await asyncio.gather(*(post(client, maria, body_for(joao)) for _ in range(50)))

    assert [response.status_code for response in responses] == [201] * 50
    assert all(response.json() == responses[0].json() for response in responses)
    assert [r.headers.get("idempotent-replayed") for r in responses].count(None) == 1
    assert await count(db, "transfers") == 1
    assert await count(db, "outbox_events") == 1
    assert await available(client, maria) == "70.00"
    assert await available(client, joao) == "30.00"


async def test_two_keys_are_two_transfers(
    client: httpx.AsyncClient, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    first = await post(client, maria, body_for(joao), key="key-1")
    second = await post(client, maria, body_for(joao), key="key-2")

    assert first.json()["id"] != second.json()["id"]
    assert await available(client, maria) == "40.00"


async def test_a_key_reused_for_a_different_body_is_refused(
    client: httpx.AsyncClient, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    await post(client, maria, body_for(joao, "30.00"))

    response = await post(client, maria, body_for(joao, "31.00"))

    assert_problem(response, 422, "idempotency_key_reused")
    assert await available(client, maria) == "70.00"


async def test_a_refusal_for_insufficient_funds_is_stored_and_a_top_up_does_not_change_it(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    refused = await post(client, maria, body_for(joao, "150.00"))
    assert_problem(refused, 402, "insufficient_funds")

    await deposit(db, maria, 500_00)
    again = await post(client, maria, body_for(joao, "150.00"))

    assert_problem(again, 402, "insufficient_funds")
    assert again.headers["idempotent-replayed"] == "true"
    assert again.json()["detail"] == refused.json()["detail"]
    assert await count(db, "transfers") == 0
    assert await available(client, maria) == "600.00"
    # The key stands for the attempt that was declined. A new attempt needs a new key.
    assert (await post(client, maria, body_for(joao, "150.00"), key="key-2")).status_code == 201


@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", URL), ("GET", URL), ("GET", f"{URL}/0199b7c2-6f0e-7b1a-9d53-2c1f4e8a7b10")],
)
async def test_a_request_without_a_token_is_refused(
    client: httpx.AsyncClient, method: str, path: str
) -> None:
    response = await client.request(
        method, path, json={} if method == "POST" else None, headers={"Idempotency-Key": "k"}
    )

    assert_problem(response, 401, "unauthenticated")


async def test_a_credential_without_the_transfers_create_scope_cannot_send(
    app: FastAPI,
    client: httpx.AsyncClient,
    db: Database,
    maria: RegisteredUser,
    joao: RegisteredUser,
) -> None:
    app.dependency_overrides[get_principal] = lambda: agent_for(maria, Scope.TRANSFERS_READ)

    response = await client.post(URL, json=body_for(joao), headers={"Idempotency-Key": "k"})

    assert_problem(response, 403, "insufficient_scope")
    assert await count(db, "transfers") == 0
    # Refused before the key was read: a credential that may not send cannot fill the
    # key table either.
    assert await count(db, "idempotency_keys") == 0


@pytest.mark.parametrize("path", [URL, f"{URL}/0199b7c2-6f0e-7b1a-9d53-2c1f4e8a7b10"])
async def test_a_credential_without_the_transfers_read_scope_cannot_read(
    app: FastAPI, client: httpx.AsyncClient, maria: RegisteredUser, path: str
) -> None:
    app.dependency_overrides[get_principal] = lambda: agent_for(maria, Scope.TRANSFERS_CREATE)

    assert_problem(await client.get(path), 403, "insufficient_scope")


async def test_an_agent_with_the_create_scope_sends_its_owners_money(
    app: FastAPI,
    client: httpx.AsyncClient,
    db: Database,
    maria: RegisteredUser,
    joao: RegisteredUser,
) -> None:
    agent = agent_for(maria, Scope.TRANSFERS_CREATE)
    await give_open_policy(db, agent)
    app.dependency_overrides[get_principal] = lambda: agent

    response = await client.post(URL, json=body_for(joao), headers={"Idempotency-Key": "k"})

    assert response.status_code == 201, response.text
    assert response.json()["sender"] == {"id": maria.id, "handle": "maria"}
    async with db.transaction() as session:
        rows = await session.execute(
            text("SELECT actor_id::text, initiated_by_type FROM idempotency_keys, transfers")
        )
        # The key belongs to the agent that sent it, not to the agent's owner.
        assert rows.all() == [(str(agent.actor_id), "agent")]


async def test_another_users_transfer_is_not_found(
    client: httpx.AsyncClient, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    created = (await post(client, maria, body_for(joao))).json()
    stranger = await register_user(client)

    theirs = await client.get(f"{URL}/{created['id']}", headers=stranger.headers)
    nothing = await client.get(f"{URL}/{new_id()}", headers=stranger.headers)

    assert_problem(theirs, 404, "transfer_not_found")
    assert_problem(nothing, 404, "transfer_not_found")
    assert theirs.json()["detail"] == nothing.json()["detail"]
    assert (await client.get(URL, headers=stranger.headers)).json()["items"] == []


async def test_a_transfer_id_that_is_not_an_id_is_a_422(
    client: httpx.AsyncClient, maria: RegisteredUser
) -> None:
    assert_problem(await client.get(f"{URL}/abc", headers=maria.headers), 422, "invalid_request")


@pytest.mark.parametrize(
    ("amount", "code"),
    [
        ("1.005", "invalid_amount"),
        ("-1", "invalid_amount"),
        ("1e3", "invalid_amount"),
        ("0", "invalid_amount"),
        ("0.00", "invalid_amount"),
        ("", "invalid_amount"),
        (" 1.00", "invalid_amount"),
        ("1,000.00", "invalid_amount"),
        (5, "invalid_request"),
        (5.5, "invalid_request"),
        (None, "invalid_request"),
    ],
)
async def test_a_malformed_amount_is_a_422_and_does_not_use_up_the_key(
    client: httpx.AsyncClient,
    db: Database,
    maria: RegisteredUser,
    joao: RegisteredUser,
    amount: object,
    code: str,
) -> None:
    response = await post(client, maria, {**body_for(joao), "amount": amount})

    assert_problem(response, 422, code)
    assert await count(db, "transfers") == 0
    assert await count(db, "idempotency_keys") == 0
    assert await available(client, maria) == "100.00"
    # Rejected before the key was read: the corrected request may use the same key.
    assert (await post(client, maria, body_for(joao, "1.00"))).status_code == 201


async def test_an_unsupported_asset_is_a_422(
    client: httpx.AsyncClient, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    response = await post(client, maria, {**body_for(joao), "asset": "EUR"})

    assert_problem(response, 422, "unknown_asset")


@pytest.mark.parametrize(
    "change",
    [{"note": "x"}, {"recipient": None}, {"recipient": ""}, {"asset": 7}, {"memo": 7}],
)
async def test_a_body_that_does_not_match_the_schema_is_a_422(
    client: httpx.AsyncClient, maria: RegisteredUser, joao: RegisteredUser, change: dict[str, Any]
) -> None:
    response = await post(client, maria, {**body_for(joao), **change})

    assert_problem(response, 422, "invalid_request")


async def test_a_memo_longer_than_140_characters_is_a_422(
    client: httpx.AsyncClient, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    response = await post(client, maria, body_for(joao, memo="m" * 141))

    assert_problem(response, 422, "invalid_memo")
    assert await available(client, maria) == "100.00"


async def test_a_transfer_to_oneself_is_a_422(
    client: httpx.AsyncClient, maria: RegisteredUser
) -> None:
    assert_problem(await post(client, maria, body_for(maria)), 422, "transfer_to_self")


async def test_a_transfer_to_nobody_is_a_404(
    client: httpx.AsyncClient, maria: RegisteredUser
) -> None:
    response = await post(client, maria, {"recipient": "@nobody", "asset": "USD", "amount": "1"})

    assert_problem(response, 404, "recipient_not_found")


def without_request_id(response: httpx.Response) -> dict[str, Any]:
    body: dict[str, Any] = response.json()
    assert body.pop("request_id") == response.headers["x-request-id"]
    return body


async def test_every_recipient_who_cannot_be_paid_gets_the_one_answer(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    # An address is not public, as a handle is. If an address nobody registered were
    # answered differently from one whose account cannot be paid, the difference would
    # say which addresses have, or had, an account.
    closed = await register_user(client, handle="closed", email="closed@example.com")
    async with db.transaction() as session:
        await identity.close_user(session, uuid.UUID(closed.id))
    recipients = {
        "an address nobody registered": "nobody@example.com",
        "the address of a closed account": "closed@example.com",
        "the handle of a closed account": "@closed",
        "the id of a closed account": closed.id,
        "a handle nobody has": "@nobody",
        "an id nobody has": str(new_id()),
        "nothing that could name anyone": "not a recipient",
    }

    answers = {
        what: await post(
            client, maria, {"recipient": to, "asset": "USD", "amount": "1.00"}, key=f"key-{n}"
        )
        for n, (what, to) in enumerate(recipients.items())
    }

    first = answers["an address nobody registered"]
    assert_problem(first, 404, "recipient_not_found")
    for what, answer in answers.items():
        assert answer.status_code == 404, what
        assert answer.headers["content-type"] == PROBLEM_CONTENT_TYPE, what
        assert without_request_id(answer) == without_request_id(first), what
    assert await available(client, maria) == "100.00"
    assert await count(db, "transfers") == 0
    # The control: a recipient who can be paid, by address, is.
    paid = await post(
        client,
        maria,
        {"recipient": joao.email, "asset": "USD", "amount": "1.00"},
        key="key-paid",
    )
    assert paid.status_code == 201, paid.text


async def test_an_account_closed_while_it_is_being_paid_gets_the_same_answer(
    client: httpx.AsyncClient,
    db: Database,
    maria: RegisteredUser,
    joao: RegisteredUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Found as open, and closed by the time the movement is authorised: the refusal then
    # comes from another module, and must not be told apart from the one above.
    unknown = await post(
        client, maria, {"recipient": "nobody@example.com", "asset": "USD", "amount": "1.00"}
    )
    found = identity.find_user

    async def find_then_close(session: Any, identifier: str) -> identity.User | None:
        user = await found(session, identifier)
        if user is not None:
            await identity.close_user(session, user.id)
        return user

    monkeypatch.setattr(identity, "find_user", find_then_close)

    closing = await post(client, maria, body_for(joao), key="key-2")

    assert closing.status_code == 404
    assert without_request_id(closing) == without_request_id(unknown)


async def test_a_restricted_user_gets_a_403_and_keeps_their_money(
    client: httpx.AsyncClient, db: Database, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    async with db.transaction() as session:
        await identity.restrict_user(session, uuid.UUID(maria.id), "review")

    response = await post(client, maria, body_for(joao))

    assert_problem(response, 403, "user_restricted")
    assert await available(client, maria) == "100.00"


async def test_a_refusal_carries_the_id_of_the_request_that_received_it(
    client: httpx.AsyncClient, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    refused = await post(client, maria, body_for(joao, "150.00"))
    again = await post(client, maria, body_for(joao, "150.00"))

    assert refused.json()["request_id"] == refused.headers["x-request-id"]
    assert again.json()["request_id"] == again.headers["x-request-id"]
    assert again.json()["request_id"] != refused.json()["request_id"]


async def test_the_list_pages_over_http_without_repeating_or_skipping(
    client: httpx.AsyncClient, maria: RegisteredUser, joao: RegisteredUser
) -> None:
    for cents in range(1, 6):
        created = await post(client, maria, body_for(joao, f"0.0{cents}"), key=f"key-{cents}")
        assert created.status_code == 201, created.text

    amounts: list[str] = []
    params: dict[str, str | int] = {"limit": 2}
    for _ in range(3):
        response = await client.get(URL, headers=joao.headers, params=params)
        assert response.status_code == 200, response.text
        body = response.json()
        amounts += [item["amount"] for item in body["items"]]
        if body["next_cursor"] is None:
            break
        params = {"limit": 2, "cursor": body["next_cursor"]}

    assert amounts == ["0.05", "0.04", "0.03", "0.02", "0.01"]
    assert body["next_cursor"] is None


@pytest.mark.parametrize("cursor", ["garbage", "", "e30", "!!!"])
async def test_a_bad_cursor_is_a_422(
    client: httpx.AsyncClient, maria: RegisteredUser, cursor: str
) -> None:
    response = await client.get(URL, headers=maria.headers, params={"cursor": cursor})

    assert_problem(response, 422, "invalid_cursor")


@pytest.mark.parametrize("limit", ["0", "-3", "many"])
async def test_a_limit_that_is_not_a_positive_whole_number_is_a_422(
    client: httpx.AsyncClient, maria: RegisteredUser, limit: str
) -> None:
    response = await client.get(URL, headers=maria.headers, params={"limit": limit})

    assert_problem(response, 422, "invalid_request")


@pytest.fixture
async def second_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Another client of the same app, as a second device would be."""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://corridor.test") as http:
        yield http


async def test_the_same_key_from_two_users_is_two_transfers(
    client: httpx.AsyncClient,
    second_client: httpx.AsyncClient,
    db: Database,
    maria: RegisteredUser,
    joao: RegisteredUser,
) -> None:
    await deposit(db, joao, 100_00)

    first = await post(client, maria, body_for(joao, "1.00"), key="shared")
    second = await post(second_client, joao, body_for(maria, "2.00"), key="shared")

    assert (first.status_code, second.status_code) == (201, 201)
    assert first.json()["id"] != second.json()["id"]
