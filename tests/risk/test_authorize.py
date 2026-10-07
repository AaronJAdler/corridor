"""The authorisation hook every money movement passes through before it posts."""

import pytest

from corridor import identity, risk
from corridor.identity import Principal, User
from corridor.platform.db import Database
from corridor.platform.ids import new_id
from corridor.risk import MoneyMovement
from tests.identity.support import add_user, close_account


def transfer(sender: User, recipient: User, amount: int = 5_00) -> MoneyMovement:
    return MoneyMovement(
        kind="transfer",
        user_id=sender.id,
        principal=Principal.for_user(sender.id, "user", new_id()),
        asset="USD",
        amount=amount,
        counterparty_id=recipient.id,
    )


@pytest.fixture
async def maria(db: Database) -> User:
    async with db.transaction() as session:
        return await add_user(session, "maria")


@pytest.fixture
async def joao(db: Database) -> User:
    async with db.transaction() as session:
        return await add_user(session, "joao")


async def test_a_movement_between_two_active_users_is_allowed(
    db: Database, maria: User, joao: User
) -> None:
    async with db.transaction() as session:
        decision = await risk.authorize(session, transfer(maria, joao))

    assert decision == risk.Decision(outcome="allow")


async def test_a_restricted_user_cannot_move_money_out(
    db: Database, maria: User, joao: User
) -> None:
    async with db.transaction() as session:
        await identity.restrict_user(session, maria.id, "unpaid receivable")

    with pytest.raises(risk.UserRestricted) as refusal:
        async with db.transaction() as session:
            await risk.authorize(session, transfer(maria, joao))

    assert isinstance(refusal.value, risk.Denied)
    assert (refusal.value.status, refusal.value.code) == (403, "user_restricted")
    # Why the account is restricted is between the user and an operator.
    assert "receivable" not in str(refusal.value.detail)


async def test_a_user_whose_restriction_was_lifted_moves_money_again(
    db: Database, maria: User, joao: User
) -> None:
    async with db.transaction() as session:
        await identity.restrict_user(session, maria.id, "review")
        await identity.lift_restriction(session, maria.id)

    async with db.transaction() as session:
        assert (await risk.authorize(session, transfer(maria, joao))).outcome == "allow"


async def test_a_closed_account_cannot_move_money_out(
    db: Database, maria: User, joao: User
) -> None:
    async with db.transaction() as session:
        await close_account(session, maria.id)

    with pytest.raises(risk.UserRestricted):
        async with db.transaction() as session:
            await risk.authorize(session, transfer(maria, joao))


async def test_a_restricted_user_can_still_be_paid(db: Database, maria: User, joao: User) -> None:
    async with db.transaction() as session:
        await identity.restrict_user(session, joao.id, "review")

    async with db.transaction() as session:
        assert (await risk.authorize(session, transfer(maria, joao))).outcome == "allow"


async def test_a_closed_account_cannot_be_paid_and_looks_like_no_account(
    db: Database, maria: User, joao: User
) -> None:
    async with db.transaction() as session:
        await close_account(session, joao.id)

    with pytest.raises(risk.CounterpartyUnavailable) as refusal:
        async with db.transaction() as session:
            await risk.authorize(session, transfer(maria, joao))

    assert isinstance(refusal.value, risk.Denied)
    assert (refusal.value.status, refusal.value.code) == (404, "recipient_not_found")


async def test_a_counterparty_that_is_nobody_cannot_be_paid(db: Database, maria: User) -> None:
    nobody = MoneyMovement(
        kind="transfer",
        user_id=maria.id,
        principal=Principal.for_user(maria.id, "user", new_id()),
        asset="USD",
        amount=1,
        counterparty_id=new_id(),
    )

    with pytest.raises(risk.CounterpartyUnavailable):
        async with db.transaction() as session:
            await risk.authorize(session, nobody)


async def test_a_movement_with_no_counterparty_is_judged_on_the_user_alone(
    db: Database, maria: User
) -> None:
    alone = MoneyMovement(
        kind="transfer",
        user_id=maria.id,
        principal=Principal.for_user(maria.id, "user", new_id()),
        asset="USD",
        amount=1,
    )

    async with db.transaction() as session:
        assert (await risk.authorize(session, alone)).outcome == "allow"
