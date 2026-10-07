"""Withdrawal requests: funds are reserved first, and only then is a provider asked."""

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from corridor import identity, payments
from corridor.identity import InsufficientScope, Scope, User
from corridor.ledger import InsufficientFunds
from corridor.payments import (
    BeneficiaryAssetMismatch,
    BeneficiaryNotFound,
    DuplicateWithdrawal,
    InvalidAddress,
    InvalidWithdrawalTarget,
    WithdrawalNotCancelable,
    WithdrawalNotFound,
)
from corridor.platform.config import Settings
from corridor.platform.db import (
    LOCK_NOT_AVAILABLE,
    Database,
    advisory_xact_lock,
    lock_key,
    sqlstate_of,
)
from corridor.platform.ids import new_id
from corridor.platform.money import InvalidAmount, UnknownAsset
from corridor.providers import SimBank
from corridor.risk import UserRestricted
from tests.payments.support import (
    acting_as,
    add_beneficiary,
    agent_of,
    available,
    count,
    deposit,
    entries,
    held,
    rows,
    withdraw,
    withdrawal_row,
)
from tests.support.providers import CLABE, EXTERNAL_ADDRESS


@pytest.fixture(name="settings")
def without_a_minimum_fee(settings: Settings) -> Settings:
    """The suite's settings with no least withdrawal fee, so that the amounts in this
    module are the ones each test names. The minimum has tests of its own."""
    return settings.model_copy(update={"withdrawal_min_fee": {}})


@pytest.fixture
def charging(settings: Settings) -> Settings:
    """The test settings with a 1.5% withdrawal fee."""
    return settings.model_copy(update={"withdrawal_fee_bps": 150})


async def cancel(db: Database, user: User, withdrawal_id: uuid.UUID) -> payments.Withdrawal:
    async with db.transaction() as session:
        return await payments.cancel_withdrawal(session, acting_as(user), withdrawal_id)


# --- requesting ------------------------------------------------------------------------------


async def test_a_bank_withdrawal_moves_the_amount_and_the_fee_to_held(
    db: Database, charging: Settings, bank: SimBank, maria: User
) -> None:
    await deposit(db, maria, 500_00)
    beneficiary = await add_beneficiary(db, bank, maria)

    withdrawal = await withdraw(db, charging, maria, 100_00, beneficiary=beneficiary)

    assert (withdrawal.user_id, withdrawal.asset, withdrawal.amount) == (maria.id, "USD", 100_00)
    assert (withdrawal.fee, withdrawal.kind, withdrawal.status) == (1_50, "bank", "held")
    assert (withdrawal.beneficiary_id, withdrawal.to_address) == (beneficiary.id, None)
    assert (withdrawal.provider, withdrawal.provider_ref) == ("simbank", None)
    assert (await available(db, maria), await held(db, maria)) == (398_50, 101_50)
    (entry,) = await entries(db, "withdrawal", str(withdrawal.id))
    assert (entry["id"], entry["kind"]) == (withdrawal.hold_entry_id, "withdrawal_hold")
    assert entry["postings"] == [("user_available", "D", 101_50), ("user_held", "C", 101_50)]
    row = await withdrawal_row(db, withdrawal.id)
    assert (row["status"], row["submitted_at"], row["final_entry_id"]) == ("held", None, None)


async def test_a_request_enqueues_one_submission_and_is_audited(
    db: Database, settings: Settings, bank: SimBank, maria: User
) -> None:
    await deposit(db, maria, 500_00)
    beneficiary = await add_beneficiary(db, bank, maria)

    withdrawal = await withdraw(db, settings, maria, 100_00, beneficiary=beneficiary)

    (event,) = await rows(db, "SELECT topic, payload FROM outbox_events")
    assert (event["topic"], event["payload"]) == (
        "withdrawal.submit",
        {"withdrawal_id": str(withdrawal.id)},
    )
    (audited,) = await rows(db, "SELECT * FROM audit_events WHERE action = 'withdrawal.requested'")
    assert (audited["actor_type"], audited["actor_id"]) == ("user", str(maria.id))
    assert (audited["principal_id"], audited["resource_id"]) == (maria.id, str(withdrawal.id))


