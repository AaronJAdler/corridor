"""Builders for the admin tests: an administrator who can log in, and a look at the audit
log."""

import secrets
from typing import Any

import httpx

from corridor import identity
from corridor.platform.config import Settings
from corridor.platform.db import Database
from tests.payments.support import rows
from tests.support.auth import PASSWORD, RegisteredUser, login


async def admin(client: httpx.AsyncClient, db: Database, settings: Settings) -> RegisteredUser:
    """An administrator, registered directly and logged in over HTTP. Registration over
    HTTP makes ordinary users only."""
    name = f"admin_{secrets.token_hex(5)}"
    password_hash = await identity.PasswordHasher(settings).hash(PASSWORD)
    async with db.transaction() as session:
        user = await identity.register(
            session,
            email=f"{name}@example.com",
            handle=name,
            display_name=name.title(),
            password_hash=password_hash,
            role="admin",
        )
    tokens = await login(client, user.email)
    return RegisteredUser(
        user={"id": str(user.id), "email": user.email}, password=PASSWORD, tokens=tokens
    )


async def audited(db: Database, action: str) -> list[dict[str, Any]]:
    return await rows(
        db,
        "SELECT actor_type, actor_id, resource_type, resource_id, outcome, details"
        " FROM audit_events WHERE action = :action ORDER BY id",
        action=action,
    )


def keyed(user: RegisteredUser, key: str | None = None) -> dict[str, str]:
    """The user's headers, with an idempotency key: a new one unless one is given."""
    return {**user.headers, "Idempotency-Key": key or f"adm-{secrets.token_hex(8)}"}
