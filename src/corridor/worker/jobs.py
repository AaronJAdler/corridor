"""The scheduled jobs a worker runs."""

from datetime import timedelta

from corridor import outbox, payments
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
# How often overdue withdrawals are looked for. How long one waits before it counts as
# overdue is a setting, `payout_sweep_after_seconds`.
PAYOUT_SWEEP_INTERVAL_SECONDS = 30.0


def build_jobs(
    settings: Settings, *, bank: BankRail | None = None, custody: Custodian | None = None
) -> list[Job]:
    """Every job that exists today. The payout sweep is among them only when there is a
    provider to ask: a worker with none has no payouts to read."""
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

    async def sweep_payouts(db: Database) -> None:
        advanced = await payments.sweep_payouts(db, bank, custody, settings)
        log.info("payout_sweep.done", advanced=advanced)

    jobs = [
        Job("outbox.purge_finished", PURGE_INTERVAL_SECONDS, purge_finished),
        Job("idempotency.purge_expired", PURGE_INTERVAL_SECONDS, purge_idempotency),
    ]
    if bank is not None or custody is not None:
        jobs.append(Job("payments.sweep_payouts", PAYOUT_SWEEP_INTERVAL_SECONDS, sweep_payouts))
    return jobs