async def test_an_on_chain_withdrawal_holds_funds_for_an_address(
    db: Database, charging: Settings, maria: User
) -> None:
    await deposit(db, maria, 50_000_000, "USDC")

    withdrawal = await withdraw(
        db, charging, maria, 25_000_000, asset="USDC", to_address=EXTERNAL_ADDRESS
    )

    assert (withdrawal.kind, withdrawal.provider) == ("chain", "simcustody")
    assert (withdrawal.beneficiary_id, withdrawal.to_address) == (None, EXTERNAL_ADDRESS)
    assert withdrawal.fee == 375_000
    assert await held(db, maria, "USDC") == 25_375_000
    assert await available(db, maria, "USDC") == 24_625_000


@pytest.mark.parametrize(("amount", "fee"), [(1, 0), (66, 0), (67, 1), (199_99, 2_99)])
async def test_the_fee_is_basis_points_of_the_amount_rounded_down(
    db: Database, charging: Settings, bank: SimBank, maria: User, amount: int, fee: int
) -> None:
    await deposit(db, maria, 500_00)
    beneficiary = await add_beneficiary(db, bank, maria)

    withdrawal = await withdraw(db, charging, maria, amount, beneficiary=beneficiary)

    assert withdrawal.fee == fee
    assert await held(db, maria) == amount + fee


async def test_the_whole_balance_can_be_withdrawn_when_there_is_no_fee(
    db: Database, settings: Settings, bank: SimBank, maria: User
) -> None:
    await deposit(db, maria, 100_00)
    beneficiary = await add_beneficiary(db, bank, maria)

    await withdraw(db, settings, maria, 100_00, beneficiary=beneficiary)

    assert (await available(db, maria), await held(db, maria)) == (0, 100_00)


@pytest.fixture
def with_a_minimum(charging: Settings) -> Settings:
    """A 1.5% withdrawal fee that is never less than 0.25 USD or 0.15 USDC."""
    return charging.model_copy(update={"withdrawal_min_fee": {"USD": "0.25", "USDC": "0.15"}})


@pytest.mark.parametrize(("amount", "fee"), [(1, 25), (16_66, 25), (17_34, 26), (100_00, 1_50)])
async def test_a_small_withdrawal_is_charged_the_minimum_fee_of_its_asset(
    db: Database, with_a_minimum: Settings, bank: SimBank, maria: User, amount: int, fee: int
) -> None:
    await deposit(db, maria, 500_00)
    beneficiary = await add_beneficiary(db, bank, maria)

    withdrawal = await withdraw(db, with_a_minimum, maria, amount, beneficiary=beneficiary)

    assert withdrawal.fee == fee
    assert (await available(db, maria), await held(db, maria)) == (
        500_00 - amount - fee,
        amount + fee,
    )
    assert (await withdrawal_row(db, withdrawal.id))["fee"] == fee


async def test_the_minimum_fee_is_in_the_asset_withdrawn(
    db: Database, with_a_minimum: Settings, maria: User
) -> None:
    await deposit(db, maria, 50_000_000, "USDC")

    withdrawal = await withdraw(
        db, with_a_minimum, maria, 1_000_000, asset="USDC", to_address=EXTERNAL_ADDRESS
    )

    assert withdrawal.fee == 150_000
    assert await held(db, maria, "USDC") == 1_150_000


