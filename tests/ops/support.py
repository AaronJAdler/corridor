"""Builders for the admin tests: an administrator who can log in, a deposit in suspense,
and a look at the audit log."""

import secrets
from typing import Any

import httpx

from corridor import identity, payments
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


async def suspended(db: Database, amount: str = "75.00") -> dict[str, Any]:
    """A bank deposit in US dollars that arrived at an account the bank never issued, so
    that it is on the books in suspense and is nobody's. Returns its row."""
    reference = f"dep_{secrets.token_hex(6)}"
    await payments.apply_bank_deposit_received(
        db,
        {
            "deposit_id": reference,
            "virtual_account_id": f"va_{secrets.token_hex(6)}",
            "asset": "USD",
            "amount": amount,
            "sender_name": "Somebody Else",
            "reference": "INV-1",
        },
    )
    return await deposit_row(db, reference)


async def deposit_row(db: Database, provider_ref: str) -> dict[str, Any]:
    (row,) = await rows(db, "SELECT * FROM deposits WHERE provider_ref = :ref", ref=provider_ref)
    return row


def recalled(deposit: dict[str, Any], amount: str = "75.00") -> dict[str, Any]:
    """The ``deposit.returned`` data of the bank taking a deposit back."""
    return {
        "deposit_id": deposit["provider_ref"],
        "asset": deposit["asset_code"],
        "amount": amount,
        "reason": "recalled_by_sender",
    }
