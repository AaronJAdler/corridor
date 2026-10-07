"""Reviews: a movement screening held back, and what an operator's decision does to it."""

import asyncio
from typing import Any

import pytest

from corridor import ops, payments, risk
from corridor.identity import Principal, User
from corridor.ops import ReviewHasNoUser
from corridor.payments import DepositNotInSuspense
from corridor.payments import deposits as deposits_module
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.errors import PermissionDenied
from corridor.platform.ids import new_id
from corridor.platform.pagination import InvalidCursor
from corridor.providers import SimBank, SimCustody
from corridor.risk import ReviewAlreadyResolved, ReviewNotFound
from tests.ops.support import audited
from tests.payments.support import (
    acting_as,
    add_beneficiary,
    add_person,
    available,
    count,
    deposit,
    entries,
    held,
    instruction_for,
    rows,
    send,
    suspense,
    withdraw,
    withdrawal_row,
)
from tests.support.providers import Sim

SENDER = "Maria Silva"
PAYOUTS = "/bank/v1/payouts"


@pytest.fixture(name="settings")
def without_a_minimum_fee(settings: Settings) -> Settings:
    """No least withdrawal fee, so that the amounts here are the ones each test names."""
    return settings.model_copy(update={"withdrawal_min_fee": {}})


@pytest.fixture
async def joao(db: Database) -> User:
    async with db.transaction() as session:
        return await add_person(session, "joao")


async def listed(db: Database, kind: str, value: str) -> None:
    async with db.transaction() as session:
        await risk.add_to_denylist(session, kind=kind, value=value, outcome="review")  # type: ignore[arg-type]


async def review_of(db: Database, subject_id: Any) -> dict[str, Any]:
    (row,) = await rows(db, "SELECT * FROM risk_reviews WHERE subject_id = :id", id=subject_id)
    return row


async def withdrawal_under_review(
    db: Database, settings: Settings, bank: SimBank, user: User, amount: int = 100_00
) -> tuple[payments.Withdrawal, Any]:
    """A held bank withdrawal to a holder listed for review, and the id of its review."""
    await deposit(db, user, 500_00)
    beneficiary = await add_beneficiary(db, bank, user)
    await listed(db, "name", beneficiary.holder_name)
    withdrawal = await withdraw(db, settings, user, amount, beneficiary=beneficiary)
    return withdrawal, (await review_of(db, withdrawal.id))["id"]


async def deposit_under_review(
    db: Database, sim: Sim, bank: SimBank, custody: SimCustody, user: User
) -> tuple[dict[str, Any], Any]:
    """A bank deposit of 250.00 USD from a listed sender, in suspense, as a row; and the
    id of its review."""
    instruction = await instruction_for(db, user, "USD", bank, custody)
    data = await sim.bank_deposit(instruction.provider_ref, "250.00")
    await listed(db, "name", data["sender_name"])
    await payments.apply_bank_deposit_received(db, data)
    (row,) = await rows(db, "SELECT * FROM deposits")
    assert row["status"] == "suspense"
    return row, (await review_of(db, row["id"]))["id"]


async def clear(db: Database, admin: Principal, review_id: Any) -> risk.Review:
    async with db.transaction() as session:
        return await ops.clear_review(session, admin, review_id)


async def reject(db: Database, admin: Principal, review_id: Any) -> risk.Review:
    async with db.transaction() as session:
        return await ops.reject_review(session, admin, review_id)


async def submit_events(db: Database) -> int:
    (row,) = await rows(
        db, "SELECT count(*) AS n FROM outbox_events WHERE topic = 'withdrawal.submit'"
    )
    return int(row["n"])


# --- a withdrawal under review ---------------------------------------------------------------


async def test_a_withdrawal_under_review_is_not_sent_and_stays_held(
    db: Database, settings: Settings, sim: Sim, bank: SimBank, custody: SimCustody, maria: User
) -> None:
    withdrawal, _ = await withdrawal_under_review(db, settings, bank, maria)

    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)

    assert sim.recorder.sent("POST", PAYOUTS) == []
    assert (await withdrawal_row(db, withdrawal.id))["status"] == "held"
    assert (await available(db, maria), await held(db, maria)) == (400_00, 100_00)


