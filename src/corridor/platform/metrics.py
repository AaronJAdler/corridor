"""Prometheus metrics.

Metrics are defined here, in one place, so the full set is visible and no name is declared
twice. They live in the default registry and are exposed at ``/metrics``.
"""

from prometheus_client import Counter, Gauge, Histogram

# Labelled with the route's template and never the path a client sent: a path holds ids,
# and every id would be a time series of its own.
HTTP_REQUESTS = Counter(
    "corridor_http_requests_total",
    "Requests answered, by method, route template and status. Errors are the 4xx and 5xx.",
    ["method", "route", "status"],
)

HTTP_REQUEST_SECONDS = Histogram(
    "corridor_http_request_duration_seconds",
    "How long a request took to answer, by method and route template.",
    ["method", "route"],
)

LEDGER_ENTRIES = Counter(
    "corridor_ledger_entries_total",
    "Journal entries written, by kind. Counted when written: one whose transaction is then"
    " rolled back is counted all the same.",
    ["kind"],
)

LEDGER_VERIFIER_FINDINGS = Gauge(
    "corridor_ledger_verifier_findings",
    "What the last run of the ledger verifier found wrong. Anything but zero needs a person.",
)

LEDGER_VERIFIER_LAST_RUN = Gauge(
    "corridor_ledger_verifier_last_run_timestamp_seconds",
    "When the ledger verifier last finished a run, as a Unix time.",
)

WEBHOOK_DELIVERIES = Counter(
    "corridor_webhook_deliveries_total",
    "Webhook deliveries received, by provider and outcome (accepted, duplicate,"
    " bad_signature, malformed).",
    ["provider", "outcome"],
)

WEBHOOK_EVENTS_PROCESSED = Counter(
    "corridor_webhook_events_processed_total",
    "Stored webhook events the worker finished with, by provider, type and outcome"
    " (processed, ignored).",
    ["provider", "type", "outcome"],
)

PROVIDER_CALLS = Counter(
    "corridor_provider_calls_total",
    "Calls to a provider, by provider, operation and outcome (ok, rejected, unknown,"
    " misconfigured). Errors are every outcome but ok.",
    ["provider", "operation", "outcome"],
)

PROVIDER_CALL_SECONDS = Histogram(
    "corridor_provider_call_duration_seconds",
    "How long a call to a provider took, by provider and operation.",
    ["provider", "operation"],
)

RECON_OPEN_BREAKS = Gauge(
    "corridor_recon_open_breaks",
    "Reconciliation breaks nobody has resolved, as of the last reconciliation run.",
)

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

RECON_BREAK_CHANGES = Counter(
    "corridor_recon_break_changes_total",
    "Open reconciliation breaks whose difference a later run found changed, by kind.",
    ["kind"],
)
