# 0015. Redis is never a source of truth, and money writes fail closed when it is down

**Status:** accepted

## Context

Redis holds rate-limit buckets, a rate cache and session revocation marks. It can be
flushed or lost at any moment. Two rules follow, and they pull in different directions.

The first is that nothing in Redis may be needed to compute a balance, authorise a debit or
prevent a duplicate. That is why idempotency keys and limits live in PostgreSQL.

The second is about what to do when Redis cannot be reached. A rate limiter that fails open
keeps the service up and stops limiting. For most requests that is right. For a request
that moves money or calls a provider it means an unbounded number of uncounted attempts,
exactly when part of the system is unhealthy.

## Decision

- Redis is never a source of truth for money, limits or idempotency.
- Every use of Redis goes through one helper that takes a default for "Redis is
  unavailable" and counts each failure in a metric.
- Rate limits by client address fail **open**. So does the per-actor limit on money reads.
- The per-actor limit on money writes fails **closed**: the request is refused with
  `503 rate_limiter_unavailable` and `Retry-After`, and nothing is done.
- The FX cache falls back to asking the rate source. The revocation mark is skipped, and
  the per-request check against PostgreSQL still refuses closed accounts and ended tokens.

## Consequences

- Losing Redis loses no money and corrupts nothing.
- Losing Redis makes transfers, withdrawals, conversions and beneficiary creation
  unavailable until it is back. Reads, logins and webhook intake keep working, and the
  worker, which does not use Redis, keeps settling what is in flight.
- `/readyz` reports `degraded`, not down, when Redis is unreachable. An operator has to
  know that `degraded` means money-moving requests are being refused.
- Refused requests are safe to retry with the same idempotency key.
- Redis availability now matters for the main user actions. The deployment uses a managed
  Redis for that reason.