async def test_a_balance_that_covers_the_amount_and_not_the_minimum_fee_holds_nothing(
    db: Database, with_a_minimum: Settings, bank: SimBank, maria: User
) -> None:
    await deposit(db, maria, 10_24)
    beneficiary = await add_beneficiary(db, bank, maria)

    with pytest.raises(InsufficientFunds):
        await withdraw(db, with_a_minimum, maria, 10_00, beneficiary=beneficiary)

    assert await available(db, maria) == 10_24
    assert await held(db, maria) == 0
    assert await count(db, "withdrawals") == 0


async def nothing_is_held(db: Database, user: User, asset: str = "USD") -> None:
    assert await held(db, user, asset) == 0
    assert await count(db, "withdrawals") == 0
    assert await count(db, "outbox_events") == 0


async def test_a_balance_that_does_not_cover_the_amount_and_the_fee_holds_nothing(
    db: Database, charging: Settings, bank: SimBank, maria: User
) -> None:
    await deposit(db, maria, 100_00)
    beneficiary = await add_beneficiary(db, bank, maria)

    with pytest.raises(InsufficientFunds):
        await withdraw(db, charging, maria, 100_00, beneficiary=beneficiary)

    assert await available(db, maria) == 100_00
    await nothing_is_held(db, maria)


async def test_an_address_that_is_not_valid_holds_nothing(
    db: Database, settings: Settings, maria: User
) -> None:
    await deposit(db, maria, 50_000_000, "USDC")

    for address in ("", "sim1nonsense", EXTERNAL_ADDRESS[:-1] + "0", EXTERNAL_ADDRESS.upper()):
        with pytest.raises(InvalidAddress) as refusal:
            await withdraw(db, settings, maria, 1_000_000, asset="USDC", to_address=address)
        assert (refusal.value.status, refusal.value.code) == (422, "invalid_address")

    await nothing_is_held(db, maria, "USDC")


async def test_an_invalid_address_is_refused_before_any_lock_is_taken(
    db: Database, settings: Settings, maria: User
) -> None:
    async with db.transaction() as holder:
        await advisory_xact_lock(holder, [lock_key("money_out", maria.id)])
        async with db.transaction() as session:
            await session.execute(text("SET LOCAL lock_timeout = '100ms'"))
            with pytest.raises(InvalidAddress):
                await payments.request_withdrawal(
                    session,
                    acting_as(maria),
                    withdrawal_id=new_id(),
                    asset="USDC",
                    amount=1_000_000,
                    to_address="sim1nonsense",
                    settings=settings,
                )


async def test_another_users_beneficiary_cannot_be_used_and_looks_like_none(
    db: Database, settings: Settings, bank: SimBank, maria: User, joao: User
) -> None:
    await deposit(db, maria, 100_00)
    his = await add_beneficiary(db, bank, joao)

    with pytest.raises(BeneficiaryNotFound) as other:
        await withdraw(db, settings, maria, 10_00, beneficiary=his)
    with pytest.raises(BeneficiaryNotFound) as missing:
        async with db.transaction() as session:
            await payments.request_withdrawal(
                session,
                acting_as(maria),
                withdrawal_id=new_id(),
                asset="USD",
                amount=10_00,
                beneficiary_id=new_id(),
                settings=settings,
            )

    assert (other.value.status, other.value.code) == (404, "beneficiary_not_found")
    assert other.value.detail == missing.value.detail
    assert await available(db, maria) == 100_00
    await nothing_is_held(db, maria)


async def test_a_beneficiary_in_another_asset_holds_nothing(
    db: Database, settings: Settings, bank: SimBank, maria: User
) -> None:
    await deposit(db, maria, 100_00)
    pesos = await add_beneficiary(
        db, bank, maria, asset="MXN", account_number=CLABE, routing_number=None
    )

    with pytest.raises(BeneficiaryAssetMismatch) as refusal:
        await withdraw(db, settings, maria, 10_00, beneficiary=pesos)

    assert (refusal.value.status, refusal.value.code) == (422, "beneficiary_asset_mismatch")
    await nothing_is_held(db, maria)