async def test_clearing_a_review_has_the_withdrawal_sent(
    db: Database,
    settings: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    ana: Principal,
    maria: User,
) -> None:
    withdrawal, review_id = await withdrawal_under_review(db, settings, bank, maria)
    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)

    cleared = await clear(db, ana, review_id)

    assert (cleared.status, cleared.subject_id) == ("cleared", withdrawal.id)
    # The event written with the request has been and gone: clearing writes another.
    assert await submit_events(db) == 2
    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)
    (payout,) = await sim.payouts()
    assert payout["reference"] == str(withdrawal.id)
    assert (await withdrawal_row(db, withdrawal.id))["status"] == "submitted"
    (event,) = await audited(db, "review.cleared")
    assert (event["actor_type"], event["actor_id"]) == ("admin", str(ana.user_id))
    assert (event["resource_type"], event["resource_id"]) == ("review", str(review_id))
    assert event["details"] == {
        "subject_type": "withdrawal",
        "subject_id": str(withdrawal.id),
        "screening": "review",
    }


async def test_rejecting_a_review_gives_the_withdrawal_back_and_it_is_never_sent(
    db: Database,
    settings: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    ana: Principal,
    maria: User,
) -> None:
    withdrawal, review_id = await withdrawal_under_review(db, settings, bank, maria)

    rejected = await reject(db, ana, review_id)

    assert rejected.status == "rejected"
    row = await withdrawal_row(db, withdrawal.id)
    assert (row["status"], row["failure_reason"]) == ("failed", "review_rejected")
    assert (await available(db, maria), await held(db, maria)) == (500_00, 0)
    _hold, release = await entries(db, "withdrawal", str(withdrawal.id))
    assert (release["id"], release["kind"]) == (row["final_entry_id"], "withdrawal_release")
    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)
    assert sim.recorder.sent("POST", PAYOUTS) == []
    (failed,) = await audited(db, "withdrawal.failed")
    assert (failed["actor_type"], failed["actor_id"]) == ("admin", str(ana.user_id))
    assert failed["details"]["reason"] == "review_rejected"
    (event,) = await audited(db, "review.rejected")
    assert event["resource_id"] == str(review_id)


async def test_rejecting_a_review_gives_back_what_the_withdrawal_used_of_the_daily_limit(
    db: Database, settings: Settings, bank: SimBank, ana: Principal, maria: User, joao: User
) -> None:
    await deposit(db, maria, 4_500_00)
    await send(db, settings, maria, joao, 1_000_00)
    await send(db, settings, maria, joao, 1_000_00)
    _, review_id = await withdrawal_under_review(db, settings, bank, maria, 500_00)
    with pytest.raises(risk.LimitExceeded):
        await send(db, settings, maria, joao, 1)

    await reject(db, ana, review_id)

    await send(db, settings, maria, joao, 500_00)


async def test_a_withdrawal_its_user_canceled_is_not_sent_when_its_review_is_cleared(
    db: Database, settings: Settings, bank: SimBank, ana: Principal, maria: User
) -> None:
    withdrawal, review_id = await withdrawal_under_review(db, settings, bank, maria)
    async with db.transaction() as session:
        await payments.cancel_withdrawal(session, acting_as(maria), withdrawal.id)

    await clear(db, ana, review_id)

    assert await submit_events(db) == 1
    assert (await withdrawal_row(db, withdrawal.id))["status"] == "canceled"


async def test_a_withdrawal_its_user_canceled_stays_canceled_when_its_review_is_rejected(
    db: Database, settings: Settings, bank: SimBank, ana: Principal, maria: User
) -> None:
    withdrawal, review_id = await withdrawal_under_review(db, settings, bank, maria)
    async with db.transaction() as session:
        await payments.cancel_withdrawal(session, acting_as(maria), withdrawal.id)

    await reject(db, ana, review_id)

    assert (await withdrawal_row(db, withdrawal.id))["status"] == "canceled"
    assert (await available(db, maria), await held(db, maria)) == (500_00, 0)
    assert len(await entries(db, "withdrawal", str(withdrawal.id))) == 2


# --- a deposit under review ------------------------------------------------------------------


