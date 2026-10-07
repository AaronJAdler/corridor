"""Beneficiaries: a user's external bank accounts, kept as the provider's token and a mask."""

import asyncio
import json
from typing import Any

import pytest

from corridor import payments
from corridor.identity import InsufficientScope, Scope, User
from corridor.payments import (
    BeneficiaryKeyReused,
    BeneficiaryRejected,
    InvalidBeneficiaryAccount,
    ProviderUnavailable,
)
from corridor.platform.db import Database
from corridor.platform.money import UnknownAsset
from corridor.providers import Beneficiary as ProviderBeneficiary
from corridor.providers import SimBank
from tests.payments.support import acting_as, add_beneficiary, agent_of, count, rows
from tests.support.providers import ACCOUNT_NUMBER, CLABE, ROUTING_NUMBER, Sim

BENEFICIARIES = "/bank/v1/beneficiaries"


async def test_a_beneficiary_is_registered_with_the_bank_and_kept_as_a_token_and_a_mask(
    db: Database, sim: Sim, bank: SimBank, maria: User
) -> None:
    beneficiary = await add_beneficiary(db, bank, maria, key="key-1")

    assert (beneficiary.user_id, beneficiary.asset) == (maria.id, "USD")
    assert beneficiary.provider == "simbank"
    assert beneficiary.provider_ref.startswith("ben_")
    assert (beneficiary.holder_name, beneficiary.account_mask) == ("Maria Silva", "••••6789")
    (row,) = await rows(db, "SELECT * FROM beneficiaries")
    assert set(row) == {
        "id",
        "user_id",
        "asset_code",
        "provider",
        "provider_ref",
        "holder_name",
        "account_mask",
        "created_at",
    }
    assert (row["id"], row["provider_ref"]) == (beneficiary.id, beneficiary.provider_ref)
    (audited,) = await rows(db, "SELECT * FROM audit_events WHERE action = 'beneficiary.created'")
    assert (audited["principal_id"], audited["resource_id"]) == (maria.id, str(beneficiary.id))
    assert ACCOUNT_NUMBER not in json.dumps(audited["details"])


async def test_the_bank_is_sent_the_account_under_a_key_of_the_caller_and_their_key(
    db: Database, sim: Sim, bank: SimBank, maria: User
) -> None:
    await add_beneficiary(db, bank, maria, key="key-1")

    (request,) = sim.recorder.sent("POST", BENEFICIARIES)
    assert request.headers["Idempotency-Key"] == f"ben:{maria.id}:key-1"
    assert json.loads(request.content) == {
        "customer_reference": str(maria.id),
        "asset": "USD",
        "holder_name": "Maria Silva",
        "account_number": ACCOUNT_NUMBER,
        "routing_number": ROUTING_NUMBER,
    }


async def test_a_beneficiary_in_another_fiat_asset_uses_that_assets_rail(
    db: Database, bank: SimBank, maria: User
) -> None:
    beneficiary = await add_beneficiary(
        db, bank, maria, asset="MXN", account_number=CLABE, routing_number=None
    )

    assert (beneficiary.asset, beneficiary.account_mask) == ("MXN", "••••" + CLABE[-4:])


async def test_repeating_a_request_with_its_key_returns_the_same_beneficiary(
    db: Database, sim: Sim, bank: SimBank, maria: User
) -> None:
    first = await add_beneficiary(db, bank, maria, key="key-1")

    again = await add_beneficiary(db, bank, maria, key="key-1")
    several = await asyncio.gather(
        *(add_beneficiary(db, bank, maria, key="key-1") for _ in range(10))
    )

    assert again == first
    assert all(beneficiary == first for beneficiary in several)
    assert await count(db, "beneficiaries") == 1
    assert len(sim.app.state.sim.bank._beneficiaries) == 1
    assert (
        len(await rows(db, "SELECT 1 FROM audit_events WHERE action = 'beneficiary.created'")) == 1
    )


async def test_another_key_is_another_beneficiary(db: Database, bank: SimBank, maria: User) -> None:
    first = await add_beneficiary(db, bank, maria, key="key-1")
    second = await add_beneficiary(db, bank, maria, key="key-2")

    assert first.id != second.id
    assert await count(db, "beneficiaries") == 2


async def test_two_users_may_use_the_same_key_for_different_accounts(
    db: Database, bank: SimBank, maria: User, joao: User
) -> None:
    hers = await add_beneficiary(db, bank, maria, key="key-1")
    his = await add_beneficiary(db, bank, joao, key="key-1", account_number="000987654321")

    assert (hers.user_id, his.user_id) == (maria.id, joao.id)
    assert hers.provider_ref != his.provider_ref
    assert his.account_mask == "••••4321"


