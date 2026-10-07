# Decision records

Each record states the context a decision was made in, the decision, and what follows from
it. The architecture's [decision log](../architecture.md#20-decision-log) lists them with
their alternatives.

| # | Decision |
|---|---|
| [0001](0001-modular-monolith.md) | Modular monolith with enforced boundaries |
| [0002](0002-integer-minor-units.md) | Integer minor units for money |
| [0003](0003-append-only-ledger-with-triggers.md) | Append-only double-entry ledger, enforced by database triggers |
| [0004](0004-cached-balances-for-constrained-accounts.md) | Cached balances only for accounts that must not go negative |
| [0005](0005-read-committed-with-ordered-locks.md) | READ COMMITTED with ordered explicit locks and a per-user money-out lock |
| [0006](0006-idempotency-in-postgresql.md) | Idempotency keys in PostgreSQL, committed with the effect |
| [0007](0007-outbox-over-broker.md) | Transactional outbox instead of a message broker |
| [0008](0008-webhooks-stored-then-processed.md) | Webhooks are verified, stored and acknowledged before they are processed |
| [0009](0009-holds-as-ledger-movements.md) | Withdrawal holds are ledger movements |
| [0010](0010-submitting-state-before-provider-call.md) | A `submitting` state, committed before the provider is called |
| [0011](0011-refusal-check-before-release.md) | A provider's refusal is checked against what the provider holds before funds are released |
| [0012](0012-limits-in-usd-with-usage-rows.md) | Limits in USD at reference rates, with one usage row per movement |
| [0013](0013-deny-by-default-agent-policy.md) | Agents get scoped keys, a deny-by-default spend policy and an approval threshold |
| [0014](0014-authenticated-rate-cache.md) | FX rates cached in Redis carry an HMAC |
| [0015](0015-fail-closed-money-writes-on-redis-loss.md) | Redis is never a source of truth, and money writes fail closed when it is down |
| [0016](0016-simulators-as-separate-package.md) | Simulated providers in a separate package, behind a written contract |
| [0017](0017-hand-written-migrations.md) | Hand-written migrations, with triggers and least-privilege grants |
| [0018](0018-access-tokens-checked-per-request.md) | ES256 access tokens, checked against the database on every request |