async def test_clearing_a_review_releases_the_deposit_from_suspense_to_its_user(
    db: Database,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    ana: Principal,
    maria: User,
) -> None:
    row, review_id = await deposit_under_review(db, sim, bank, custody, maria)
    assert (await available(db, maria), await suspense(db)) == (0, 250_00)

    cleared = await clear(db, ana, review_id)

    assert cleared.status == "cleared"
    assert (await available(db, maria), await suspense(db)) == (250_00, 0)
    (stored,) = await rows(db, "SELECT * FROM deposits")
    assert (stored["status"], stored["user_id"]) == ("completed", maria.id)
    # The entry that brought the money onto the books is still the deposit's own.
    assert stored["entry_id"] == row["entry_id"]
    arrived, released = await entries(db, "deposit", f"simbank:{row['provider_ref']}")
    assert (arrived["kind"], released["kind"]) == ("deposit_suspense", "deposit_release")
    assert released["postings"] == [("suspense", "D", 250_00), ("user_available", "C", 250_00)]
    (told,) = await rows(db, "SELECT payload FROM outbox_events WHERE topic = 'deposit.completed'")
    assert (told["payload"]["user_id"], told["payload"]["amount"]) == (str(maria.id), "25000")
    (event,) = await audited(db, "deposit.released")
    assert (event["actor_type"], event["actor_id"]) == ("admin", str(ana.user_id))
    # And it is now a deposit its user can see.
    async with db.transaction() as session:
        seen = await payments.get_deposit(session, acting_as(maria), row["id"])
    assert seen.status == "completed"


async def test_rejecting_a_review_leaves_the_deposit_in_suspense(
    db: Database,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    ana: Principal,
    maria: User,
) -> None:
    _, review_id = await deposit_under_review(db, sim, bank, custody, maria)
    journal = await count(db, "journal_entries")

    rejected = await reject(db, ana, review_id)

    assert rejected.status == "rejected"
    assert (await available(db, maria), await suspense(db)) == (0, 250_00)
    (stored,) = await rows(db, "SELECT * FROM deposits")
    assert (stored["status"], stored["user_id"]) == ("suspense", None)
    assert await count(db, "journal_entries") == journal


async def test_a_deposit_that_arrived_at_nobodys_account_cannot_be_cleared(
    db: Database, ana: Principal
) -> None:
    await listed(db, "name", SENDER)
    await payments.apply_bank_deposit_received(
        db,
        {
            "deposit_id": "dep_stray",
            "virtual_account_id": "va_nobody",
            "asset": "USD",
            "amount": "250.00",
            "sender_name": SENDER,
            "reference": "",
        },
    )
    (row,) = await rows(db, "SELECT * FROM deposits")
    review = await review_of(db, row["id"])

    with pytest.raises(ReviewHasNoUser) as refusal:
        await clear(db, ana, review["id"])

    assert (refusal.value.status, refusal.value.code) == (409, "review_has_no_user")
    assert (await review_of(db, row["id"]))["status"] == "open"
    assert await suspense(db) == 250_00
    # It can still be rejected.
    assert (await reject(db, ana, review["id"])).status == "rejected"


async def test_a_deposit_the_bank_took_back_cannot_be_cleared_and_its_review_stays_open(
    db: Database,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    ana: Principal,
    maria: User,
) -> None:
    row, review_id = await deposit_under_review(db, sim, bank, custody, maria)
    await payments.apply_bank_deposit_returned(
        db, await sim.return_bank_deposit(row["provider_ref"])
    )

    with pytest.raises(DepositNotInSuspense) as refusal:
        await clear(db, ana, review_id)

    assert (refusal.value.status, refusal.value.code) == (409, "deposit_not_in_suspense")
    assert (await review_of(db, row["id"]))["status"] == "open"
    assert (await available(db, maria), await suspense(db)) == (0, 0)


# --- a decision is made once -----------------------------------------------------------------


