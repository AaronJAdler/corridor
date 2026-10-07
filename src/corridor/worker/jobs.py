"""The scheduled jobs a worker runs."""

from datetime import timedelta

from corridor import fx, outbox, payments, recon
from corridor.platform.clock import utcnow
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.logging import get_logger
from corridor.providers import BankRail, Custodian
from corridor.worker.purge import purge_idempotency_keys
from corridor.worker.scheduler import Job

log = get_logger(__name__)

PURGE_INTERVAL_SECONDS = 3600.0
# A day: longer than any client goes on retrying one request.
IDEMPOTENCY_KEY_RETENTION = timedelta(hours=24)
# A quote lives for seconds. One that expired a day ago and was never converted answers no
# question anybody still has.
UNUSED_QUOTE_RETENTION = timedelta(hours=24)
# How often overdue withdrawals are looked for. How long one waits before it counts as
# overdue is a setting, `payout_sweep_after_seconds`.
PAYOUT_SWEEP_INTERVAL_SECONDS = 30.0
RECONCILIATION_JOB = "recon.run"
RECONCILIATION_INTERVAL_SECONDS = 300.0
# What each run looks back over. Much longer than the interval, so that every movement is
# compared many times and a run that was missed leaves no gap.
RECONCILIATION_WINDOW = timedelta(hours=1)


def build_jobs(
    settings: Settings,
    *,
    bank: BankRail | None = None,
    custody: Custodian | None = None,
    reconcile: bool = False,
) -> list[Job]:
    """Every job that exists today. The payout sweep is among them only when there is a
    provider to ask: a worker with none has no payouts to read. Reconciliation is among
    them when it is asked for and there is a provider to compare with."""
    retention = timedelta(days=settings.outbox_retention_days)

    async def purge_finished(db: Database) -> None:
        # Finished events are kept for a while, to answer "what happened to this one?",
        # and then deleted so the queue's table stays the size of the work in hand.
        async with db.transaction() as session:
            deleted = await outbox.purge_finished(session, older_than=utcnow() - retention)
        log.info("outbox.purged", deleted=deleted)

    async def purge_idempotency(db: Database) -> None:
        async with db.transaction() as session:
            deleted = await purge_idempotency_keys(
                session, older_than=utcnow() - IDEMPOTENCY_KEY_RETENTION
            )
        log.info("idempotency.purged", deleted=deleted)

    async def purge_quotes(db: Database) -> None:
        async with db.transaction() as session:
            deleted = await fx.purge_unused_quotes(
                session, expired_before=utcnow() - UNUSED_QUOTE_RETENTION
            )
        log.info("fx.quotes_purged", deleted=deleted)

    async def sweep_payouts(db: Database) -> None:
        advanced = await payments.sweep_payouts(db, bank, custody, settings)
        log.info("payout_sweep.done", advanced=advanced)

    async def reconcile_window(db: Database) -> None:
        # The scheduler runs a job on one worker at a time, so two runs do not overlap;
        # if they did, each disagreement would still get one break.
        now = utcnow()
        result = await recon.run(
            db, bank, custody, window_start=now - RECONCILIATION_WINDOW, window_end=now
        )
        log.info(
            "recon.done",
            status=result.run.status,
            breaks_found=result.run.breaks_found,
            repaired=result.repaired,
        )

    jobs = [
        Job("outbox.purge_finished", PURGE_INTERVAL_SECONDS, purge_finished),
        Job("idempotency.purge_expired", PURGE_INTERVAL_SECONDS, purge_idempotency),
        Job("fx.purge_unused_quotes", PURGE_INTERVAL_SECONDS, purge_quotes),
    ]
    if bank is not None or custody is not None:
        jobs.append(Job("payments.sweep_payouts", PAYOUT_SWEEP_INTERVAL_SECONDS, sweep_payouts))
        if reconcile:
            jobs.append(Job(RECONCILIATION_JOB, RECONCILIATION_INTERVAL_SECONDS, reconcile_window))
    return jobs
