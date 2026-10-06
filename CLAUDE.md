# Corridor

Wallet backend: a FastAPI modular monolith on PostgreSQL and Redis with a double-entry
ledger. PostgreSQL is the only source of truth. Providers are simulated in `src/corridor_sim`.

- Design: [docs/architecture.md](docs/architecture.md)
- Work plan and progress: [plans/pending/corridor-v1.md](plans/pending/corridor-v1.md)

## Commands

Python 3.14 and [uv](https://docs.astral.sh/uv/). The commands are the same in PowerShell
and in a Unix shell.

```text
uv sync --frozen          install exactly what the lockfile says
uv run poe check          every gate below, in order
uv run poe lint           ruff check and ruff format --check
uv run poe format         apply lint fixes and formatting
uv run poe typecheck      mypy --strict over src
uv run poe contracts      module boundaries (import-linter)
uv run poe test           the test suite
uv run pytest tests/ledger -q        one directory
uv run pytest -k name -q             one test
uv run poe smoke          start the API as a real process and probe it
uv run corridor serve     run the API (needs CORRIDOR_DATABASE_URL and CORRIDOR_REDIS_URL)
uv run corridor db migrate           apply migrations (needs CORRIDOR_DATABASE_OWNER_URL)
```

Tests and the smoke test need a real PostgreSQL 16+ and Redis 7+. Point them at both with
two environment variables:

```text
CORRIDOR_TEST_POSTGRES_ADMIN_URL   postgresql://USER[:PASSWORD]@HOST:PORT/postgres  (a role that can create databases and roles)
CORRIDOR_TEST_REDIS_URL            redis://HOST:PORT/0
CORRIDOR_TEST_POSTGRES_CLONE_STRATEGY   optional: FILE_COPY is faster on a throwaway server with fsync off
```

## Rules that must never be broken

1. **No floating point for money.** Amounts are integer minor units (`NUMERIC(38,0)` mapped
   to `int`). The API carries decimal strings. Parse and format only through
   `corridor.platform.money`.
2. **Every balance change goes through `ledger.post_entry`.** No other code writes
   `journal_entries`, `postings` or `account_balances`.
3. **Journal entries, postings and audit events are append-only.** No `UPDATE`, no `DELETE`.
   A correction is a new entry.
4. **No network call inside a database transaction.** Commit intent, call the provider from
   the worker, commit the result in a second transaction.
5. **Redis is never the source of truth** for money, limits or idempotency. Every Redis use
   has a defined behaviour for when Redis is down.
6. **Entry points own the transaction.** An HTTP handler, an outbox handler or a scheduled job
   opens one transaction and passes the session down. Service functions take a session and
   never commit.
7. **Locks are taken in one order:** idempotency key, then per-user money-out locks in
   ascending order, then the business row (`FOR UPDATE`), then balance rows (`FOR NO KEY UPDATE`) in
   ascending account id.
8. **Tables are private to their module.** Other modules call the owner's service functions.
   Nothing imports another module's `models`.
9. **No secrets in the repository, in logs or in test fixtures.** Synthetic data only.

## Conventions

- **Layers.** `api | worker` > `ops` > `webhooks | recon | agents` > `payments | fx` >
  `wallets | risk` > `ledger | identity | providers | outbox | audit` > `platform`. A module
  imports only from layers below it; siblings do not import each other. `uv run poe contracts`
  enforces this.
- **Time.** Business time comes from `corridor.platform.clock.utcnow()` and is passed into
  SQL as a parameter, so tests can control it. Timestamps are timezone-aware UTC.
- **Ids.** UUIDv7 from `corridor.platform.ids.new_id()`.
- **Errors.** Raise a `DomainError` subclass with a stable `code`. The API renders it as an
  RFC 9457 problem document. Never return an error body by hand.
- **SQL.** SQLAlchemy 2.1 Core-style statements, async sessions, no lazy loading and no ORM
  relationships. Named constraints. `timestamptz` everywhere. `text` plus `CHECK` instead of
  enum types.
- **Migrations.** Hand-written Alembic revisions, one per module, numbered `NNNN_name.py`.
  Triggers and grants are part of the migration.
- **Tests.** Real PostgreSQL and Redis; each test gets its own database cloned from a
  migrated template. A guard on a money path needs a test that fails when the guard is
  removed. A test that cannot run is reported as not run, never as passed.
- **Imports.** Absolute imports only.

## Git

- One branch per unit of work; never commit directly on `main`. History stays linear.
- Stage explicit paths.
- Commits and pull requests are authored by the repository owner and carry no co-author or
  tool-attribution lines.
