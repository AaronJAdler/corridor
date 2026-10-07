"""The payments verifier: what the ledger holds for withdrawals and for suspense, against
the withdrawals and deposits that say why it holds it.

The ledger's own verifier cannot see either: a hold that was given back twice, or a deposit
that left suspense and is still recorded as in it, leaves every entry balanced and every
balance equal to its postings.
"""

import os
import subprocess
import sys
from typing import Any

import pytest
from sqlalchemy import text

from corridor import audit, ledger, payments, wallets
from corridor.identity import User
from corridor.ledger import AccountKind
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.providers import SimBank, SimCustody
from tests.payments.support import (
    acting_as,
    add_beneficiary,
    deposit,
    held_bank_withdrawal,
    leave_submitting,
    rows,
    withdraw,
)
from tests.support import postgres
from tests.support.ledger import fund
from tests.support.providers import EXTERNAL_ADDRESS, Sim

# These tests leave behind exactly what the payments verifier reports, on purpose.
pytestmark = pytest.mark.usefixtures("written_by_hand")


async def findings(db: Database, **options: Any) -> list[ledger.Finding]:
    async with db.transaction() as session:
        return await payments.verify(session, **options)


async def in_suspense(db: Database, sim: Sim, amount: str = "40.00") -> dict[str, Any]:
    """A bank deposit to an account nobody was given, booked to suspense."""
    data = await sim.bank_deposit("va_nobody", amount)
    await payments.apply_bank_deposit_received(db, data)
    return data


async def test_books_with_nothing_reserved_and_nothing_in_suspense_have_no_findings(
    db: Database, maria: User
) -> None:
    await deposit(db, maria, 500_00)

    assert await findings(db) == []


async def test_withdrawals_in_every_state_and_a_deposit_in_suspense_are_no_finding(
    db: Database,
    settings: Settings,
    sim: Sim,
    bank: SimBank,
    custody: SimCustody,
    maria: User,
    joao: User,
) -> None:
    # Held, being sent, sent, settled, and given back: only the first three reserve.
    held = await held_bank_withdrawal(db, settings, bank, maria, 10_00)
    beneficiary = await add_beneficiary(db, bank, maria)
    submitting = await withdraw(db, settings, maria, 20_00, beneficiary=beneficiary)
    await leave_submitting(db, submitting.id)
    submitted = await withdraw(db, settings, maria, 30_00, beneficiary=beneficiary)
    await payments.submit_withdrawal(db, bank, custody, submitted.id)
    canceled = await withdraw(db, settings, maria, 40_00, beneficiary=beneficiary)
    async with db.transaction() as session:
        await payments.cancel_withdrawal(session, acting_as(maria), canceled.id)
    await deposit(db, joao, 50_000_000, "USDC")
    await withdraw(db, settings, joao, 25_000_000, asset="USDC", to_address=EXTERNAL_ADDRESS)
    await in_suspense(db, sim)

    assert held.status == "held"
    assert await findings(db) == []


async def test_a_hold_given_back_without_its_withdrawal_is_found(
    db: Database, settings: Settings, bank: SimBank, maria: User
) -> None:
    withdrawal = await held_bank_withdrawal(db, settings, bank, maria, 100_00)
    reserved = withdrawal.amount + withdrawal.fee
    # What an adjustment written by hand could once do: the held balance goes back to the
    # user, and the withdrawal is still held and will still be sent.
    async with db.transaction() as session:
        wallet = await wallets.get_wallet(session, maria.id, "USD")
        await ledger.post_entry(
            session,
            ledger.EntryDraft(
                "adjustment",
                "adjustment",
                "by-hand",
                (
                    ledger.debit(wallet.held_account_id, reserved),
                    ledger.credit(wallet.available_account_id, reserved),
                ),
            ),
        )

    assert [(f.check, f.subject, f.detail) for f in await findings(db)] == [
        (
            "held_mismatch",
            f"{maria.id}:USD",
            f"user_held is 0 but the withdrawals that still reserve funds come to {reserved}",
        )
    ]


async def test_a_withdrawal_ended_without_its_hold_being_moved_is_found(
    db: Database, superuser_db: Database, settings: Settings, bank: SimBank, maria: User
) -> None:
    withdrawal = await held_bank_withdrawal(db, settings, bank, maria, 100_00)
    reserved = withdrawal.amount + withdrawal.fee
    async with superuser_db.transaction() as session:
        await session.execute(
            text("UPDATE withdrawals SET status = 'failed' WHERE id = :id"), {"id": withdrawal.id}
        )

    assert [(f.check, f.subject, f.detail) for f in await findings(db)] == [
        (
            "held_mismatch",
            f"{maria.id}:USD",
            f"user_held is {reserved} but the withdrawals that still reserve funds come to 0",
        )
    ]


