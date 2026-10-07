"""The jobs that keep watch and tidy up: the ledger verifier and the purge of old login
failure counts."""

from datetime import timedelta

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from corridor import ledger, payments
from corridor.ledger import AccountKind
from corridor.platform.clock import ManualClock
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.worker import Scheduler, build_jobs
from corridor.worker.jobs import LEDGER_VERIFY_JOB
from corridor.worker.scheduler import Job
from tests.outbox.helpers import LogReader
from tests.support.ledger import fund, funded_user

PURGE_JOB = "auth.purge_login_failures"


def job_named(settings: Settings, name: str) -> Job:
    (job,) = [job for job in build_jobs(settings) if job.name == name]
    return job


def gauge(name: str) -> float | None:
    return REGISTRY.get_sample_value(name)


def test_every_worker_runs_the_jobs_that_need_no_provider(settings: Settings) -> None:
    # Written out: a job that is added or dropped is a decision that shows up here.
    assert [(job.name, job.interval_seconds) for job in build_jobs(settings)] == [
        ("outbox.purge_finished", 3600),
        ("idempotency.purge_expired", 3600),
        ("fx.purge_unused_quotes", 3600),
        ("auth.purge_login_failures", 3600),
        ("webhooks.redact_payloads", 3600),
        ("ledger.verify", 3600),
    ]


# --- the ledger verifier -----------------------------------------------------------------------


async def test_the_verifier_job_reports_a_sound_ledger_as_no_findings(
    db: Database, settings: Settings, clock: ManualClock, logs: LogReader
) -> None:
    async with db.transaction() as session:
        await funded_user(session, 100_00)

    await job_named(settings, LEDGER_VERIFY_JOB).run(db)

    assert gauge("corridor_ledger_verifier_findings") == 0
    assert gauge("corridor_ledger_verifier_last_run_timestamp_seconds") == clock.now().timestamp()
    assert [line["event"] for line in logs() if line["event"].startswith("ledger.")] == [
        "ledger.verify_ok"
    ]


@pytest.mark.corrupts_ledger
async def test_the_verifier_job_reports_what_it_finds_and_does_not_fail(
    db: Database,
    superuser_db: Database,
    settings: Settings,
    clock: ManualClock,
    logs: LogReader,
) -> None:
    async with db.transaction() as session:
        maria = await funded_user(session, 100_00)
    async with superuser_db.transaction() as session:
        # The cached balance says more than the postings add up to.
        await session.execute(
            text("UPDATE account_balances SET balance = balance + 1 WHERE account_id = :account"),
            {"account": maria.available},
        )

    # Through the scheduler, as a worker runs it: a finding is a result, not a failure
    # of the job, so the job is recorded as having run without an error.
    assert LEDGER_VERIFY_JOB in await Scheduler(db, build_jobs(settings)).tick()

    assert gauge("corridor_ledger_verifier_findings") == 1
    (failed,) = [line for line in logs() if line["event"] == "ledger.verify_failed"]
    assert (failed["level"], failed["findings"]) == ("error", 1)
    assert failed["checks"] == ["balance_mismatch"]
    async with db.transaction() as session:
        error = await session.execute(
            text("SELECT last_error FROM job_runs WHERE name = :name"),
            {"name": LEDGER_VERIFY_JOB},
        )
        assert error.scalar_one() is None


async def test_the_verifier_job_also_reports_what_the_payments_verifier_finds(
    db: Database, settings: Settings, logs: LogReader
) -> None:
    # Money in suspense that no deposit accounts for. The ledger's own checks see nothing
    # wrong with it: the entry balances.
    async with db.transaction() as session:
        suspense = await ledger.open_account(session, AccountKind.SUSPENSE, "USD")
        await fund(session, suspense.id, 12_00)

    assert LEDGER_VERIFY_JOB in await Scheduler(db, build_jobs(settings)).tick()

    assert gauge("corridor_ledger_verifier_findings") == 1
    (failed,) = [line for line in logs() if line["event"] == "ledger.verify_failed"]
    assert (failed["findings"], failed["checks"]) == (1, ["suspense_mismatch"])


async def test_the_verifier_job_reads_both_verifiers_in_one_snapshot(
    db: Database, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, str]] = []

    async def isolation(session: AsyncSession, **_options: object) -> list[ledger.Finding]:
        level = await session.execute(text("SHOW transaction_isolation"))
        read_only = await session.execute(text("SHOW transaction_read_only"))
        seen.append((level.scalar_one(), read_only.scalar_one()))
        return []

    monkeypatch.setattr(payments, "verify", isolation)

    await job_named(settings, LEDGER_VERIFY_JOB).run(db)

    assert seen == [("repeatable read", "on")]


# --- old login failure counts ------------------------------------------------------------------


async def add_counts(db: Database, clock: ManualClock, name: str, *, locked_for: int = 0) -> None:
    async with db.transaction() as session:
        await session.execute(
            text(
                "INSERT INTO login_lockouts (email_hash, client, failed_logins, locked_until,"
                " updated_at) VALUES (:name, '203.0.113.7', 5, :until, :now)"
            ),
            {
                "name": name,
                "until": clock.now() + timedelta(seconds=locked_for) if locked_for else None,
                "now": clock.now(),
            },
        )
        await session.execute(
            text(
                "INSERT INTO login_throttles (email_hash, failed_logins, last_failed_at)"
                " VALUES (:name, 5, :now)"
            ),
            {"name": name, "now": clock.now()},
        )


async def counted(db: Database, table: str) -> set[str]:
    async with db.transaction() as session:
        return set((await session.execute(text(f"SELECT email_hash FROM {table}"))).scalars())  # noqa: S608


async def test_the_purge_job_deletes_counts_nothing_has_added_to_for_a_day(
    db: Database, settings: Settings, clock: ManualClock
) -> None:
    await add_counts(db, clock, "a-day-and-a-second-old")
    clock.advance(seconds=1)
    await add_counts(db, clock, "a-day-old")
    clock.advance(hours=24)

    await job_named(settings, PURGE_JOB).run(db)

    assert await counted(db, "login_lockouts") == {"a-day-old"}
    assert await counted(db, "login_throttles") == {"a-day-old"}


async def test_the_purge_job_keeps_a_count_that_still_locks_somebody_out(
    db: Database, settings: Settings, clock: ManualClock
) -> None:
    # Not something the settings allow today, where the longest lock is an hour. The
    # purge must not be what shortens a lock if that changes.
    await add_counts(db, clock, "locked-for-two-days", locked_for=2 * 24 * 3600)
    clock.advance(hours=25)

    await job_named(settings, PURGE_JOB).run(db)

    assert await counted(db, "login_lockouts") == {"locked-for-two-days"}
    assert await counted(db, "login_throttles") == set()
