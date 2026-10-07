"""Reading transfers: each is visible to its two sides and to nobody else."""

import uuid

import pytest

from corridor import payments
from corridor.identity import InsufficientScope, Scope, User
from corridor.payments import Transfer
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.errors import InvalidRequest
from corridor.platform.ids import new_id
from corridor.platform.pagination import InvalidCursor, encode_cursor
from tests.payments.support import acting_as, add_person, agent_of, deposit, send


@pytest.fixture
async def ana(db: Database) -> User:
    async with db.transaction() as session:
        return await add_person(session, "ana")


async def get(db: Database, user: User, transfer_id: uuid.UUID) -> Transfer:
    async with db.transaction() as session:
        return await payments.get_transfer(session, acting_as(user), transfer_id)


async def listed(
    db: Database, user: User, *, cursor: str | None = None, limit: int = 50
) -> tuple[list[uuid.UUID], str | None]:
    async with db.transaction() as session:
        page = await payments.list_transfers(session, acting_as(user), cursor=cursor, limit=limit)
    return [transfer.id for transfer in page.items], page.next_cursor


async def test_the_sender_and_the_recipient_can_both_read_a_transfer(
    db: Database, settings: Settings, maria: User, joao: User
) -> None:
    await deposit(db, maria, 10_00)
    sent = await send(db, settings, maria, joao, 1_00, memo="rent")

    assert await get(db, maria, sent.id) == sent
    assert await get(db, joao, sent.id) == sent


async def test_a_transfer_between_two_others_is_not_found(
    db: Database, settings: Settings, maria: User, joao: User, ana: User
) -> None:
    await deposit(db, maria, 10_00)
    sent = await send(db, settings, maria, joao, 1_00)

    with pytest.raises(payments.TransferNotFound) as refusal:
        await get(db, ana, sent.id)

    assert (refusal.value.status, refusal.value.code) == (404, "transfer_not_found")


async def test_someone_elses_transfer_and_no_transfer_get_the_same_answer(
    db: Database, settings: Settings, maria: User, joao: User, ana: User
) -> None:
    await deposit(db, maria, 10_00)
    sent = await send(db, settings, maria, joao, 1_00)

    with pytest.raises(payments.TransferNotFound) as hidden:
        await get(db, ana, sent.id)
    with pytest.raises(payments.TransferNotFound) as absent:
        await get(db, ana, new_id())

    assert hidden.value.detail == absent.value.detail
    assert hidden.value.extra == absent.value.extra == {}


@pytest.mark.parametrize("scopes", [(), (Scope.TRANSFERS_CREATE, Scope.WALLET_READ)])
async def test_reading_a_transfer_needs_the_transfers_read_scope(
    db: Database, settings: Settings, maria: User, joao: User, scopes: tuple[str, ...]
) -> None:
    await deposit(db, maria, 10_00)
    sent = await send(db, settings, maria, joao, 1_00)

    async with db.transaction() as session:
        with pytest.raises(InsufficientScope):
            await payments.get_transfer(session, agent_of(maria, *scopes), sent.id)
        with pytest.raises(InsufficientScope):
            await payments.list_transfers(session, agent_of(maria, *scopes))


async def test_an_agent_with_the_read_scope_reads_its_owners_transfers(
    db: Database, settings: Settings, maria: User, joao: User
) -> None:
    await deposit(db, maria, 10_00)
    sent = await send(db, settings, maria, joao, 1_00)
    reader = agent_of(joao, Scope.TRANSFERS_READ)

    async with db.transaction() as session:
        assert await payments.get_transfer(session, reader, sent.id) == sent
        assert (await payments.list_transfers(session, reader)).items == (sent,)


async def test_a_list_holds_what_the_user_sent_and_received_newest_first(
    db: Database, settings: Settings, maria: User, joao: User, ana: User
) -> None:
    await deposit(db, maria, 10_00)
    await deposit(db, joao, 10_00)
    first = await send(db, settings, maria, joao, 1_00)
    second = await send(db, settings, joao, maria, 2_00)
    third = await send(db, settings, maria, ana, 3_00)
    between_others = await send(db, settings, joao, ana, 4_00)

    assert await listed(db, maria) == ([third.id, second.id, first.id], None)
    assert await listed(db, joao) == ([between_others.id, second.id, first.id], None)
    assert await listed(db, ana) == ([between_others.id, third.id], None)


async def test_a_user_with_no_transfers_gets_an_empty_page(db: Database, maria: User) -> None:
    assert await listed(db, maria) == ([], None)


async def test_a_list_pages_without_repeating_or_skipping_whatever_arrives_in_between(
    db: Database, settings: Settings, maria: User, joao: User
) -> None:
    await deposit(db, maria, 100_00)
    await deposit(db, joao, 100_00)
    sent = [
        # Alternating directions, so a page is cut across both sides of the list.
        await send(db, settings, *((maria, joao) if cents % 2 else (joao, maria)), cents)
        for cents in range(1, 6)
    ]

    seen: list[uuid.UUID] = []
    cursor: str | None = None
    for _ in range(3):
        page, cursor = await listed(db, maria, cursor=cursor, limit=2)
        seen += page
        if cursor is None:
            break
        await send(db, settings, maria, joao, 99)

    assert seen == [transfer.id for transfer in reversed(sent)]
    assert cursor is None


async def test_a_page_that_ends_exactly_at_the_end_has_no_next_cursor(
    db: Database, settings: Settings, maria: User, joao: User
) -> None:
    await deposit(db, maria, 10_00)
    for _ in range(2):
        await send(db, settings, maria, joao, 1_00)

    page, cursor = await listed(db, maria, limit=2)

    assert (len(page), cursor) == (2, None)


async def test_a_cursor_from_another_users_list_is_refused(
    db: Database, settings: Settings, maria: User, joao: User
) -> None:
    await deposit(db, maria, 10_00)
    for _ in range(2):
        await send(db, settings, maria, joao, 1_00)
    _, marias = await listed(db, maria, limit=1)
    assert marias is not None

    with pytest.raises(InvalidCursor):
        await listed(db, joao, cursor=marias)


@pytest.mark.parametrize("position", ["not-a-uuid", "", 7, "0199b7c2"])
async def test_a_cursor_whose_position_is_not_a_transfer_id_is_refused(
    db: Database, maria: User, position: int | str
) -> None:
    forged = encode_cursor(kind="transfers", scope=str(maria.id), position=position)

    with pytest.raises(InvalidCursor):
        await listed(db, maria, cursor=forged)


async def test_a_cursor_of_another_kind_of_list_is_refused(db: Database, maria: User) -> None:
    other = encode_cursor(kind="wallet_entries", scope=str(maria.id), position=str(new_id()))

    with pytest.raises(InvalidCursor):
        await listed(db, maria, cursor=other)


async def test_a_page_of_less_than_one_is_refused(db: Database, maria: User) -> None:
    with pytest.raises(InvalidRequest):
        await listed(db, maria, limit=0)
