"""Prometheus metrics.

Metrics are defined here, in one place, so the full set is visible and no name is declared
twice. They live in the default registry and are exposed at ``/metrics``.
"""

from prometheus_client import Counter, Gauge

DB_TRANSACTION_RETRIES = Counter(
    "corridor_db_transaction_retries_total",
    "Transactions re-run after a deadlock or serialisation failure.",
    ["sqlstate"],
)

REDIS_UNAVAILABLE = Counter(
    "corridor_redis_unavailable_total",
    "Redis operations that failed and fell back to their degraded behaviour.",
    ["use"],
)

RATE_LIMIT_REJECTIONS = Counter(
    "corridor_rate_limit_rejections_total",
    "Requests refused because a rate limit was exceeded.",
    ["group"],
)

OUTBOX_PROCESSED = Counter(
    "corridor_outbox_processed_total",
    "Outbox events handled, by topic and outcome (done, retry, dead).",
    ["topic", "outcome"],
)

OUTBOX_PENDING = Gauge(
    "corridor_outbox_pending",
    "Outbox events waiting to be processed.",
)

OUTBOX_OLDEST_PENDING_SECONDS = Gauge(
    "corridor_outbox_oldest_pending_seconds",
    "Age of the oldest outbox event that is due and not yet processed.",
)

OUTBOX_DEAD = Gauge(
    "corridor_outbox_dead",
    "Outbox events that exhausted their attempts and need an operator.",
)

SCHEDULED_JOB_RUNS = Counter(
    "corridor_scheduled_job_runs_total",
    "Scheduled job runs, by job and outcome (ok, error).",
    ["job", "outcome"],
)

RECON_BREAKS = Counter(
    "corridor_recon_breaks_total",
    "Reconciliation breaks opened, by kind. A break that a later run sees again is not counted again.",
    ["kind"],
)
