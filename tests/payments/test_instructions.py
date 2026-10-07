"""Deposit instructions: where a user sends money to, obtained from the provider once."""

import asyncio
from typing import Any

import pytest
from sqlalchemy import text

from corridor import payments
from corridor.identity import InsufficientScope, Scope, User
from corridor.payments import ProviderUnavailable
from corridor.platform.db import Database
from corridor.platform.money import UnknownAsset
from corridor.providers import SimBank, SimCustody, VirtualAccount, is_valid_address
from tests.payments.support import acting_as, agent_of, count, rows
from tests.support.providers import Sim

VIRTUAL_ACCOUNTS = "/bank/v1/virtual-accounts"
ADDRESSES = "/custody/v1/addresses"


async def instruct(
    db: Database, user: User, asset: str, bank: SimBank | None, custody: SimCustody | None
) -> payments.DepositInstruction:
    return await payments.get_deposit_instruction(
        db, acting_as(user), asset, bank=bank, custody=custody
    )


async def test_a_bank_asset_gets_a_virtual_account_from_the_bank(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    instruction = await instruct(db, maria, "USD", bank, custody)

    assert (instruction.user_id, instruction.asset, instruction.kind) == (maria.id, "USD", "bank")
    assert instruction.provider == "simbank"
    assert instruction.provider_ref.startswith("va_")
    assert set(instruction.details) == {"rail", "bank_name", "account_number", "routing_number"}
    assert instruction.details["rail"] == "ach"
    (stored,) = await rows(db, "SELECT * FROM deposit_instructions")
    assert stored["user_id"] == maria.id
    assert (stored["provider"], stored["provider_ref"]) == ("simbank", instruction.provider_ref)
    assert stored["details"] == dict(instruction.details)


async def test_an_asset_without_a_routing_number_stores_none(
    db: Database, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    instruction = await instruct(db, maria, "MXN", bank, custody)

    assert set(instruction.details) == {"rail", "bank_name", "account_number"}
    assert instruction.details["rail"] == "spei"


async def test_a_stablecoin_gets_a_deposit_address_from_the_custodian(
    db: Database, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    instruction = await instruct(db, maria, "USDC", bank, custody)

    assert (instruction.kind, instruction.provider) == ("chain", "simcustody")
    assert instruction.provider_ref.startswith("addr_")
    assert set(instruction.details) == {"network", "address"}
    assert is_valid_address(instruction.details["address"])


async def test_asking_again_returns_the_stored_instruction_without_asking_the_provider(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    first = await instruct(db, maria, "USD", bank, custody)

    second = await instruct(db, maria, "USD", bank, custody)

    assert second == first
    assert len(sim.recorder.sent("POST", VIRTUAL_ACCOUNTS)) == 1
    assert await count(db, "deposit_instructions") == 1


async def test_the_provider_is_called_with_a_key_made_of_the_user_and_the_asset(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    await instruct(db, maria, "USD", bank, custody)
    await instruct(db, maria, "USDC", bank, custody)

    (account,) = sim.recorder.sent("POST", VIRTUAL_ACCOUNTS)
    (address,) = sim.recorder.sent("POST", ADDRESSES)
    assert account.headers["Idempotency-Key"] == f"instr:{maria.id}:USD"
    assert address.headers["Idempotency-Key"] == f"instr:{maria.id}:USDC"
    assert str(maria.id).encode() in account.content
    assert str(maria.id).encode() in address.content


async def test_twenty_concurrent_requests_leave_one_instruction_and_one_account(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    found = await asyncio.gather(*(instruct(db, maria, "USD", bank, custody) for _ in range(20)))

    assert all(instruction == found[0] for instruction in found)
    assert await count(db, "deposit_instructions") == 1
    # What the bank holds, looked at directly: the control endpoints do not list accounts.
    assert len(sim.app.state.sim.bank._virtual_accounts) == 1


async def test_each_user_and_each_asset_has_an_instruction_of_its_own(
    db: Database, bank: SimBank, custody: SimCustody, maria: User, joao: User
) -> None:
    found = [
        await instruct(db, user, asset, bank, custody)
        for user in (maria, joao)
        for asset in ("USD", "BRL", "USDC")
    ]

    assert len({instruction.provider_ref for instruction in found}) == 6
    assert await count(db, "deposit_instructions") == 6


class WatchingBank:
    """A bank that, each time it is asked for an account, counts the transactions open in
    the test's database at that moment.

    It looks through a second, superuser connection at ``pg_stat_activity``: what the
    server itself says is open, whichever session, pool or code path opened it. A spy on
    one session object would see only the session it was given.
    """

    name = "simbank"

    def __init__(self, inner: SimBank, observer: Database) -> None:
        self._inner = inner
        self._observer = observer
        self.open_transactions: list[int] = []

    async def create_virtual_account(self, **arguments: Any) -> VirtualAccount:
        async with self._observer.transaction() as session:
            seen = await session.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity"
                    " WHERE datname = current_database() AND pid <> pg_backend_pid()"
                    " AND xact_start IS NOT NULL"
                )
            )
        self.open_transactions.append(int(seen.scalar_one()))
        return await self._inner.create_virtual_account(**arguments)


async def test_no_transaction_is_open_while_the_provider_is_called(
    db: Database, superuser_db: Database, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    watching = WatchingBank(bank, superuser_db)

    await payments.get_deposit_instruction(
        db,
        acting_as(maria),
        "USD",
        bank=watching,  # type: ignore[arg-type]
        custody=custody,
    )

    assert watching.open_transactions == [0]


async def test_the_watcher_does_see_a_transaction_that_is_open(
    db: Database, superuser_db: Database, bank: SimBank
) -> None:
    # The instrument itself: a provider call made with a transaction open is noticed.
    watching = WatchingBank(bank, superuser_db)

    async with db.transaction() as session:
        await session.execute(text("SELECT 1"))
        await watching.create_virtual_account(
            customer_reference="someone", asset_code="USD", idempotency_key="watched"
        )

    assert watching.open_transactions == [1]


@pytest.mark.parametrize(
    ("asset", "operation"),
    [("USD", "bank.create_virtual_account"), ("USDC", "custody.create_address")],
)
async def test_a_provider_that_does_not_answer_is_unavailable_and_nothing_is_stored(
    db: Database,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    maria: User,
    asset: str,
    operation: str,
) -> None:
    await sim.inject(operation, "error")

    with pytest.raises(ProviderUnavailable) as refusal:
        await instruct(db, maria, asset, bank, custody)

    assert (refusal.value.status, refusal.value.code) == (503, "provider_unavailable")
    assert await count(db, "deposit_instructions") == 0


async def test_a_request_after_the_provider_recovers_gets_the_account_it_made(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    await sim.inject("bank.create_virtual_account", "error_after_effect")
    with pytest.raises(ProviderUnavailable):
        await instruct(db, maria, "USD", bank, custody)

    instruction = await instruct(db, maria, "USD", bank, custody)

    assert list(sim.app.state.sim.bank._virtual_accounts) == [instruction.provider_ref]


async def test_a_provider_that_is_not_configured_is_unavailable(db: Database, maria: User) -> None:
    with pytest.raises(ProviderUnavailable):
        await instruct(db, maria, "USD", None, None)


async def test_an_unknown_asset_is_refused_before_anything_is_asked(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    with pytest.raises(UnknownAsset):
        await instruct(db, maria, "EUR", bank, custody)

    assert sim.recorder.requests == []


async def test_a_credential_without_the_deposits_scope_is_refused(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    with pytest.raises(InsufficientScope):
        await payments.get_deposit_instruction(
            db, agent_of(maria, Scope.WALLET_READ), "USD", bank=bank, custody=custody
        )

    assert sim.recorder.requests == []


class OneAccountBank:
    """A bank that gives every customer the same account: what must never be believed."""

    name = "simbank"

    async def create_virtual_account(
        self, *, customer_reference: str, asset_code: str, idempotency_key: str
    ) -> VirtualAccount:
        return VirtualAccount(
            id="va_shared",
            customer_reference=customer_reference,
            asset_code=asset_code,
            rail="ach",
            bank_name="Sim Bank",
            account_number="900000000001",
            routing_number=None,
        )


async def test_an_account_the_provider_already_gave_another_user_is_not_stored_twice(
    db: Database, custody: SimCustody, maria: User, joao: User
) -> None:
    shared: Any = OneAccountBank()
    await payments.get_deposit_instruction(
        db, acting_as(maria), "USD", bank=shared, custody=custody
    )

    with pytest.raises(RuntimeError, match="not this instruction's"):
        await payments.get_deposit_instruction(
            db, acting_as(joao), "USD", bank=shared, custody=custody
        )

    assert [
        row["user_id"] for row in await rows(db, "SELECT user_id FROM deposit_instructions")
    ] == [maria.id]


class OvertakenBank(OneAccountBank):
    """A bank whose answer arrives after another request has stored a different account
    for the same user and asset."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create_virtual_account(
        self, *, customer_reference: str, asset_code: str, idempotency_key: str
    ) -> VirtualAccount:
        async with self._db.transaction() as session:
            await session.execute(
                text(
                    "INSERT INTO deposit_instructions"
                    " (user_id, asset_code, provider, provider_ref, details, created_at)"
                    " VALUES (:user_id, :asset, 'simbank', 'va_other', '{}', now())"
                ),
                {"user_id": customer_reference, "asset": asset_code},
            )
        return await super().create_virtual_account(
            customer_reference=customer_reference,
            asset_code=asset_code,
            idempotency_key=idempotency_key,
        )


async def test_an_answer_that_disagrees_with_the_stored_instruction_is_not_returned(
    db: Database, custody: SimCustody, maria: User
) -> None:
    overtaken: Any = OvertakenBank(db)

    with pytest.raises(RuntimeError, match="not this instruction's"):
        await payments.get_deposit_instruction(
            db, acting_as(maria), "USD", bank=overtaken, custody=custody
        )

    stored = await rows(db, "SELECT provider_ref FROM deposit_instructions")
    assert [row["provider_ref"] for row in stored] == ["va_other"]
