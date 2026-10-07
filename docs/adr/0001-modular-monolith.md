# 0001. Modular monolith with enforced boundaries

**Status:** accepted

## Context

Corridor has about fifteen areas of responsibility: a ledger, identity, payments, FX, risk,
reconciliation, agents and so on. Most money movements touch several of them and must
commit together. A transfer writes a journal entry, a transfer row, a usage row, an outbox
event and an audit event, and either all of them exist or none does.

Separate services would each own a database. A movement would then need a distributed
transaction or a saga for every step, before there is any load that requires the split.
One undivided codebase has the opposite problem: nothing stops any module from reading
any table, and the lines along which it could later be split are never drawn.

## Decision

Build one deployable codebase, `corridor`, that runs as two processes (API and worker)
against one PostgreSQL database, and enforce the module boundaries mechanically.

- Modules are arranged in layers. A module imports only from layers below it, and modules
  in the same layer do not import each other.
- A module's tables are private. Other modules call its service functions and receive
  frozen dataclasses. There are no foreign keys between modules.
- Entry points (an HTTP handler, an outbox handler, a scheduled job) own the transaction
  and pass the session down. Service functions never commit.
- `import-linter` contracts encode the layers and the private `models` modules.
  `uv run poe contracts` fails on a violation, and CI runs it.

## Consequences

- One database transaction per unit of work. No distributed transactions.
- One thing to deploy, and one place to read a movement from start to finish.
- A module can be extracted later along an existing line. The ledger is the first
  candidate: it depends on nothing but `platform`.
- Cross-module references are opaque ids, so the database does not check that, for example,
  a wallet's `user_id` names a user. The application and the tests carry that.
- The layering sometimes decides where code lives. Pagination helpers are in `platform`
  because modules below the API return pages; `agents` sits above `payments` because an
  approved request executes a transfer.
- One exception is recorded in the code: the worker purges the API's `idempotency_keys`
  table with one plain SQL statement, because scheduled work must not run in the API.
