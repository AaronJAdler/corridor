# Corridor

Wallet backend: a FastAPI modular monolith on PostgreSQL and Redis with a double-entry
ledger. PostgreSQL is the only source of truth. Providers are simulated in `src/corridor_sim`.

- Design, as built: [docs/architecture.md](docs/architecture.md). Decisions: [docs/adr/](docs/adr/README.md)
- API: [docs/api.md](docs/api.md). Operations: [docs/runbook.md](docs/runbook.md)
- Provider contract: [docs/provider-api.md](docs/provider-api.md)
- Work plan and progress: [plans/pending/corridor-v1.md](plans/pending/corridor-v1.md)

## Commands

Python 3.14 and [uv](https://docs.astral.sh/uv/). The commands are the same in PowerShell
and in a Unix shell.

```text
uv sync --frozen            install exactly what the lockfile says
uv run poe check            lint, typecheck, contracts, the suite with coverage, the thresholds
uv run poe lint             ruff check and ruff format --check
uv run poe format           apply lint fixes and formatting
uv run poe typecheck        mypy --strict over src
uv run poe contracts        module boundaries (import-linter)
uv run poe test             the test suite (roughly a quarter of an hour)
uv run pytest tests/ledger -q          one directory (add -k name for one test)
uv run poe test-cov         the suite, recording coverage; then `uv run poe coverage` fails
                            under 94% overall or 96% on ledger, payments, fx, risk
uv run poe smoke            start the API as a real process and probe it
uv run poe e2e              API, worker and simulators as processes; a scripted scenario
uv run poe demo             the same stack, narrating a deposit, conversion, transfer, withdrawal
uv run poe audit            pip-audit over the installed dependencies
uv run poe lint-docs        links, diagrams and docs/openapi.json against the code
uv run poe lint-docker      also lint-compose (needs Docker), lint-ci, lint-infra (needs Terraform)
uv run corridor serve       run the API (needs CORRIDOR_DATABASE_URL, CORRIDOR_REDIS_URL, a signing key)
uv run corridor worker      run the worker
uv run corridor db migrate  apply migrations (needs CORRIDOR_DATABASE_OWNER_URL)
uv run corridor keys generate --out DIR     generate a signing key pair
uv run corridor verify-ledger               recompute the ledger's invariants; exit 1 on a finding
uv run python scripts/lint_docs.py --write-openapi    regenerate docs/openapi.json after an API change
```

Tests, `smoke`, `e2e` and `demo` need a real PostgreSQL 16+ and Redis 7+:

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
   A correction is a new entry. Transfers, conversions, beneficiaries and deposit
   instructions are write-once.
4. **No network call, and no password hashing, inside a database transaction.** Commit
   intent, call the provider from the worker, commit the result in a second transaction.
5. **Redis is never the source of truth** for money, limits or idempotency. Every Redis use
   goes through `RedisStore.attempt` with a stated behaviour for when Redis is down.
6. **Entry points own the transaction.** An HTTP handler, an outbox handler or a scheduled job
   opens one transaction and passes the session down. Service functions take a session and
   never commit. A refusal that must leave a record is returned and raised after the commit.
7. **Locks are taken in one order** (next section).
8. **Tables are private to their module.** Other modules call the owner's service functions.
   Nothing imports another module's `models`.
9. **No secrets in the repository, in logs or in test fixtures.** Synthetic data only.
10. **Funds are given back without asking the provider only from `held`.** Once a
    withdrawal is `submitting`, a refusal is checked against what the provider holds.
11. **Money leaves suspense only under a lock on its deposit**, whose status must be
    `suspense`, in the transaction that posts the entry.

## Lock order

1. The idempotency key: an advisory lock on the actor and the key (`api/idempotency.py`).
2. Per-user money-out advisory locks (`risk.MONEY_OUT_LOCK`), in ascending key order
   (`platform.db.advisory_xact_lock` sorts them). Every path that takes money out of a
   wallet takes the owner's lock first: transfer, withdrawal, conversion, approving an
   agent's request, an adjustment that debits a user, a returned deposit, restricting a user.
3. The business row (`SELECT … FOR UPDATE`): withdrawal, deposit, quote, approval request,
   adjustment.
4. Balance rows (`FOR NO KEY UPDATE`) in ascending account id, inside `ledger.post_entry`.

## Conventions

- **Layers.** `api | worker` > `ops` > `webhooks | recon | agents` > `payments | fx` >
  `wallets | risk` > `ledger | identity | providers | outbox | audit` > `platform`. A module
  imports only from layers below it; siblings do not import each other. `corridor` and
  `corridor_sim` never import each other. `uv run poe contracts` enforces this. Imports
  are absolute; ruff refuses a relative one.
- **Module shape.** `models.py` (private tables), `types.py` (frozen dataclasses handed to
  callers), `errors.py`, service modules, and an `__init__.py` that exports with `__all__`.
- **Time.** `corridor.platform.clock.utcnow()` only, passed into SQL as a parameter. Never
  `datetime.now()` or SQL `now()` in application queries. Tests move the clock; none sleeps.
- **Ids.** UUIDv7 from `corridor.platform.ids.new_id()`. The caller of a money movement
  makes its id, a new one per attempt.
- **Errors.** A refusal a client can cause is a `DomainError` subclass with a stable `code`
  and `status`; the API renders it as an RFC 9457 problem document. A broken contract
  between modules is an ordinary exception. Never return an error body by hand.
- **SQL.** SQLAlchemy 2.1 Core-style statements, async sessions, no relationships, no lazy
  loading. `MinorUnits` for amounts. Named constraints (`pk_`, `uq_`, `fk_`, `ix_`, `ck_`).
  `timestamptz` everywhere. `text` plus `CHECK` instead of enum types. No column defaults.
- **Migrations.** Hand-written SQL, one statement per `op.execute`, `NNNN_name.py`, a real
  `downgrade`. Triggers and grants are part of the migration: revoke what the application
  role must not do, and grant `UPDATE` on named columns only.
- **Authorisation.** Routes take `require(scope)`, `CurrentPrincipal` or `AdminPrincipal`;
  only `require` admits an agent key. Services check the scope again and answer another
  user's resource as not found.
- **Logging.** `corridor.platform.logging.get_logger(__name__)`, dotted event names, never
  a secret, token, password, hash, body or full account number.
- **Tests.** Real PostgreSQL and Redis; each test gets its own database. Test names are
  sentences. A guard on a money path needs a test that fails when the guard is removed. A
  test that cannot run is reported as not run, never as passed.
- **Docs.** An API change needs `docs/openapi.json` regenerated and `docs/api.md` updated;
  `uv run poe lint-docs` fails otherwise.

## Git

- One branch per unit of work; never commit directly on `main`. History stays linear.
- Stage explicit paths.
- Commits and pull requests are authored by the repository owner and carry no co-author or
  tool-attribution lines.
