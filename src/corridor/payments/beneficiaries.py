"""Beneficiaries: the external bank accounts a user withdraws to.

The account number passes through here once, on its way to the bank, and is never stored,
logged or returned. What is kept is the bank's token for the account and a masked value to
show the user which account it is.
"""

import uuid
from typing import Final, cast

from sqlalchemy import RowMapping, Table, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import audit, identity
from corridor.identity import Principal, Scope
from corridor.payments.deposits import position_of
from corridor.payments.errors import (
    AccountNotActive,
    BeneficiaryKeyReused,
    BeneficiaryRejected,
    InvalidBeneficiaryAccount,
    ProviderUnavailable,
    UnsupportedBeneficiaryAsset,
)
from corridor.payments.models import BeneficiaryRow
from corridor.payments.types import Beneficiary
from corridor.platform.clock import utcnow
from corridor.platform.db import Database
from corridor.platform.ids import new_id
from corridor.platform.logging import get_logger
from corridor.platform.money import get_asset
from corridor.platform.pagination import DEFAULT_LIMIT, Page, clamp_limit, encode_cursor
from corridor.providers import BankRail, ProviderError, ProviderRejected

log = get_logger(__name__)

# A Core table: every statement against it is written out below.
_beneficiaries = cast(Table, BeneficiaryRow.__table__)

CURSOR_KIND: Final = "beneficiaries"


async def create_beneficiary(
    db: Database,
    principal: Principal,
    *,
    bank: BankRail | None,
    asset: str,
    holder_name: str,
    account_number: str,
    routing_number: str | None,
    idempotency_key: str,
) -> Beneficiary:
    """Register one of the user's bank accounts with the bank, and keep its token.

    An entry point: the bank is called with no transaction open, and what it answers is
    stored in a transaction afterwards. The bank is given the caller's idempotency key,
    under the caller's own id so that two callers cannot collide on one, which makes a
    repeated request find the account it already registered instead of a second one.

    Only an active account registers one, and that is decided, in a transaction that has
    ended, before the bank is given any account details.
    """
    identity.require_scope(principal, Scope.BENEFICIARIES_WRITE)
    if get_asset(asset).kind != "fiat":
        raise UnsupportedBeneficiaryAsset
    user_id = principal.user_id
    owner = await db.run(lambda session: identity.get_user(session, user_id))
    if owner.status != "active":
        raise AccountNotActive
    if bank is None:
        raise ProviderUnavailable

    try:
        registered = await bank.create_beneficiary(
            customer_reference=str(user_id),
            asset_code=asset,
            holder_name=holder_name,
            account_number=account_number,
            routing_number=routing_number,
            idempotency_key=f"ben:{principal.actor_id}:{idempotency_key}",
        )
    except ProviderRejected as refusal:
        # A definite refusal: nothing was registered. Only the bank's code is read. Its
        # message may quote what it was sent.
        if refusal.code == "invalid_account":
            raise InvalidBeneficiaryAccount from None
        if refusal.code == "unsupported_asset":
            raise UnsupportedBeneficiaryAsset from None
        if refusal.code == "idempotency_conflict":
            raise BeneficiaryKeyReused from None
        log.warning("beneficiary.rejected", provider=refusal.provider, code=refusal.code)
        raise BeneficiaryRejected from None
    except ProviderError as error:
        # Unknown, or Corridor's own configuration. Nothing is stored either way, and the
        # same key finds the same account if the bank did register it.
        log.warning(
            "beneficiary.provider_failed", provider=error.provider, operation=error.operation
        )
        raise ProviderUnavailable from None

    async def store(session: AsyncSession) -> Beneficiary:
        inserted = await session.execute(
            pg_insert(_beneficiaries)
            .values(
                id=new_id(),
                user_id=user_id,
                asset_code=registered.asset_code,
                provider=bank.name,
                provider_ref=registered.id,
                holder_name=registered.holder_name,
                account_mask=registered.account_mask,
                created_at=utcnow(),
            )
            # A repeat of the request: the bank answered with the token already stored.
            .on_conflict_do_nothing(constraint="uq_beneficiaries_provider_provider_ref")
            .returning(_beneficiaries.c.id)
        )
        created = inserted.scalar_one_or_none() is not None
        rows = await session.execute(
            select(_beneficiaries).where(
                _beneficiaries.c.provider == bank.name,
                _beneficiaries.c.provider_ref == registered.id,
            )
        )
        beneficiary = _beneficiary(rows.mappings().one())
        if beneficiary.user_id != user_id:
            # The bank gave this user a token that is another user's. Never hand it over.
            raise RuntimeError(f"{bank.name} answered with a beneficiary that is not the caller's")
        if created:
            await audit.record(
                session,
                actor=(
                    audit.Actor.agent(principal.actor_id)
                    if principal.is_agent
                    else audit.Actor.user(principal.actor_id)
                ),
                action="beneficiary.created",
                principal_id=user_id,
                resource_type="beneficiary",
                resource_id=beneficiary.id,
                details={"asset": beneficiary.asset, "provider": beneficiary.provider},
            )
        return beneficiary

    return await db.run(store)


async def list_beneficiaries(
    session: AsyncSession,
    principal: Principal,
    *,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> Page[Beneficiary]:
    """One page of the saved accounts of the principal's user, newest first."""
    identity.require_scope(principal, Scope.BENEFICIARIES_READ)
    limit = clamp_limit(limit)
    scope = str(principal.user_id)
    query = select(_beneficiaries).where(_beneficiaries.c.user_id == principal.user_id)
    if cursor is not None:
        query = query.where(_beneficiaries.c.id < position_of(cursor, CURSOR_KIND, scope))
    rows = await session.execute(query.order_by(_beneficiaries.c.id.desc()).limit(limit + 1))
    found = [_beneficiary(row) for row in rows.mappings()]
    shown = found[:limit]
    return Page(
        items=tuple(shown),
        next_cursor=(
            encode_cursor(kind=CURSOR_KIND, scope=scope, position=str(shown[-1].id))
            if len(found) > limit
            else None
        ),
    )


async def find_own(
    session: AsyncSession, user_id: uuid.UUID, beneficiary_id: uuid.UUID
) -> Beneficiary | None:
    """The user's beneficiary with this id. Another user's is no beneficiary at all."""
    rows = await session.execute(
        select(_beneficiaries).where(
            _beneficiaries.c.id == beneficiary_id, _beneficiaries.c.user_id == user_id
        )
    )
    row = rows.mappings().one_or_none()
    return _beneficiary(row) if row is not None else None


async def get(session: AsyncSession, beneficiary_id: uuid.UUID) -> Beneficiary:
    """A beneficiary by id, whoever's it is: for the code that pays a withdrawal out."""
    rows = await session.execute(
        select(_beneficiaries).where(_beneficiaries.c.id == beneficiary_id)
    )
    return _beneficiary(rows.mappings().one())


def _beneficiary(row: RowMapping) -> Beneficiary:
    return Beneficiary(
        id=row["id"],
        user_id=row["user_id"],
        asset=row["asset_code"],
        provider=row["provider"],
        provider_ref=row["provider_ref"],
        holder_name=row["holder_name"],
        account_mask=row["account_mask"],
        created_at=row["created_at"],
    )
