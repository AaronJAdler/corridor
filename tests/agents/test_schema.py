"""What the agent tables enforce on their own, with no help from the application.

These tests write rows with plain SQL, the way a buggy or hostile caller would, and check
that PostgreSQL says no.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from corridor.platform.db import (
    CHECK_VIOLATION,
    UNIQUE_VIOLATION,
    Database,
    constraint_of,
    sqlstate_of,
)
from corridor.platform.ids import new_id
from tests.agents.support import rows

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
FOREIGN_KEY_VIOLATION = "23503"
INSUFFICIENT_PRIVILEGE = "42501"
NOT_NULL_VIOLATION = "23502"

# Not a hash of anything: 64 hexadecimal characters, which is all the column asks for.
A_HASH = "ab" * 32


async def add_agent(session: AsyncSession, **overrides: Any) -> uuid.UUID:
    agent = new_id()
    values = {
        "id": agent,
        "owner": new_id(),
        "name": "Bill payer",
        "status": "active",
        "now": NOW,
        **overrides,
    }
    await session.execute(
        text(
            "INSERT INTO agents (id, owner_user_id, name, status, created_at)"
            " VALUES (:id, :owner, :name, :status, :now)"
        ),
        values,
    )
    return agent


async def add_key(session: AsyncSession, agent: uuid.UUID, **overrides: Any) -> uuid.UUID:
    key = new_id()
    values = {
        "id": key,
        "agent": agent,
        "prefix": uuid.uuid4().hex[:12],
        "key_hash": A_HASH,
        "scopes": ["wallet:read"],
        "now": NOW,
        **overrides,
    }
    await session.execute(
        text(
            "INSERT INTO agent_keys (id, agent_id, prefix, key_hash, scopes, expires_at,"
            " revoked_at, last_used_at, created_at)"
            " VALUES (:id, :agent, :prefix, :key_hash, :scopes, NULL, NULL, NULL, :now)"
        ),
        values,
    )
    return key


async def refused(db: Database, statement: str, **parameters: Any) -> DBAPIError:
    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            await session.execute(text(statement), parameters)
    return failure.value


@pytest.fixture
async def agent(db: Database) -> uuid.UUID:
    async with db.transaction() as session:
        return await add_agent(session)


@pytest.fixture
async def key(db: Database, agent: uuid.UUID) -> uuid.UUID:
    async with db.transaction() as session:
        return await add_key(session, agent)


@pytest.mark.parametrize("status", ["deleted", "ACTIVE", ""])
async def test_an_agents_status_is_one_of_three(db: Database, status: str) -> None:
    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            await add_agent(session, status=status)

    assert sqlstate_of(failure.value) == CHECK_VIOLATION
    assert constraint_of(failure.value) == "ck_agents_status"


@pytest.mark.parametrize("name", ["", "x" * 101])
async def test_an_agents_name_has_between_1_and_100_characters(db: Database, name: str) -> None:
    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            await add_agent(session, name=name)

    assert constraint_of(failure.value) == "ck_agents_name"


async def test_a_key_belongs_to_an_agent_that_exists(db: Database) -> None:
    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            await add_key(session, new_id())

    assert sqlstate_of(failure.value) == FOREIGN_KEY_VIOLATION
    assert constraint_of(failure.value) == "fk_agent_keys_agent_id_agents"


async def test_no_two_keys_share_a_prefix(db: Database, agent: uuid.UUID) -> None:
    async with db.transaction() as session:
        await add_key(session, agent, prefix="abcdefghijkl")

    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            await add_key(session, await add_agent(session), prefix="abcdefghijkl")

    assert sqlstate_of(failure.value) == UNIQUE_VIOLATION
    assert constraint_of(failure.value) == "uq_agent_keys_prefix"


@pytest.mark.parametrize(
    "prefix", ["", "abcdefghijk", "abcdefghijklm", "ABCDEFGHIJKL", "abcdef_hijkl"]
)
async def test_a_prefix_is_twelve_lower_case_letters_and_digits(
    db: Database, agent: uuid.UUID, prefix: str
) -> None:
    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            await add_key(session, agent, prefix=prefix)

    assert constraint_of(failure.value) == "ck_agent_keys_prefix"


@pytest.mark.parametrize("key_hash", ["", "ab" * 31, "AB" * 32, "ck_test_abcdefghijkl_" + "S" * 43])
async def test_only_a_sha256_digest_in_hex_can_be_stored_as_a_keys_hash(
    db: Database, agent: uuid.UUID, key_hash: str
) -> None:
    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            await add_key(session, agent, key_hash=key_hash)

    assert constraint_of(failure.value) == "ck_agent_keys_key_hash"


async def test_no_key_can_be_stored_with_the_scope_of_a_users_own_session(
    db: Database, agent: uuid.UUID
) -> None:
    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            await add_key(session, agent, scopes=["wallet:read", "*"])

    assert sqlstate_of(failure.value) == CHECK_VIOLATION
    assert constraint_of(failure.value) == "ck_agent_keys_scopes_never_all"


async def test_a_key_has_scopes_even_if_they_are_none(db: Database, agent: uuid.UUID) -> None:
    with pytest.raises(DBAPIError) as failure:
        async with db.transaction() as session:
            await add_key(session, agent, scopes=None)

    assert sqlstate_of(failure.value) == NOT_NULL_VIOLATION


@pytest.mark.parametrize("table", ["agents", "agent_keys"])
async def test_the_application_cannot_delete_an_agent_or_a_key(
    db: Database, key: uuid.UUID, table: str
) -> None:
    failure = await refused(db, f"DELETE FROM {table}")  # noqa: S608

    assert sqlstate_of(failure) == INSUFFICIENT_PRIVILEGE
    assert len(await rows(db, f"SELECT id FROM {table}")) == 1  # noqa: S608


@pytest.mark.parametrize(
    "assignment",
    ["owner_user_id = :id", "name = 'Renamed'", "id = :id", "created_at = :now"],
)
async def test_the_application_can_change_nothing_of_an_agent_but_its_status(
    db: Database, agent: uuid.UUID, assignment: str
) -> None:
    failure = await refused(db, f"UPDATE agents SET {assignment}", id=new_id(), now=NOW)  # noqa: S608

    assert sqlstate_of(failure) == INSUFFICIENT_PRIVILEGE


@pytest.mark.parametrize(
    "assignment",
    [
        "scopes = ARRAY['transfers:create']",
        "expires_at = NULL",
        "key_hash = :hash",
        "prefix = 'zzzzzzzzzzzz'",
        "agent_id = :id",
        "created_at = :now",
    ],
)
async def test_the_application_cannot_widen_rehash_or_move_a_key(
    db: Database, key: uuid.UUID, assignment: str
) -> None:
    failure = await refused(
        db,
        f"UPDATE agent_keys SET {assignment}",  # noqa: S608
        id=new_id(),
        now=NOW,
        hash="cd" * 32,
    )

    assert sqlstate_of(failure) == INSUFFICIENT_PRIVILEGE


async def test_the_application_can_change_what_is_meant_to_change(
    db: Database, agent: uuid.UUID, key: uuid.UUID
) -> None:
    async with db.transaction() as session:
        await session.execute(text("UPDATE agents SET status = 'paused'"))
        await session.execute(
            text("UPDATE agent_keys SET revoked_at = :now, last_used_at = :now"), {"now": NOW}
        )

    assert await rows(db, "SELECT status FROM agents") == [{"status": "paused"}]
    assert await rows(db, "SELECT revoked_at, last_used_at FROM agent_keys") == [
        {"revoked_at": NOW, "last_used_at": NOW}
    ]


async def test_the_agents_of_an_owner_and_the_keys_of_an_agent_are_indexed(
    owner_db: Database,
) -> None:
    found = await rows(
        owner_db,
        "SELECT indexname FROM pg_indexes WHERE tablename IN ('agents', 'agent_keys')"
        " ORDER BY indexname",
    )

    assert [row["indexname"] for row in found] == [
        "ix_agent_keys_agent_id",
        "ix_agents_owner_user_id",
        "pk_agent_keys",
        "pk_agents",
        "uq_agent_keys_prefix",
    ]