@pytest.mark.parametrize("first", ["clear", "reject"])
@pytest.mark.parametrize("second", ["clear", "reject"])
async def test_a_withdrawal_review_is_decided_once(
    db: Database,
    settings: Settings,
    bank: SimBank,
    ana: Principal,
    bruno: Principal,
    maria: User,
    first: str,
    second: str,
) -> None:
    withdrawal, review_id = await withdrawal_under_review(db, settings, bank, maria)
    decide = {"clear": clear, "reject": reject}
    await decide[first](db, ana, review_id)
    before = (await submit_events(db), await count(db, "journal_entries"))

    with pytest.raises(ReviewAlreadyResolved) as refusal:
        await decide[second](db, bruno, review_id)

    assert (refusal.value.status, refusal.value.code) == (409, "review_already_resolved")
    assert (await submit_events(db), await count(db, "journal_entries")) == before
    assert (await withdrawal_row(db, withdrawal.id))["status"] == (
        "held" if first == "clear" else "failed"
    )


async def test_a_deposit_review_is_cleared_once(
    db: Database,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    ana: Principal,
    bruno: Principal,
    maria: User,
) -> None:
    _, review_id = await deposit_under_review(db, sim, bank, custody, maria)
    await clear(db, ana, review_id)

    for decide in (clear, reject):
        with pytest.raises(ReviewAlreadyResolved):
            await decide(db, bruno, review_id)

    assert (await available(db, maria), await suspense(db)) == (250_00, 0)


async def test_of_a_clearance_and_a_rejection_of_a_withdrawal_at_once_exactly_one_wins(
    db: Database,
    settings: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    ana: Principal,
    bruno: Principal,
    maria: User,
) -> None:
    withdrawal, review_id = await withdrawal_under_review(db, settings, bank, maria)

    outcomes = await asyncio.gather(
        clear(db, ana, review_id), reject(db, bruno, review_id), return_exceptions=True
    )

    (decided,) = [outcome for outcome in outcomes if isinstance(outcome, risk.Review)]
    (refused,) = [outcome for outcome in outcomes if not isinstance(outcome, risk.Review)]
    assert isinstance(refused, ReviewAlreadyResolved)
    assert (await review_of(db, withdrawal.id))["status"] == decided.status
    await payments.submit_withdrawal(db, bank, custody, withdrawal.id)
    row = await withdrawal_row(db, withdrawal.id)
    if decided.status == "cleared":
        # Sent, and its funds are still reserved: the rejection gave nothing back.
        assert row["status"] == "submitted"
        assert (await available(db, maria), await held(db, maria)) == (400_00, 100_00)
        assert len(await sim.payouts()) == 1
    else:
        assert (row["status"], row["failure_reason"]) == ("failed", "review_rejected")
        assert (await available(db, maria), await held(db, maria)) == (500_00, 0)
        assert await sim.payouts() == []


async def test_of_a_clearance_and_a_rejection_of_a_deposit_at_once_exactly_one_wins(
    db: Database,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    ana: Principal,
    bruno: Principal,
    maria: User,
) -> None:
    row, review_id = await deposit_under_review(db, sim, bank, custody, maria)

    outcomes = await asyncio.gather(
        clear(db, ana, review_id), reject(db, bruno, review_id), return_exceptions=True
    )

    (decided,) = [outcome for outcome in outcomes if isinstance(outcome, risk.Review)]
    (refused,) = [outcome for outcome in outcomes if not isinstance(outcome, risk.Review)]
    assert isinstance(refused, ReviewAlreadyResolved)
    assert (await review_of(db, row["id"]))["status"] == decided.status
    expected = (250_00, 0) if decided.status == "cleared" else (0, 250_00)
    assert (await available(db, maria), await suspense(db)) == expected


async def test_twenty_clearances_at_once_release_a_deposit_once(
    db: Database,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    ana: Principal,
    maria: User,
) -> None:
    _, review_id = await deposit_under_review(db, sim, bank, custody, maria)

    outcomes = await asyncio.gather(
        *(clear(db, ana, review_id) for _ in range(20)), return_exceptions=True
    )

    assert sum(isinstance(outcome, risk.Review) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, ReviewAlreadyResolved) for outcome in outcomes) == 19
    assert (await available(db, maria), await suspense(db)) == (250_00, 0)


# --- who may, and what there is ----------------------------------------------------------------


async def test_a_review_that_does_not_exist_cannot_be_decided(db: Database, ana: Principal) -> None:
    for decide in (clear, reject):
        with pytest.raises(ReviewNotFound) as refusal:
            await decide(db, ana, new_id())
        assert (refusal.value.status, refusal.value.code) == (404, "review_not_found")


