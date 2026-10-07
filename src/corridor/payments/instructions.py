"""Deposit instructions: the virtual account or the address a user pays an asset into.

There is one per user and asset, obtained from the provider the first time it is asked for
and kept. The provider is called between two transactions and never inside one.
"""

from typing import cast

from sqlalchemy import RowMapping, Table, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import identity
from corridor.identity import Principal, Scope
from corridor.payments.errors import AccountNotActive, ProviderUnavailable
from corridor.payments.models import DepositInstructionRow
from corridor.payments.types import BANK_PROVIDER, DepositInstruction, FlowKind
from corridor.platform.clock import utcnow
from corridor.platform.db import Database
from corridor.platform.logging import get_logger
from corridor.platform.money import get_asset
from corridor.providers import BankRail, Custodian, ProviderError

log = get_logger(__name__)

# A Core table: every statement against it is written out below.
_instructions = cast(Table, DepositInstructionRow.__table__)


async def get_deposit_instruction(
    db: Database,
    principal: Principal,
    asset: str,
    *,
    bank: BankRail | None,
    custody: Custodian | None,
) -> DepositInstruction:
    """Where the principal's user deposits ``asset``.

    An entry point: it runs two transactions with the provider call between them. The
    first reads what is stored. If there is nothing, the provider is asked with no
    transaction open, under a key made of the user and the asset, so that every request
    for the same instruction, concurrent or repeated, is answered with the same account.
    The second stores the answer unless another request already has, and reads back
    whichever was stored.

    An instruction that exists is shown to its user whatever state the account is in. A
    new one is made only for an active account, and that is decided before the provider
    is asked for anything.
    """
    identity.require_scope(principal, Scope.DEPOSITS_READ)
    kind = get_asset(asset).kind
    user_id = principal.user_id

    async def read(session: AsyncSession) -> DepositInstruction | None:
        found = await _find(session, principal, asset)
        if found is None and (await identity.get_user(session, user_id)).status != "active":
            raise AccountNotActive
        return found

    stored = await db.run(read)
    if stored is not None:
        return stored

    key = f"instr:{user_id}:{asset}"
    details: dict[str, str]
    try:
        if kind == "fiat":
            if bank is None:
                raise ProviderUnavailable
            account = await bank.create_virtual_account(
                customer_reference=str(user_id), asset_code=asset, idempotency_key=key
            )
            provider, provider_ref = bank.name, account.id
            details = {
                "rail": account.rail,
                "bank_name": account.bank_name,
                "account_number": account.account_number,
            }
            if account.routing_number is not None:
                details["routing_number"] = account.routing_number
        else:
            if custody is None:
                raise ProviderUnavailable
            address = await custody.create_address(
                customer_reference=str(user_id), asset_code=asset, idempotency_key=key
            )
            provider, provider_ref = custody.name, address.id
            details = {"network": address.network, "address": address.address}
    except ProviderError as error:
        # Whichever way the call went wrong, nothing was stored here and the same key
        # finds the same account next time. The reason is an operator's, not the user's.
        log.warning(
            "deposit_instruction.provider_failed",
            provider=error.provider,
            operation=error.operation,
            asset=asset,
        )
        raise ProviderUnavailable from error

    async def store(session: AsyncSession) -> DepositInstruction:
        await session.execute(
            pg_insert(_instructions)
            .values(
                user_id=user_id,
                asset_code=asset,
                provider=provider,
                provider_ref=provider_ref,
                details=details,
                created_at=utcnow(),
            )
            # A concurrent request for the same instruction stored it first, and the two
            # rows collide on both unique constraints at once. Naming one of them here
            # would let the other raise, so neither is named and the row is checked below.
            .on_conflict_do_nothing()
        )
        found = await _find(session, principal, asset)
        if found is None or (found.provider, found.provider_ref) != (provider, provider_ref):
            # The insert was skipped for some other reason: the provider gave this user an
            # account that is stored under another user or another asset. Never guess.
            raise RuntimeError(
                f"{provider} answered {key} with an account that is not this instruction's"
            )
        return found

    return await db.run(store)


async def find_by_provider_ref(
    session: AsyncSession, provider: str, provider_ref: str
) -> DepositInstruction | None:
    """Whose instruction a provider's account or address is. The one way a deposit that
    arrives at it is attributed to a user."""
    rows = await session.execute(
        select(_instructions).where(
            _instructions.c.provider == provider, _instructions.c.provider_ref == provider_ref
        )
    )
    row = rows.mappings().one_or_none()
    return _instruction(row) if row is not None else None


async def _find(
    session: AsyncSession, principal: Principal, asset: str
) -> DepositInstruction | None:
    rows = await session.execute(
        select(_instructions).where(
            _instructions.c.user_id == principal.user_id, _instructions.c.asset_code == asset
        )
    )
    row = rows.mappings().one_or_none()
    return _instruction(row) if row is not None else None


def _instruction(row: RowMapping) -> DepositInstruction:
    # The bank issues accounts and the custodian addresses; there is no third provider.
    kind: FlowKind = "bank" if row["provider"] == BANK_PROVIDER else "chain"
    return DepositInstruction(
        user_id=row["user_id"],
        asset=row["asset_code"],
        kind=kind,
        provider=row["provider"],
        provider_ref=row["provider_ref"],
        details=row["details"],
        created_at=row["created_at"],
    )