async def test_a_key_reused_for_another_account_is_refused(
    db: Database, bank: SimBank, maria: User
) -> None:
    await add_beneficiary(db, bank, maria, key="key-1")

    with pytest.raises(BeneficiaryKeyReused) as refusal:
        await add_beneficiary(db, bank, maria, key="key-1", account_number="000987654321")

    assert (refusal.value.status, refusal.value.code) == (422, "idempotency_key_reused")
    assert await count(db, "beneficiaries") == 1


async def test_an_account_the_bank_refuses_is_refused_and_nothing_is_stored(
    db: Database, bank: SimBank, maria: User
) -> None:
    with pytest.raises(InvalidBeneficiaryAccount) as refusal:
        await add_beneficiary(db, bank, maria, account_number="12")

    assert (refusal.value.status, refusal.value.code) == (422, "invalid_account")
    assert "12" not in str(refusal.value.detail)
    assert await count(db, "beneficiaries") == 0
    assert await count(db, "audit_events") == 0


async def test_a_stablecoin_has_no_beneficiaries(
    db: Database, sim: Sim, bank: SimBank, maria: User
) -> None:
    with pytest.raises(BeneficiaryRejected) as refusal:
        await add_beneficiary(db, bank, maria, asset="USDC")
    with pytest.raises(UnknownAsset):
        await add_beneficiary(db, bank, maria, asset="EUR")

    assert refusal.value.code == "unsupported_asset"
    assert sim.recorder.requests == []


@pytest.mark.parametrize("mode", ["error", "error_after_effect"])
async def test_a_bank_that_does_not_answer_is_unavailable_and_the_retry_makes_one_beneficiary(
    db: Database, sim: Sim, bank: SimBank, maria: User, mode: str
) -> None:
    await sim.inject("bank.create_beneficiary", mode)

    with pytest.raises(ProviderUnavailable):
        await add_beneficiary(db, bank, maria, key="key-1")
    assert await count(db, "beneficiaries") == 0

    beneficiary = await add_beneficiary(db, bank, maria, key="key-1")
    assert list(sim.app.state.sim.bank._beneficiaries) == [beneficiary.provider_ref]


async def test_without_a_bank_configured_beneficiaries_are_unavailable(
    db: Database, maria: User
) -> None:
    with pytest.raises(ProviderUnavailable):
        await payments.create_beneficiary(
            db,
            acting_as(maria),
            bank=None,
            asset="USD",
            holder_name="Maria Silva",
            account_number=ACCOUNT_NUMBER,
            routing_number=ROUTING_NUMBER,
            idempotency_key="key-1",
        )


async def test_a_user_lists_their_own_beneficiaries_newest_first(
    db: Database, bank: SimBank, maria: User, joao: User
) -> None:
    first = await add_beneficiary(db, bank, maria)
    second = await add_beneficiary(db, bank, maria)
    third = await add_beneficiary(db, bank, maria)
    await add_beneficiary(db, bank, joao)

    async with db.transaction() as session:
        page = await payments.list_beneficiaries(session, acting_as(maria), limit=2)
        rest = await payments.list_beneficiaries(
            session, acting_as(maria), limit=2, cursor=page.next_cursor
        )

    assert [beneficiary.id for beneficiary in page.items] == [third.id, second.id]
    assert [beneficiary.id for beneficiary in rest.items] == [first.id]
    assert rest.next_cursor is None


async def test_beneficiaries_need_their_scopes(
    db: Database, sim: Sim, bank: SimBank, maria: User
) -> None:
    with pytest.raises(InsufficientScope):
        await payments.create_beneficiary(
            db,
            agent_of(maria, Scope.BENEFICIARIES_READ),
            bank=bank,
            asset="USD",
            holder_name="Maria Silva",
            account_number=ACCOUNT_NUMBER,
            routing_number=ROUTING_NUMBER,
            idempotency_key="key-1",
        )
    async with db.transaction() as session:
        with pytest.raises(InsufficientScope):
            await payments.list_beneficiaries(session, agent_of(maria, Scope.BENEFICIARIES_WRITE))

    assert sim.recorder.requests == []


class OneTokenBank:
    """A bank that answers every registration with the same token: what must never be
    believed when the token is already another user's."""

    name = "simbank"

    async def create_beneficiary(self, **arguments: Any) -> ProviderBeneficiary:
        return ProviderBeneficiary(
            id="ben_shared",
            asset_code=arguments["asset_code"],
            rail="ach",
            holder_name=arguments["holder_name"],
            account_mask="••••6789",
        )


async def test_a_token_the_bank_already_gave_another_user_is_not_handed_over(
    db: Database, maria: User, joao: User
) -> None:
    shared: Any = OneTokenBank()
    hers = await add_beneficiary(db, shared, maria)

    with pytest.raises(RuntimeError, match="not the caller's"):
        await add_beneficiary(db, shared, joao)

    (row,) = await rows(db, "SELECT id, user_id FROM beneficiaries")
    assert (row["id"], row["user_id"]) == (hers.id, maria.id)
