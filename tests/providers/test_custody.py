"""The custodian adapter against the real simulator."""

from datetime import UTC, datetime, timedelta

import pytest

from corridor.providers import Custodian, ProviderRejected, SimCustody, Withdrawal, is_valid_address
from corridor_sim.custody import address_for
from tests.providers.conftest import CUSTOMER, EXTERNAL_ADDRESS, REFERENCE, START, Sim

DAY_START = datetime(2026, 1, 15, tzinfo=UTC)
DAY_END = DAY_START + timedelta(days=1)


async def withdraw(
    custody: SimCustody, to_address: str = EXTERNAL_ADDRESS, *, amount: int = 25_000_000
) -> Withdrawal:
    return await custody.create_withdrawal(
        asset_code="USDC",
        amount=amount,
        to_address=to_address,
        reference=REFERENCE,
        idempotency_key=REFERENCE,
    )


def test_the_adapter_is_a_custodian_named_simcustody(custody: SimCustody) -> None:
    port: Custodian = custody
    assert port.name == "simcustody"


async def test_a_deposit_address_is_issued_for_a_customer(custody: SimCustody) -> None:
    address = await custody.create_address(
        customer_reference=CUSTOMER, asset_code="USDC", idempotency_key="addr-1"
    )

    assert address.id.startswith("addr_")
    assert (address.customer_reference, address.asset_code) == (CUSTOMER, "USDC")
    assert address.network == "simchain"
    assert is_valid_address(address.address)


async def test_asking_again_returns_the_same_address(custody: SimCustody) -> None:
    first = await custody.create_address(
        customer_reference=CUSTOMER, asset_code="USDC", idempotency_key="addr-1"
    )
    second = await custody.create_address(
        customer_reference=CUSTOMER, asset_code="USDC", idempotency_key="addr-2"
    )

    assert second == first


async def test_a_withdrawal_is_sent_in_major_units_and_read_back_in_minor_units(
    custody: SimCustody, sim: Sim
) -> None:
    withdrawal = await withdraw(custody)

    assert withdrawal.id.startswith("wd_")
    assert (withdrawal.status, withdrawal.asset_code) == ("pending", "USDC")
    assert (withdrawal.amount, withdrawal.network_fee) == (25_000_000, 150_000)
    assert (withdrawal.to_address, withdrawal.reference) == (EXTERNAL_ADDRESS, REFERENCE)
    assert (withdrawal.tx_hash, withdrawal.confirmations) == (None, 0)
    assert withdrawal.created_at == START
    assert (withdrawal.completed_at, withdrawal.failure_reason) == (None, None)
    [held] = await sim.withdrawals()
    assert (held["amount"], held["network_fee"]) == ("25.000000", "0.150000")


async def test_a_withdrawal_of_one_millionth_keeps_all_six_places(
    custody: SimCustody, sim: Sim
) -> None:
    withdrawal = await withdraw(custody, amount=1)

    assert withdrawal.amount == 1
    assert (await sim.withdrawals())[0]["amount"] == "0.000001"


async def test_a_confirmed_withdrawal_is_read_back_completed(custody: SimCustody, sim: Sim) -> None:
    created = await withdraw(custody)
    await sim.mine(4)

    withdrawal = await custody.get_withdrawal(created.id)

    assert (withdrawal.id, withdrawal.status) == (created.id, "completed")
    assert withdrawal.tx_hash is not None
    assert withdrawal.confirmations >= 3
    assert withdrawal.completed_at is not None


async def test_a_withdrawal_the_network_rejects_is_read_back_failed(
    custody: SimCustody, sim: Sim
) -> None:
    created = await withdraw(custody, address_for("dead".ljust(32, "a")))
    await sim.mine(1)

    withdrawal = await custody.get_withdrawal(created.id)

    assert (withdrawal.status, withdrawal.failure_reason) == ("failed", "rejected_by_network")


async def test_withdrawals_are_found_by_reference(custody: SimCustody) -> None:
    created = await withdraw(custody)

    assert await custody.find_withdrawals(REFERENCE) == (created,)
    assert await custody.find_withdrawals("no-such-reference") == ()


async def test_a_repeated_withdrawal_with_the_same_key_returns_the_same_withdrawal(
    custody: SimCustody, sim: Sim
) -> None:
    first = await withdraw(custody)
    second = await withdraw(custody)

    assert second == first
    assert len(await sim.withdrawals()) == 1


async def test_a_statement_lists_final_movements_with_their_transaction_hashes(
    custody: SimCustody, sim: Sim
) -> None:
    address = await custody.create_address(
        customer_reference=CUSTOMER, asset_code="USDC", idempotency_key="addr-1"
    )
    await sim.control(
        "POST",
        "/custody/deposits",
        {"address": address.address, "amount": "40.500000", "from_address": EXTERNAL_ADDRESS},
        expect=201,
    )
    created = await withdraw(custody)
    await sim.mine(4)

    statement = await custody.list_transactions(asset_code="USDC", start=DAY_START, end=DAY_END)

    assert sorted(
        (line.type, line.direction, line.amount, line.reference) for line in statement.transactions
    ) == [
        ("deposit", "credit", 40_500_000, None),
        ("network_fee", "debit", 150_000, REFERENCE),
        ("withdrawal", "debit", 25_000_000, REFERENCE),
    ]
    assert all(line.tx_hash for line in statement.transactions)
    assert created.id in {line.id for line in statement.transactions}
    assert statement.closing_balance == 40_500_000 - 25_000_000 - 150_000


# --- refusals --------------------------------------------------------------------------------


async def test_a_withdrawal_to_a_malformed_address_is_rejected(
    custody: SimCustody, sim: Sim
) -> None:
    with pytest.raises(ProviderRejected) as refused:
        await withdraw(custody, EXTERNAL_ADDRESS[:-1] + "x")

    assert (refused.value.status, refused.value.code) == (422, "invalid_address")
    assert (refused.value.provider, refused.value.operation) == ("simcustody", "create_withdrawal")
    assert await sim.withdrawals() == []


async def test_a_withdrawal_in_an_asset_the_custodian_does_not_carry_is_rejected(
    custody: SimCustody,
) -> None:
    with pytest.raises(ProviderRejected) as refused:
        await custody.create_withdrawal(
            asset_code="USD",
            amount=25_00,
            to_address=EXTERNAL_ADDRESS,
            reference=REFERENCE,
            idempotency_key=REFERENCE,
        )

    assert (refused.value.status, refused.value.code) == (422, "unsupported_asset")


async def test_an_address_in_an_asset_the_custodian_does_not_carry_is_rejected(
    custody: SimCustody,
) -> None:
    with pytest.raises(ProviderRejected) as refused:
        await custody.create_address(
            customer_reference=CUSTOMER, asset_code="USD", idempotency_key="addr-usd"
        )

    assert (refused.value.status, refused.value.code) == (422, "unsupported_asset")


async def test_an_unknown_withdrawal_is_rejected_as_not_found(custody: SimCustody) -> None:
    with pytest.raises(ProviderRejected) as refused:
        await custody.get_withdrawal("wd_missing")

    assert (refused.value.status, refused.value.code) == (404, "withdrawal_not_found")


@pytest.mark.parametrize("amount", [0, -1])
async def test_a_withdrawal_of_nothing_is_never_sent(
    custody: SimCustody, sim: Sim, amount: int
) -> None:
    with pytest.raises(ValueError, match="positive"):
        await withdraw(custody, amount=amount)

    assert sim.recorder.requests == []