async def test_a_target_of_the_wrong_kind_for_the_asset_holds_nothing(
    db: Database, settings: Settings, bank: SimBank, maria: User
) -> None:
    await deposit(db, maria, 100_00)
    await deposit(db, maria, 50_000_000, "USDC")
    beneficiary = await add_beneficiary(db, bank, maria)

    for asset, target in (
        ("USD", {}),
        ("USD", {"to_address": EXTERNAL_ADDRESS}),
        ("USD", {"beneficiary": beneficiary, "to_address": EXTERNAL_ADDRESS}),
        ("USDC", {}),
        ("USDC", {"beneficiary": beneficiary}),
        ("USDC", {"beneficiary": beneficiary, "to_address": EXTERNAL_ADDRESS}),
    ):
        with pytest.raises(InvalidWithdrawalTarget) as refusal:
            await withdraw(db, settings, maria, 10_00, asset=asset, **target)  # type: ignore[arg-type]
        assert (refusal.value.status, refusal.value.code) == (422, "invalid_withdrawal_target")

    await nothing_is_held(db, maria)
    await nothing_is_held(db, maria, "USDC")


@pytest.mark.parametrize("amount", [0, -1, True, 10**38])
async def test_an_amount_that_is_not_a_positive_storable_integer_is_refused(
    db: Database, charging: Settings, bank: SimBank, maria: User, amount: int
) -> None:
    await deposit(db, maria, 100_00)
    beneficiary = await add_beneficiary(db, bank, maria)

    with pytest.raises(InvalidAmount):
        await withdraw(db, charging, maria, amount, beneficiary=beneficiary)

    await nothing_is_held(db, maria)


async def test_an_unknown_asset_is_refused(db: Database, settings: Settings, maria: User) -> None:
    with pytest.raises(UnknownAsset):
        await withdraw(db, settings, maria, 10_00, asset="EUR", to_address=EXTERNAL_ADDRESS)


async def test_a_restricted_user_cannot_withdraw(
    db: Database, settings: Settings, bank: SimBank, maria: User
) -> None:
    await deposit(db, maria, 100_00)
    beneficiary = await add_beneficiary(db, bank, maria)
    async with db.transaction() as session:
        await identity.restrict_user(session, maria.id, "under review")

    with pytest.raises(UserRestricted):
        await withdraw(db, settings, maria, 10_00, beneficiary=beneficiary)

    assert await available(db, maria) == 100_00
    await nothing_is_held(db, maria)


async def test_requesting_needs_the_withdrawals_create_scope(
    db: Database, settings: Settings, bank: SimBank, maria: User
) -> None:
    await deposit(db, maria, 100_00)
    beneficiary = await add_beneficiary(db, bank, maria)

    with pytest.raises(InsufficientScope):
        await withdraw(
            db,
            settings,
            maria,
            10_00,
            beneficiary=beneficiary,
            principal=agent_of(maria, Scope.WITHDRAWALS_READ, Scope.TRANSFERS_CREATE),
        )
    by_agent = await withdraw(
        db,
        settings,
        maria,
        10_00,
        beneficiary=beneficiary,
        principal=agent_of(maria, Scope.WITHDRAWALS_CREATE),
    )

    assert by_agent.status == "held"
    (audited,) = await rows(db, "SELECT * FROM audit_events WHERE action = 'withdrawal.requested'")
    assert (audited["actor_type"], audited["principal_id"]) == ("agent", maria.id)


async def test_a_withdrawal_id_used_twice_is_a_bug_and_holds_once(
    db: Database, settings: Settings, bank: SimBank, maria: User
) -> None:
    await deposit(db, maria, 100_00)
    beneficiary = await add_beneficiary(db, bank, maria)
    first = await withdraw(db, settings, maria, 10_00, beneficiary=beneficiary)

    with pytest.raises(DuplicateWithdrawal):
        await withdraw(db, settings, maria, 10_00, beneficiary=beneficiary, withdrawal_id=first.id)

    assert await held(db, maria) == 10_00
    assert await count(db, "withdrawals") == 1


