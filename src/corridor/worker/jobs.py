"""The scheduled jobs a worker runs."""

from datetime import timedelta

from corridor import outbox
from corridor.platform.clock import utcnow
from corridor.platform.config import Settings
from corridor.platform.db import Database
from corridor.platform.logging import get_logger
from corridor.worker.purge import purge_idempotency_keys
from corridor.worker.scheduler import Job

log = get_logger(__name__)

PURGE_INTERVAL_SECONDS = 3600.0
# A day: longer than any client goes on retrying one request.
IDEMPOTENCY_KEY_RETENTION = timedelta(hours=24)


def build_jobs(settings: Settings) -> list[Job]:
    """Every job that exists today."""
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

    return [
        Job("outbox.purge_finished", PURGE_INTERVAL_SECONDS, purge_finished),
        Job("idempotency.purge_expired", PURGE_INTERVAL_SECONDS, purge_idempotency),
    ]