async def test_only_an_administrator_reads_or_decides_reviews(
    db: Database, settings: Settings, bank: SimBank, maria: User
) -> None:
    withdrawal, review_id = await withdrawal_under_review(db, settings, bank, maria)
    herself = acting_as(maria)

    for decide in (clear, reject):
        with pytest.raises(PermissionDenied):
            await decide(db, herself, review_id)
    with pytest.raises(PermissionDenied):
        async with db.transaction() as session:
            await ops.list_open_reviews(session, herself)

    assert (await review_of(db, withdrawal.id))["status"] == "open"
    assert (await withdrawal_row(db, withdrawal.id))["status"] == "held"


async def opened(db: Database, count_: int) -> list[Any]:
    """Reviews of made-up withdrawals, oldest first, by id."""
    made = []
    for _ in range(count_):
        async with db.transaction() as session:
            review = await risk.open_review(
                session, subject_type="withdrawal", subject_id=new_id(), outcome="review"
            )
        made.append(review.id)
    return made


async def test_open_reviews_are_listed_newest_first_a_page_at_a_time(
    db: Database, ana: Principal
) -> None:
    first, second, third, fourth = await opened(db, 4)
    async with db.transaction() as session:
        await risk.resolve_review(
            session,
            subject_type="withdrawal",
            subject_id=await _subject(db, third),
            cleared=True,
        )

    async with db.transaction() as session:
        page = await ops.list_open_reviews(session, ana, limit=2)
    async with db.transaction() as session:
        rest = await ops.list_open_reviews(session, ana, cursor=page.next_cursor, limit=2)

    assert [review.id for review in page.items] == [fourth, second]
    assert page.next_cursor is not None
    assert ([review.id for review in rest.items], rest.next_cursor) == ([first], None)
    assert all(review.status == "open" for review in page.items + rest.items)
    listings = await audited(db, "review.listed")
    assert [event["details"] for event in listings] == [{"returned": 2}, {"returned": 1}]
    assert {event["actor_id"] for event in listings} == {str(ana.user_id)}


async def test_a_cursor_from_another_list_is_refused(db: Database, ana: Principal) -> None:
    await opened(db, 2)
    async with db.transaction() as session:
        other = await risk.list_reviews(session, limit=1)
    assert other.next_cursor is not None

    with pytest.raises(InvalidCursor):
        async with db.transaction() as session:
            await ops.list_open_reviews(session, ana, cursor=other.next_cursor)


async def _subject(db: Database, review_id: Any) -> Any:
    (row,) = await rows(db, "SELECT subject_id FROM risk_reviews WHERE id = :id", id=review_id)
    return row["subject_id"]


# --- a release that overtakes a return -------------------------------------------------------


async def test_a_return_that_read_the_deposit_before_it_was_released_is_delivered_again(
    db: Database,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    ana: Principal,
    maria: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A return learns whose money-out lock to take by reading the deposit before it locks
    it. If the deposit is released to its user in between, that lock was nobody's, and the
    return must not go on to take the money out of the user's balance without it."""
    row, review_id = await deposit_under_review(db, sim, bank, custody, maria)
    returned = await sim.return_bank_deposit(row["provider_ref"])
    find_deposit = deposits_module.find_deposit
    async with db.transaction() as session:
        stale = await find_deposit(session, "simbank", row["provider_ref"])
    await clear(db, ana, review_id)

    async def read_before_the_release(*_arguments: Any) -> Any:
        return stale

    with monkeypatch.context() as patch:
        patch.setattr(deposits_module, "find_deposit", read_before_the_release)
        with pytest.raises(payments.DepositNotReceived):
            await payments.apply_bank_deposit_returned(db, returned)

    assert (await available(db, maria), await suspense(db)) == (250_00, 0)
    # Delivered again, it reads the deposit as it now is and takes the money back.
    await payments.apply_bank_deposit_returned(db, returned)
    assert (await available(db, maria), await suspense(db)) == (0, 0)
    (stored,) = await rows(db, "SELECT status FROM deposits")
    assert stored["status"] == "returned"