async def test_a_request_waits_for_the_users_money_out_lock(
    db: Database, settings: Settings, bank: SimBank, maria: User
) -> None:
    await deposit(db, maria, 100_00)
    beneficiary = await add_beneficiary(db, bank, maria)

    async with db.transaction() as holder:
        await advisory_xact_lock(holder, [lock_key("money_out", maria.id)])
        with pytest.raises(DBAPIError) as failure:
            async with db.transaction() as blocked:
                await blocked.execute(text("SET LOCAL lock_timeout = '100ms'"))
                await payments.request_withdrawal(
                    blocked,
                    acting_as(maria),
                    withdrawal_id=new_id(),
                    asset="USD",
                    amount=1_00,
                    beneficiary_id=beneficiary.id,
                    settings=settings,
                )

    assert sqlstate_of(failure.value) == LOCK_NOT_AVAILABLE
    assert await held(db, maria) == 0


async def test_fifty_requests_from_a_balance_that_affords_twenty_hold_exactly_twenty(
    db: Database, settings: Settings, bank: SimBank, maria: User
) -> None:
    await deposit(db, maria, 20_00)
    beneficiary = await add_beneficiary(db, bank, maria)

    async def attempt() -> bool:
        try:
            await withdraw(db, settings, maria, 1_00, beneficiary=beneficiary)
        except InsufficientFunds:
            return False
        return True

    done = await asyncio.gather(*(attempt() for _ in range(50)))

    assert done.count(True) == 20
    assert (await available(db, maria), await held(db, maria)) == (0, 20_00)
    assert await count(db, "withdrawals") == 20
    assert await count(db, "outbox_events") == 20


# --- cancelling ------------------------------------------------------------------------------


async def test_cancelling_a_held_withdrawal_releases_the_amount_and_the_fee(
    db: Database, charging: Settings, bank: SimBank, maria: User
) -> None:
    await deposit(db, maria, 500_00)
    beneficiary = await add_beneficiary(db, bank, maria)
    withdrawal = await withdraw(db, charging, maria, 100_00, beneficiary=beneficiary)

    canceled = await cancel(db, maria, withdrawal.id)

    assert canceled.status == "canceled"
    assert (await available(db, maria), await held(db, maria)) == (500_00, 0)
    _hold, release = await entries(db, "withdrawal", str(withdrawal.id))
    assert (release["id"], release["kind"]) == (canceled.final_entry_id, "withdrawal_release")
    assert release["postings"] == [("user_held", "D", 101_50), ("user_available", "C", 101_50)]
    (audited,) = await rows(db, "SELECT * FROM audit_events WHERE action = 'withdrawal.canceled'")
    assert audited["resource_id"] == str(withdrawal.id)


async def test_a_withdrawal_is_cancelled_once_and_then_it_is_a_conflict(
    db: Database, settings: Settings, bank: SimBank, maria: User
) -> None:
    await deposit(db, maria, 500_00)
    beneficiary = await add_beneficiary(db, bank, maria)
    withdrawal = await withdraw(db, settings, maria, 100_00, beneficiary=beneficiary)
    await cancel(db, maria, withdrawal.id)

    with pytest.raises(WithdrawalNotCancelable) as refusal:
        await cancel(db, maria, withdrawal.id)

    assert (refusal.value.status, refusal.value.code) == (409, "withdrawal_not_cancelable")
    assert (await available(db, maria), await held(db, maria)) == (500_00, 0)
    assert await count(db, "journal_entries") == 3


