"""Builders for identity tests: a user in a line, and a look at what was stored."""

import uuid
from typing import Any, Final, Literal

import argon2
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import identity
from corridor.identity import User

# Passwords for test users. They protect nothing.
PASSWORD: Final = "correct horse battery staple"  # pragma: allowlist secret
WRONG_PASSWORD: Final = "incorrect horse battery staple"  # pragma: allowlist secret

# A real Argon2id hash of PASSWORD, made once, with the cheapest parameters there are.
PASSWORD_HASH: Final = argon2.PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(
    PASSWORD
)


async def add_user(
    session: AsyncSession,
    name: str = "maria",
    *,
    role: Literal["user", "admin"] = "user",
    password_hash: str = PASSWORD_HASH,
) -> User:
    return await identity.register(
        session,
        email=f"{name}@example.com",
        handle=name,
        display_name=name.title(),
        password_hash=password_hash,
        role=role,
    )


async def close_account(session: AsyncSession, user_id: uuid.UUID) -> None:
    """Close an account directly: no service function does it yet."""
    await session.execute(
        text("UPDATE users SET status = 'closed' WHERE id = :id"), {"id": user_id}
    )


async def user_row(session: AsyncSession, user_id: uuid.UUID) -> dict[str, Any]:
    """Everything stored for a user, including what a ``User`` leaves out."""
    row = (await session.execute(text("SELECT * FROM users WHERE id = :id"), {"id": user_id})).one()
    return dict(row._mapping)


async def count(session: AsyncSession, table: Literal["users", "refresh_tokens"]) -> int:
    return int((await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one())  # noqa: S608