async def test_what_is_reserved_is_compared_user_by_user_and_asset_by_asset(
    db: Database,
    superuser_db: Database,
    settings: Settings,
    bank: SimBank,
    maria: User,
    joao: User,
) -> None:
    # The two differences cancel in the total. Each user is still wrong.
    first = await held_bank_withdrawal(db, settings, bank, maria, 100_00)
    second = await held_bank_withdrawal(db, settings, bank, joao, 100_00)
    async with superuser_db.transaction() as session:
        await session.execute(
            text("UPDATE withdrawals SET user_id = :other WHERE id = :id"),
            {"other": joao.id, "id": first.id},
        )
        await session.execute(
            text("UPDATE withdrawals SET asset_code = 'MXN' WHERE id = :id"), {"id": second.id}
        )

    found = await findings(db)

    assert {(f.check, f.subject) for f in found} == {
        ("held_mismatch", f"{maria.id}:USD"),
        ("held_mismatch", f"{joao.id}:MXN"),
    }


async def test_a_deposit_recorded_as_in_suspense_whose_money_has_left_is_found(
    db: Database, superuser_db: Database, sim: Sim
) -> None:
    data = await in_suspense(db, sim)
    (row,) = await rows(
        db, "SELECT id FROM deposits WHERE provider_ref = :ref", ref=data["deposit_id"]
    )
    async with db.transaction() as session:
        await payments.return_from_suspense(session, row["id"], actor=audit.Actor.system("test"))
    async with superuser_db.transaction() as session:
        # The entry that took it out stands. The row says it never left.
        await session.execute(
            text("UPDATE deposits SET status = 'suspense' WHERE id = :id"), {"id": row["id"]}
        )

    assert [(f.check, f.subject, f.detail) for f in await findings(db)] == [
        (
            "suspense_mismatch",
            "USD",
            "suspense holds 0 but the deposits in suspense come to 4000",
        )
    ]


async def test_money_in_suspense_that_no_deposit_accounts_for_is_found(db: Database) -> None:
    async with db.transaction() as session:
        suspense = await ledger.open_account(session, AccountKind.SUSPENSE, "MXN")
        settlement = await ledger.open_account(
            session, AccountKind.BANK_SETTLEMENT, "MXN", provider="simbank"
        )
        await ledger.post_entry(
            session,
            ledger.EntryDraft(
                "adjustment",
                "adjustment",
                "by-hand",
                (ledger.debit(settlement.id, 7_00), ledger.credit(suspense.id, 7_00)),
            ),
        )

    assert [(f.check, f.subject, f.detail) for f in await findings(db)] == [
        ("suspense_mismatch", "MXN", "suspense holds 700 but the deposits in suspense come to 0")
    ]


async def test_findings_are_capped_for_each_check(
    db: Database,
    superuser_db: Database,
    settings: Settings,
    bank: SimBank,
    maria: User,
    joao: User,
) -> None:
    for user in (maria, joao):
        await held_bank_withdrawal(db, settings, bank, user, 100_00)
    async with superuser_db.transaction() as session:
        await session.execute(text("UPDATE withdrawals SET status = 'failed'"))

    assert len(await findings(db)) == 2
    assert len(await findings(db, limit_per_check=1)) == 1


# --- the command -----------------------------------------------------------------------------


async def test_the_verify_ledger_command_reports_what_the_payments_verifier_finds(
    database: postgres.TestDatabase, settings: Settings, db: Database
) -> None:
    async with db.transaction() as session:
        suspense = await ledger.open_account(session, AccountKind.SUSPENSE, "USD")
        await fund(session, suspense.id, 12_00)

    result = subprocess.run(  # noqa: ASYNC221 - a short command, and nothing else is running
        [sys.executable, "-m", "corridor", "verify-ledger"],
        cwd=postgres.REPO_ROOT,
        env={
            **os.environ,
            "CORRIDOR_DATABASE_URL": database.app_url,
            "CORRIDOR_REDIS_URL": settings.redis_url.get_secret_value(),
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 1
    assert result.stdout.strip() == (
        "suspense_mismatch: USD: suspense holds 1200 but the deposits in suspense come to 0"
    )
    assert "Ledger verification FAILED: 1 finding(s)." in result.stderr
