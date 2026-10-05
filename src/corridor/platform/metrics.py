"""Prometheus metrics.

Metrics are defined here, in one place, so the full set is visible and no name is declared
twice. They live in the default registry and are exposed at ``/metrics``.
"""

from prometheus_client import Counter

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
