"""The bank rail adapter against the real simulator."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr

from corridor.platform.config import Settings
from corridor.providers import (
    BankRail,
    Payout,
    ProviderMisconfigured,
    ProviderRejected,
    SimBank,
)
from tests.providers.conftest import (
    ACCOUNT_NUMBER,
    CLABE,
    CUSTOMER,
    REFERENCE,
    ROUTING_NUMBER,
    START,
    WRONG_API_KEY,
    Sim,
)

DAY_START = datetime(2026, 1, 15, tzinfo=UTC)
DAY_END = DAY_START + timedelta(days=1)


async def usd_beneficiary(bank: SimBank, account_number: str = ACCOUNT_NUMBER) -> str:
    beneficiary = await bank.create_beneficiary(
        customer_reference=CUSTOMER,
        asset_code="USD",
        holder_name="Maria Silva",
        account_number=account_number,
        routing_number=ROUTING_NUMBER,
        idempotency_key=f"ben-{account_number}",
    )
    return beneficiary.id


async def usd_payout(bank: SimBank, beneficiary_id: str, *, key: str = REFERENCE) -> Payout:
    return await bank.create_payout(
        beneficiary_id=beneficiary_id,
        asset_code="USD",
        amount=100_00,
        reference=REFERENCE,
        idempotency_key=key,
    )


def test_the_adapter_is_a_bank_rail_named_simbank(bank: SimBank) -> None:
    port: BankRail = bank
    assert port.name == "simbank"


async def test_a_virtual_account_comes_back_with_its_deposit_details(bank: SimBank) -> None:
    account = await bank.create_virtual_account(
        customer_reference=CUSTOMER, asset_code="USD", idempotency_key="va-1"
    )

    assert account.id.startswith("va_")
    assert (account.customer_reference, account.asset_code) == (CUSTOMER, "USD")
    assert (account.rail, account.bank_name) == ("ach", "Sim Bank")
    assert account.account_number.isdigit()
    assert account.routing_number is not None


async def test_only_a_usd_virtual_account_has_a_routing_number(bank: SimBank) -> None:
    account = await bank.create_virtual_account(
        customer_reference=CUSTOMER, asset_code="MXN", idempotency_key="va-mxn"
    )

    assert (account.rail, account.routing_number) == ("spei", None)


async def test_asking_again_returns_the_same_virtual_account(bank: SimBank) -> None:
    first = await bank.create_virtual_account(
        customer_reference=CUSTOMER, asset_code="USD", idempotency_key="va-1"
    )
    second = await bank.create_virtual_account(
        customer_reference=CUSTOMER, asset_code="USD", idempotency_key="va-2"
    )

    assert second == first


async def test_a_virtual_account_does_not_show_its_numbers_when_printed(bank: SimBank) -> None:
    account = await bank.create_virtual_account(
        customer_reference=CUSTOMER, asset_code="USD", idempotency_key="va-1"
    )

    assert account.account_number not in repr(account)
    assert str(account.routing_number) not in repr(account)


async def test_a_beneficiary_comes_back_as_a_token_and_a_mask(bank: SimBank) -> None:
    beneficiary = await bank.create_beneficiary(
        customer_reference=CUSTOMER,
        asset_code="USD",
        holder_name="Maria Silva",
        account_number=ACCOUNT_NUMBER,
        routing_number=ROUTING_NUMBER,
        idempotency_key="ben-1",
    )

    assert beneficiary.id.startswith("ben_")
    assert (beneficiary.asset_code, beneficiary.rail) == ("USD", "ach")
    assert beneficiary.holder_name == "Maria Silva"
    assert beneficiary.account_mask.endswith(ACCOUNT_NUMBER[-4:])
    assert ACCOUNT_NUMBER not in beneficiary.account_mask


async def test_a_payout_is_sent_in_major_units_and_read_back_in_minor_units(
    bank: SimBank, sim: Sim
) -> None:
    beneficiary_id = await usd_beneficiary(bank)

    payout = await usd_payout(bank, beneficiary_id)

    assert payout.id.startswith("po_")
    assert (payout.status, payout.beneficiary_id) == ("pending", beneficiary_id)
    assert (payout.asset_code, payout.amount, payout.fee) == ("USD", 100_00, 25)
    assert payout.reference == REFERENCE
    assert payout.created_at == START
    assert (payout.settled_at, payout.failure_reason) == (None, None)
    [held] = await sim.payouts()
    assert (held["amount"], held["fee"]) == ("100.00", "0.25")


async def test_a_payout_of_one_cent_keeps_its_cent(bank: SimBank, sim: Sim) -> None:
    beneficiary_id = await usd_beneficiary(bank)

    payout = await bank.create_payout(
        beneficiary_id=beneficiary_id,
        asset_code="USD",
        amount=1,
        reference=REFERENCE,
        idempotency_key=REFERENCE,
    )

    assert payout.amount == 1
    assert (await sim.payouts())[0]["amount"] == "0.01"


async def test_a_payout_in_pesos_carries_the_spei_fee(bank: SimBank) -> None:
    beneficiary = await bank.create_beneficiary(
        customer_reference=CUSTOMER,
        asset_code="MXN",
        holder_name="Maria Silva",
        account_number=CLABE,
        idempotency_key="ben-mxn",
    )

    payout = await bank.create_payout(
        beneficiary_id=beneficiary.id,
        asset_code="MXN",
        amount=1_234_56,
        reference=REFERENCE,
        idempotency_key=REFERENCE,
    )

    assert (payout.asset_code, payout.amount, payout.fee) == ("MXN", 1_234_56, 5_00)


async def test_a_settled_payout_is_read_back_completed(bank: SimBank, sim: Sim) -> None:
    created = await usd_payout(bank, await usd_beneficiary(bank))
    await sim.advance(30)

    payout = await bank.get_payout(created.id)

    assert (payout.id, payout.status) == (created.id, "completed")
    assert payout.settled_at == START + timedelta(seconds=30)
    assert payout.settled_at.utcoffset() == timedelta(0)


async def test_a_payout_to_a_closed_account_is_read_back_failed(bank: SimBank, sim: Sim) -> None:
    created = await usd_payout(bank, await usd_beneficiary(bank, "000123450000"))
    await sim.advance(30)

    payout = await bank.get_payout(created.id)

    assert (payout.status, payout.failure_reason) == ("failed", "account_closed")


async def test_payouts_are_found_by_reference(bank: SimBank) -> None:
    created = await usd_payout(bank, await usd_beneficiary(bank))

    assert await bank.find_payouts(REFERENCE) == (created,)
    assert await bank.find_payouts("no-such-reference") == ()


async def test_a_repeated_payout_with_the_same_key_returns_the_same_payout(
    bank: SimBank, sim: Sim
) -> None:
    beneficiary_id = await usd_beneficiary(bank)

    first = await usd_payout(bank, beneficiary_id)
    second = await usd_payout(bank, beneficiary_id)

    assert second == first
    assert len(await sim.payouts()) == 1


async def test_a_statement_lists_settled_movements_in_minor_units(bank: SimBank, sim: Sim) -> None:
    account = await bank.create_virtual_account(
        customer_reference=CUSTOMER, asset_code="USD", idempotency_key="va-1"
    )
    await sim.control(
        "POST",
        "/bank/deposits",
        {
            "virtual_account_id": account.id,
            "amount": "250.00",
            "sender_name": "Joao Souza",
            "reference": "INV-2041",
        },
        expect=201,
    )
    payout = await usd_payout(bank, await usd_beneficiary(bank))
    await sim.advance(30)

    statement = await bank.list_transactions(asset_code="USD", start=DAY_START, end=DAY_END)

    assert (statement.asset_code, statement.start, statement.end) == ("USD", DAY_START, DAY_END)
    assert [
        (line.type, line.direction, line.amount, line.reference, line.related_id)
        for line in statement.transactions
    ] == [
        ("deposit", "credit", 250_00, "INV-2041", account.id),
        ("payout", "debit", 100_00, REFERENCE, payout.beneficiary_id),
        ("payout_fee", "debit", 25, REFERENCE, payout.id),
    ]
    assert statement.transactions[1].id == payout.id
    assert statement.transactions[1].occurred_at == START + timedelta(seconds=30)
    assert all(line.asset_code == "USD" for line in statement.transactions)
    assert statement.closing_balance == 149_75


async def test_an_overdrawn_statement_closes_below_zero(bank: SimBank, sim: Sim) -> None:
    await usd_payout(bank, await usd_beneficiary(bank))
    await sim.advance(30)

    statement = await bank.list_transactions(asset_code="USD", start=DAY_START, end=DAY_END)

    assert statement.closing_balance == -100_25


async def test_a_statement_window_must_name_instants(bank: SimBank) -> None:
    with pytest.raises(ValueError, match="timezone"):
        await bank.list_transactions(
            asset_code="USD",
            start=DAY_START.replace(tzinfo=None),
            end=DAY_END,
        )


@pytest.mark.parametrize("amount", [0, -1])
async def test_a_payout_of_nothing_is_never_sent(bank: SimBank, sim: Sim, amount: int) -> None:
    beneficiary_id = await usd_beneficiary(bank)
    sim.recorder.requests.clear()

    with pytest.raises(ValueError, match="positive"):
        await bank.create_payout(
            beneficiary_id=beneficiary_id,
            asset_code="USD",
            amount=amount,
            reference=REFERENCE,
            idempotency_key=REFERENCE,
        )

    assert sim.recorder.requests == []


# --- refusals --------------------------------------------------------------------------------


async def test_a_payout_to_an_unknown_beneficiary_is_rejected(bank: SimBank, sim: Sim) -> None:
    with pytest.raises(ProviderRejected) as refused:
        await usd_payout(bank, "ben_missing")

    assert (refused.value.status, refused.value.code) == (404, "beneficiary_not_found")
    assert (refused.value.provider, refused.value.operation) == ("simbank", "create_payout")
    assert refused.value.message
    assert await sim.payouts() == []


async def test_a_payout_in_another_asset_than_the_beneficiarys_is_rejected(bank: SimBank) -> None:
    beneficiary_id = await usd_beneficiary(bank)

    with pytest.raises(ProviderRejected) as refused:
        await bank.create_payout(
            beneficiary_id=beneficiary_id,
            asset_code="MXN",
            amount=100_00,
            reference=REFERENCE,
            idempotency_key=REFERENCE,
        )

    assert (refused.value.status, refused.value.code) == (422, "asset_mismatch")


async def test_a_beneficiary_with_a_malformed_account_is_rejected(bank: SimBank) -> None:
    with pytest.raises(ProviderRejected) as refused:
        await usd_beneficiary(bank, "12")

    assert (refused.value.status, refused.value.code) == (422, "invalid_account")


async def test_a_virtual_account_in_an_asset_the_bank_does_not_move_is_rejected(
    bank: SimBank,
) -> None:
    with pytest.raises(ProviderRejected) as refused:
        await bank.create_virtual_account(
            customer_reference=CUSTOMER, asset_code="USDC", idempotency_key="va-usdc"
        )

    assert (refused.value.status, refused.value.code) == (422, "unsupported_asset")


async def test_an_unknown_payout_is_rejected_as_not_found(bank: SimBank) -> None:
    with pytest.raises(ProviderRejected) as refused:
        await bank.get_payout("po_missing")

    assert (refused.value.status, refused.value.code) == (404, "payout_not_found")


async def test_a_key_reused_for_a_different_payout_is_rejected(bank: SimBank, sim: Sim) -> None:
    beneficiary_id = await usd_beneficiary(bank)
    await usd_payout(bank, beneficiary_id)

    with pytest.raises(ProviderRejected) as refused:
        await bank.create_payout(
            beneficiary_id=beneficiary_id,
            asset_code="USD",
            amount=100_01,
            reference=REFERENCE,
            idempotency_key=REFERENCE,
        )

    assert (refused.value.status, refused.value.code) == (409, "idempotency_conflict")
    assert len(await sim.payouts()) == 1


async def test_a_payout_id_cannot_reach_another_path(bank: SimBank, sim: Sim) -> None:
    with pytest.raises(ProviderRejected):
        await bank.get_payout("../beneficiaries")

    assert sim.recorder.requests[-1].url.raw_path == b"/bank/v1/payouts/..%2Fbeneficiaries"


# --- credentials and configuration -----------------------------------------------------------


async def test_a_wrong_api_key_is_a_misconfiguration_not_a_refusal(
    sim: Sim, provider_settings: Settings
) -> None:
    wrong = provider_settings.model_copy(update={"bank_rail_api_key": SecretStr(WRONG_API_KEY)})
    bank = SimBank(wrong, client=sim.http)

    with pytest.raises(ProviderMisconfigured):
        await bank.create_virtual_account(
            customer_reference=CUSTOMER, asset_code="USD", idempotency_key="va-1"
        )


@pytest.mark.parametrize("missing", ["bank_rail_url", "bank_rail_api_key"])
def test_an_adapter_without_its_address_or_key_cannot_be_built(
    provider_settings: Settings, missing: str
) -> None:
    with pytest.raises(ProviderMisconfigured):
        SimBank(provider_settings.model_copy(update={missing: None}))