async def test_twenty_cancellations_at_once_release_the_funds_once(
    db: Database, settings: Settings, bank: SimBank, maria: User
) -> None:
    await deposit(db, maria, 500_00)
    beneficiary = await add_beneficiary(db, bank, maria)
    withdrawal = await withdraw(db, settings, maria, 100_00, beneficiary=beneficiary)

    async def attempt() -> bool:
        try:
            await cancel(db, maria, withdrawal.id)
        except WithdrawalNotCancelable:
            return False
        return True

    done = await asyncio.gather(*(attempt() for _ in range(20)))

    assert done.count(True) == 1
    assert (await available(db, maria), await held(db, maria)) == (500_00, 0)


@pytest.mark.parametrize(
    "status",
    ["under_review", "submitting", "submitted", "completed", "failed", "canceled", "released"],
)
async def test_only_a_held_withdrawal_can_be_cancelled(
    db: Database, settings: Settings, bank: SimBank, maria: User, status: str
) -> None:
    await deposit(db, maria, 500_00)
    beneficiary = await add_beneficiary(db, bank, maria)
    withdrawal = await withdraw(db, settings, maria, 100_00, beneficiary=beneficiary)
    async with db.transaction() as session:
        await session.execute(
            text("UPDATE withdrawals SET status = :status WHERE id = :id"),
            {"status": status, "id": withdrawal.id},
        )

    with pytest.raises(WithdrawalNotCancelable):
        await cancel(db, maria, withdrawal.id)

    assert (await available(db, maria), await held(db, maria)) == (400_00, 100_00)
    assert (await withdrawal_row(db, withdrawal.id))["status"] == status


async def test_nobody_else_can_cancel_a_withdrawal_or_learn_that_it_exists(
    db: Database, settings: Settings, bank: SimBank, maria: User, joao: User
) -> None:
    await deposit(db, maria, 500_00)
    beneficiary = await add_beneficiary(db, bank, maria)
    withdrawal = await withdraw(db, settings, maria, 100_00, beneficiary=beneficiary)

    with pytest.raises(WithdrawalNotFound) as other:
        await cancel(db, joao, withdrawal.id)
    with pytest.raises(WithdrawalNotFound) as missing:
        await cancel(db, maria, new_id())
    with pytest.raises(InsufficientScope):
        async with db.transaction() as session:
            await payments.cancel_withdrawal(
                session, agent_of(maria, Scope.WITHDRAWALS_READ), withdrawal.id
            )

    assert (other.value.status, other.value.code) == (404, "withdrawal_not_found")
    assert other.value.detail == missing.value.detail
    assert await held(db, maria) == 100_00


# --- reading ---------------------------------------------------------------------------------


async def test_a_user_reads_their_own_withdrawals_and_nobody_elses(
    db: Database, settings: Settings, bank: SimBank, maria: User, joao: User
) -> None:
    await deposit(db, maria, 500_00)
    beneficiary = await add_beneficiary(db, bank, maria)
    made = [
        await withdraw(db, settings, maria, amount, beneficiary=beneficiary) for amount in (1, 2, 3)
    ]

    async with db.transaction() as session:
        one = await payments.get_withdrawal(session, acting_as(maria), made[0].id)
        with pytest.raises(WithdrawalNotFound):
            await payments.get_withdrawal(session, acting_as(joao), made[0].id)
        first = await payments.list_withdrawals(session, acting_as(maria), limit=2)
        rest = await payments.list_withdrawals(
            session, acting_as(maria), limit=2, cursor=first.next_cursor
        )
        his = await payments.list_withdrawals(session, acting_as(joao))
        with pytest.raises(InsufficientScope):
            await payments.list_withdrawals(session, agent_of(maria, Scope.WITHDRAWALS_CREATE))
        with pytest.raises(InsufficientScope):
            await payments.get_withdrawal(
                session, agent_of(maria, Scope.WITHDRAWALS_CREATE), made[0].id
            )

    assert one == made[0]
    assert [withdrawal.amount for withdrawal in first.items] == [3, 2]
    assert [withdrawal.amount for withdrawal in rest.items] == [1]
    assert (rest.next_cursor, his.items) == (None, ())
