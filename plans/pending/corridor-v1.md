# Plan: Corridor v1

| | |
|---|---|
| **Status** | Approved 2026-10-05. In progress. |
| **Created** | 2026-10-05 |
| **Design** | [docs/architecture.md](../../docs/architecture.md) |
| **Tracker** | This file. There is no external ticket system for this project. |

Build the wallet backend described in the architecture document: a FastAPI modular monolith
on PostgreSQL and Redis with a double-entry ledger, idempotent money movement, asynchronous
provider integration, reconciliation, agent spending controls and an AWS deployment
definition.

## Progress

Updated in the same change that lands each phase.

| Phase | Title | Steps | Status | Verified by |
|---|---|---|---|---|
| 0 | Foundation | 4 | **done** 2026-10-05 | `uv run poe check` (137 tests), `uv run poe smoke` |
| 1 | Ledger | 4 | **done** 2026-10-05 | `uv run poe check` (248 tests); S2 observed: 200 debits, 50 succeed |
| 2 | Identity | 5 | **done** 2026-10-06 | `gate.sh tests` on the combined tree |
| 3 | Async backbone | 2 | **done** 2026-10-06 | `gate.sh tests` on the combined tree |
| 4 | Wallets and transfers | 4 | **done** 2026-10-06 | `gate.sh tests` on the combined tree |
| — | **Checkpoint A: core** | | **reached** 2026-10-06 | two independent reviews, no Critical or High; Required and Medium findings fixed |
| 5 | Providers and simulators | 5 | **done** 2026-10-06 | `gate.sh tests` on the combined tree |
| 6 | Inbound money | 5 | **done** 2026-10-06 | `gate.sh tests`: 2772 passed |
| 7 | Outbound money | 6 | **done** 2026-10-06 | `gate.sh tests`: 2772 passed |
| — | **Checkpoint B: money in and out** | | **reached** 2026-10-06 | S4 observed: failure-injection suite, one payout per withdrawal in every case |
| 8 | FX | 3 | **done** 2026-10-06 | `gate.sh tests`: 2772 passed |
| 9 | Risk | 4 | not started | |
| 10 | Reconciliation and operations | 4 | not started | |
| 11 | Agents | 4 | not started | |
| — | **Checkpoint C: feature complete** | | | |
| 12 | Hardening | 5 | not started | |
| 13 | Local stack | 4 | not started | |
| 14 | Deployment | 6 | not started | |
| 15 | Documentation | 5 | not started | |
| 16 | Final verification | 3 | not started | |

## Success criteria

S1 to S8 in [section 1 of the architecture](../../docs/architecture.md#success-criteria).
Each is observed by a named command before the plan is closed.

## Decisions awaiting the owner

Asked once, together, before any code was written. Answered 2026-10-05.

| # | Question | Recommendation | Answer |
|---|---|---|---|
| Q1 | Where should the project live? | `source\repos\corridor` on the owner's computer | `source\repos\corridor` |
| Q2 | Which tool defines the AWS infrastructure? | Terraform | Terraform |
| Q3 | Build cadence | Straight through, reporting at each checkpoint | Straight through |
| Q4 | How are commits attributed? | The owner's standing git rule | Authored as the owner, no co-author lines |

**Q2 in detail.** Terraform is the more widely used tool and the more transferable skill.
It cannot be validated in the build workspace: the Terraform binary and the AWS provider are
not reachable from there, so it would be checked by an HCL parser, a security scanner and a
cross-check of every argument against the provider's documentation, and first validated for
real by `terraform validate` on the owner's machine or in CI. AWS CDK in Python can be
synthesised, linted against CloudFormation's schemas and unit-tested in the workspace, at the
cost of being specific to AWS. Neither would have been applied to a real account.

## Assumptions

Each stands unless corrected at sign-off.

1. The project is named **Corridor**; the Python package is `corridor`.
2. "Use Claude Code" means the build happens in this Claude Code session. The repository
   carries a `CLAUDE.md` so work can continue in Claude Code on the owner's machine.
3. Bank, custody and FX providers are **simulated inside the repository**. Real sandbox
   adapters are a later addition behind the same interfaces.
4. Python 3.14, PostgreSQL 16, Redis 7, and the library versions in section 19 of the
   architecture.
5. Commit attribution follows the owner's answer to Q4.
6. MIT licence.
7. The owner runs the one-command stack with Docker Desktop on Windows.
8. Repository documents describe the system on its own terms.
9. Nothing is deployed and no money is spent. AWS definitions are written and statically
   checked only.
10. All data is synthetic.

**Out of scope:** a frontend, real providers, real identity verification, MFA and email
verification, cards, lending, multi-region, and any deployment to a cloud account.

## How the work is executed

- **Order.** Risk first. The ledger and its concurrency behaviour are proven before anything
  is built on them. Each step is a vertical slice with its own test.
- **Roles.** The orchestrating session writes the foundation (phases 0 to 4), integrates
  every phase, runs every verification itself and makes every commit. From phase 5 on,
  independent slices are delegated to sub-agents with decision-complete briefs and disjoint
  file sets. A sub-agent never commits, and its reported result is re-run before it counts.
- **Parallel lanes.** Phase 5 simulators alongside phase 4. Phase 8 alongside phases 6 and 7.
  Phases 10 and 11 together. Phases 13, 14 and 15 together.
- **Migrations.** One revision per module. The orchestrator assigns every revision id and
  its parent, so parallel work cannot fork the chain. Revisions may be edited in place until
  the `v1.0.0` tag and are append-only after it.
- **Git.** One branch per phase, named `phase-N-slug`. `main` is fast-forwarded to a phase
  only after that phase's verification passes. History stays linear. Paths are staged
  explicitly. There is no remote until the owner adds one.
- **Checkpoints.** At A, B and C the full suite and the ledger verifier run, the progress
  table is updated, the project is synced to the owner's folder, and a short status goes to
  the owner.
- **Gate.** A failing check blocks the next step. A check that cannot run in the workspace is
  reported as not run, never as passed.

## Workspace facts

Verified on 2026-10-05 by the command shown. These bound what "verified" can mean.

| Fact | Evidence |
|---|---|
| Python 3.14.6 is installable | `uv python install 3.14` |
| PostgreSQL 16.15 runs locally | `initdb` and `pg_ctl start` as the `postgres` user, `select version()` |
| Redis 7.0.15 runs locally | `redis-server`, `redis-cli ping` |
| Every native dependency has a Windows wheel for Python 3.14 | PyPI file listings for asyncpg, pydantic-core, cryptography, argon2-cffi-bindings, greenlet, httptools |
| No container registry is reachable, so images cannot be built | `curl` to Docker Hub, GHCR, ECR Public, Quay, MCR and GCR was refused by the egress policy |
| The Go module proxy and the Terraform and OpenTofu registries are refused | `go install` and `curl` returned the egress policy's 403 |
| PyPI and npm are reachable | `uv pip compile`, `npm view` |
| Public GitHub repositories can be read with git | `git ls-remote https://github.com/actions/checkout.git` |
| No GitHub account is linked to this session | repository listing returned "no GitHub account linked" |
| The owner's computer is Windows with no folder connected yet; `source\repos` exists | device info and a names-only listing |

Consequences: container images, GitHub Actions runs, the AWS definitions and native Windows
execution are **not** verifiable here. The plan substitutes real-process runs of the same
commands, static linters, and CI definitions that will run on the owner's first push.

## Always and never

**Always**

- Every balance change goes through `ledger.post_entry`.
- Every entry point (HTTP handler, outbox handler, scheduled job) owns its transaction and
  passes the session down.
- Tests that touch data run against real PostgreSQL and Redis.
- A guard on a money path has a test that fails when the guard is removed.
- Locks are taken in the order given in section 5 of the architecture.
- The progress table and this file change in the same commit as the work they describe.

**Never**

- Floating point for money.
- `UPDATE` or `DELETE` on journal entries, postings or audit events.
- A network call inside a database transaction.
- Redis as the source of truth for money, limits or idempotency.
- A real provider call, a real credential or real personal data.
- A secret in the repository, a brief, a log line or a test fixture.
- An invented version, commit SHA or image digest. If it cannot be looked up, it is left
  unpinned and listed as such.
- A push, a deployment or any cloud resource.
- Work outside the phases below without recording it here first.

---

## Phases

Sizes: XS under 3 files, S 3 to 6, M 7 to 12. Nothing larger than M is taken as one step.

### Phase 0: Foundation

#### 0.1 Project scaffold · S
- **Files:** `pyproject.toml`, `uv.lock`, `.python-version`, `.gitignore`, `.gitattributes`,
  `.editorconfig`, `LICENSE`, `README.md` (stub), `CLAUDE.md`, `src/corridor/__init__.py`
- **Done when:** `uv sync --frozen` installs on Python 3.14; lint, type check and module
  contracts run green on the skeleton; every dependency is listed with its reason.
- **Verify:** `uv run poe lint && uv run poe typecheck && uv run poe contracts`

#### 0.2 Platform core · M
- **Files:** `src/corridor/platform/{config,db,redis,logging,errors,ids,money,clock}.py`,
  `tests/platform/*`
- **Done when:** money parses and formats exactly for every asset scale (property test); the
  transaction helper retries deadlock and serialisation errors at most three times; settings
  load from the environment with no secret defaults.
- **Verify:** `uv run pytest tests/platform -q`

#### 0.3 Test harness · S
- **Files:** `tests/conftest.py`, `tests/support/{postgres,redis,factories}.py`,
  `migrations/{env.py,script.py.mako}`, `migrations/versions/0001_baseline.py`,
  `alembic.ini`
- **Done when:** test databases are cloned from a migrated template; a drift test fails when
  models and migrations disagree; the session time zone is UTC in every connection.
- **Verify:** `uv run pytest tests/harness -q`

#### 0.4 App shell · S · after 0.2
- **Files:** `src/corridor/api/{app,deps,errors,health,middleware}.py`, `src/corridor/cli.py`,
  `tests/api/test_shell.py`, `scripts/smoke.py`
- **Done when:** a real server process answers `/healthz` and `/readyz`; errors are RFC 9457
  bodies with a request id; `/readyz` reports Redis as degraded rather than failing.
- **Verify:** `uv run pytest tests/api/test_shell.py -q && uv run poe smoke`

### Phase 1: Ledger

#### 1.1 Ledger schema · M
- **Files:** `src/corridor/ledger/models.py`, `migrations/versions/0002_ledger.py`,
  `tests/ledger/test_schema.py`
- **Done when:** the database itself rejects an unbalanced entry at commit, any update,
  delete or truncate of journal tables, and a posting whose asset differs from its account;
  the application role has no `UPDATE` or `DELETE` grant on those tables.
- **Verify:** `uv run pytest tests/ledger/test_schema.py -q`

#### 1.2 Posting service · M · after 1.1
- **Files:** `src/corridor/ledger/{service,types,errors,__init__}.py`,
  `tests/ledger/test_posting.py`
- **Done when:** posting is atomic and returns the existing entry for a repeated source;
  insufficient funds is raised before any write; balances and `balance_after` are exact.
- **Verify:** `uv run pytest tests/ledger/test_posting.py -q`

#### 1.3 Ledger verifier · S · after 1.2
- **Files:** `src/corridor/ledger/verify.py`, `src/corridor/cli.py`,
  `tests/ledger/test_verify.py`
- **Done when:** a clean ledger yields no findings; each kind of injected violation is
  detected; the command exits non-zero on any finding.
- **Verify:** `uv run pytest tests/ledger/test_verify.py -q && uv run corridor verify-ledger`

#### 1.4 Ledger under contention · S · after 1.2
- **Files:** `tests/ledger/test_concurrency.py`, `tests/ledger/test_properties.py`
- **Done when:** 200 concurrent debits against a balance that affords 50 give exactly 50
  successes (S2); opposing transfers between two accounts never deadlock; a generated
  sequence of operations leaves the verifier clean.
- **Verify:** `uv run pytest tests/ledger/test_concurrency.py tests/ledger/test_properties.py -q`

### Phase 2: Identity

#### 2.1 Users and passwords · S
- **Files:** `src/corridor/identity/{models,passwords,service,errors}.py`,
  `migrations/versions/0003_identity.py`, `tests/identity/test_passwords.py`
- **Done when:** registration stores an Argon2id hash; login for an unknown email performs
  one hash verification; repeated failures lock the account for a growing interval.
- **Verify:** `uv run pytest tests/identity/test_passwords.py -q`

#### 2.2 Tokens · M · after 2.1
- **Files:** `src/corridor/identity/{tokens,keys,principal}.py`, `src/corridor/cli.py`,
  `tests/identity/test_tokens.py`
- **Done when:** access tokens verify with exactly one algorithm and reject wrong issuer,
  audience and expiry; refresh tokens rotate, and reuse revokes the family; keys carry a
  `kid` and a second key verifies during rotation.
- **Verify:** `uv run pytest tests/identity/test_tokens.py -q`

#### 2.3 Audit log · S
- **Files:** `src/corridor/audit/{models,service,__init__}.py`,
  `migrations/versions/0004_audit.py`, `tests/audit/test_audit.py`
- **Done when:** events are written in the caller's transaction; the table rejects update
  and delete; an event records actor, principal, action, resource, outcome and request id.
- **Verify:** `uv run pytest tests/audit -q`

#### 2.4 Rate limiting · S
- **Files:** `src/corridor/platform/ratelimit.py`, `src/corridor/api/middleware.py`,
  `tests/platform/test_ratelimit.py`
- **Done when:** the token bucket is atomic under concurrent callers; a limited request gets
  `429` with `Retry-After`; with Redis stopped the request is allowed and a metric counts it.
- **Verify:** `uv run pytest tests/platform/test_ratelimit.py -q`

#### 2.5 Auth wiring · S · after 2.2, 2.3, 2.4
- **Files:** `src/corridor/api/{deps,routers/auth}.py`, `tests/api/test_auth.py`,
  `tests/api/test_route_invariants.py`
- **Done when:** register, login, refresh, logout and `GET /v1/me` work end to end; a test
  over the route table fails if any non-public route lacks the auth dependency; removing the
  dependency from one route turns that test red.
- **Verify:** `uv run pytest tests/api/test_auth.py tests/api/test_route_invariants.py -q`

### Phase 3: Async backbone

#### 3.1 Outbox · M
- **Files:** `src/corridor/outbox/{models,service,dispatcher,retry,__init__}.py`,
  `migrations/versions/0005_outbox.py`, `tests/outbox/*`
- **Done when:** two dispatchers never process one event at the same time; a failed event
  backs off, and after eight attempts is marked dead; an expired claim is picked up again.
- **Verify:** `uv run pytest tests/outbox -q`

#### 3.2 Worker runtime · S · after 3.1
- **Files:** `src/corridor/worker/{main,scheduler}.py`, `src/corridor/cli.py`,
  `tests/worker/*`
- **Done when:** a worker process drains events and wakes on `NOTIFY`; a scheduled job runs
  on one instance only; the process finishes in-flight work and exits cleanly on shutdown.
- **Verify:** `uv run pytest tests/worker -q`

### Phase 4: Wallets and transfers

#### 4.1 Wallet provisioning · S
- **Files:** `src/corridor/wallets/{models,service,__init__}.py`,
  `migrations/versions/0006_wallets.py`, `src/corridor/api/routers/wallets.py`,
  `tests/wallets/test_provisioning.py`
- **Done when:** registering a user opens available and held accounts for each asset exactly
  once; `GET /v1/wallets` returns available, held and total per asset as decimal strings.
- **Verify:** `uv run pytest tests/wallets/test_provisioning.py -q`

#### 4.2 Statements · S · after 4.1
- **Files:** `src/corridor/wallets/statements.py`, `src/corridor/api/pagination.py`,
  `tests/wallets/test_statements.py`
- **Done when:** entries page by keyset cursor with no duplicate or missing row while new
  postings arrive; a cursor from another account is rejected; limit is capped at 200.
- **Verify:** `uv run pytest tests/wallets/test_statements.py -q`

#### 4.3 Idempotency layer · M
- **Files:** `src/corridor/api/idempotency.py`, `migrations/versions/0007_idempotency.py`,
  `tests/api/test_idempotency.py`
- **Done when:** 50 concurrent identical requests produce one effect and 50 identical
  responses (S3); a reused key with a different body gets `422`; an unexpected error leaves
  no key behind.
- **Verify:** `uv run pytest tests/api/test_idempotency.py -q`

#### 4.4 Transfers · M · after 4.1, 4.3
- **Files:** `src/corridor/payments/{models,transfers,fees,errors,__init__}.py`,
  `src/corridor/risk/{service,__init__}.py` (authorisation hook only),
  `migrations/versions/0008_payments.py`, `src/corridor/api/routers/transfers.py`,
  `tests/payments/test_transfers.py`
- **Done when:** a transfer posts one balanced entry with its fee, an outbox event and an
  audit event in one transaction; transfers to self, to an unknown user or beyond the
  balance are refused with stable codes; concurrent transfers by one sender in two assets
  are serialised by the per-user lock.
- **Verify:** `uv run pytest tests/payments/test_transfers.py -q`

### Checkpoint A: core

`uv run poe check` and `uv run corridor verify-ledger` are clean. S1, S2 and S3 are met.

### Phase 5: Providers and simulators

#### 5.1 Provider ports · S
- **Files:** `src/corridor/providers/{ports,http,errors,bank_rail,custody,fx_rates,__init__}.py`,
  `tests/providers/test_adapters.py`
- **Done when:** each adapter sends an idempotency key on every mutating call; a timeout
  surfaces as "outcome unknown", distinct from a rejection; responses are schema-validated.
- **Verify:** `uv run pytest tests/providers -q`

#### 5.2 Bank-rail simulator · M
- **Files:** `src/corridor_sim/{app,state,bank}.py`, `tests/simulators/test_bank.py`
- **Done when:** virtual accounts, beneficiary tokens, payouts and statements behave as a
  ledger of their own; a repeated idempotency key returns the first payout; ACH, SPEI and PIX
  settle on different schedules.
- **Verify:** `uv run pytest tests/simulators/test_bank.py -q`

#### 5.3 Custody simulator · M
- **Files:** `src/corridor_sim/custody.py`, `tests/simulators/test_custody.py`
- **Done when:** deposit addresses, confirmations and withdrawals work; a deposit is detected
  first and confirmed after the required block count; a withdrawal reports its network fee.
- **Verify:** `uv run pytest tests/simulators/test_custody.py -q`

#### 5.4 Rate simulator · XS
- **Files:** `src/corridor_sim/fx.py`, `tests/simulators/test_fx.py`
- **Done when:** mid rates are served with a timestamp; the walk is reproducible from a seed.
- **Verify:** `uv run pytest tests/simulators/test_fx.py -q`

#### 5.5 Webhook delivery and failure injection · S · after 5.2, 5.3
- **Files:** `src/corridor_sim/{webhooks,chaos}.py`,
  `tests/simulators/test_webhooks.py`
- **Done when:** webhooks are signed and retried; duplicates, delays, reordering and drops
  can be switched on per test; API errors and timeouts can be injected per endpoint.
- **Verify:** `uv run pytest tests/simulators/test_webhooks.py -q`

### Phase 6: Inbound money

#### 6.1 Webhook ingestion · M · after 3.1
- **Files:** `src/corridor/webhooks/{models,signature,service,__init__}.py`,
  `migrations/versions/0009_webhooks.py`, `src/corridor/api/routers/webhooks.py`,
  `tests/webhooks/*`
- **Done when:** a bad, stale or unsigned request is refused; a repeated event id is
  acknowledged and not reprocessed; the event and its outbox row commit together.
- **Verify:** `uv run pytest tests/webhooks -q`

#### 6.2 Deposit instructions · S · after 5.1
- **Files:** `src/corridor/payments/instructions.py`,
  `src/corridor/api/routers/deposits.py`, `tests/payments/test_instructions.py`
- **Done when:** a user gets one virtual account or address per asset however many times and
  however concurrently they ask; no transaction is open during the provider call.
- **Verify:** `uv run pytest tests/payments/test_instructions.py -q`

#### 6.3 Bank deposits · S · after 6.1, 6.2
- **Files:** `src/corridor/payments/deposits.py`, `tests/payments/test_bank_deposits.py`
- **Done when:** a received deposit credits the user once, however many times the webhook
  arrives; a deposit for an unknown virtual account goes to suspense.
- **Verify:** `uv run pytest tests/payments/test_bank_deposits.py -q`

#### 6.4 On-chain deposits · S · after 6.3
- **Files:** `src/corridor/payments/deposits.py`, `tests/payments/test_chain_deposits.py`
- **Done when:** a detected deposit shows as pending with no ledger effect; confirmation
  credits it; confirmation arriving before detection still credits exactly once.
- **Verify:** `uv run pytest tests/payments/test_chain_deposits.py -q`

#### 6.5 Returned deposits · S · after 6.3
- **Files:** `src/corridor/payments/returns.py`, `tests/payments/test_returns.py`
- **Done when:** a return reverses the credit; a shortfall is booked to the user's
  receivable and the user becomes restricted; a repeated return changes nothing.
- **Verify:** `uv run pytest tests/payments/test_returns.py -q`

### Phase 7: Outbound money

#### 7.1 Beneficiaries · S
- **Files:** `src/corridor/payments/beneficiaries.py`,
  `src/corridor/api/routers/beneficiaries.py`, `tests/payments/test_beneficiaries.py`
- **Done when:** account details go to the provider and only a token and a masked value are
  stored; a user cannot read or use another user's beneficiary.
- **Verify:** `uv run pytest tests/payments/test_beneficiaries.py -q`

#### 7.2 Withdrawal request · M · after 7.1
- **Files:** `src/corridor/payments/{withdrawals,addresses}.py`,
  `src/corridor/api/routers/withdrawals.py`, `tests/payments/test_withdrawal_request.py`
- **Done when:** a request moves amount plus fee from available to held and enqueues
  submission in one transaction; an invalid address or insufficient funds holds nothing; a
  held withdrawal can be cancelled once.
- **Verify:** `uv run pytest tests/payments/test_withdrawal_request.py -q`

#### 7.3 Payout submission · M · after 7.2, 5.1
- **Files:** `src/corridor/payments/handlers.py`, `tests/payments/test_payout_submission.py`
- **Done when:** the handler calls the provider outside any transaction with the withdrawal
  id as the key; a rejection releases the funds; a timeout leaves them held and retries.
- **Verify:** `uv run pytest tests/payments/test_payout_submission.py -q`

#### 7.4 Settlement webhooks · S · after 7.3, 6.1
- **Files:** `src/corridor/payments/handlers.py`, `tests/payments/test_settlement.py`
- **Done when:** a completed payout posts the settle entry with both fees; a failed payout
  releases; a settlement arriving before submission is recorded still completes correctly.
- **Verify:** `uv run pytest tests/payments/test_settlement.py -q`

#### 7.5 Payout sweeper · S · after 7.3
- **Files:** `src/corridor/payments/sweeper.py`, `tests/payments/test_sweeper.py`
- **Done when:** a withdrawal whose webhook never arrives is advanced from the provider's
  API; the sweeper is a no-op on anything already settled.
- **Verify:** `uv run pytest tests/payments/test_sweeper.py -q`

#### 7.6 Failure-injection suite · M · after 7.4, 7.5, 5.5
- **Files:** `tests/chaos/{conftest,test_withdrawal_chaos,test_deposit_chaos}.py`
- **Done when:** with a crash injected at every step, plus timeouts, duplicate, reordered and
  dropped webhooks, each withdrawal ends with exactly one provider payout (S4), every deposit
  is credited exactly once, and the verifier is clean.
- **Verify:** `uv run pytest tests/chaos -q && uv run corridor verify-ledger`

### Checkpoint B: money in and out

`uv run poe check` and the verifier are clean. S4 is met.

### Phase 8: FX

#### 8.1 Rate cache · S · after 5.1
- **Files:** `src/corridor/fx/{rates,__init__}.py`, `tests/fx/test_rates.py`
- **Done when:** rates are cached in Redis and fetched directly when Redis is down; a rate
  older than 15 seconds is refused.
- **Verify:** `uv run pytest tests/fx/test_rates.py -q`

#### 8.2 Quotes · S · after 8.1
- **Files:** `src/corridor/fx/{models,quotes,rounding}.py`,
  `migrations/versions/0010_fx.py`, `tests/fx/test_quotes.py`
- **Done when:** a quote stores both integer amounts and expires in 30 seconds; the buy
  amount always rounds down; for generated amounts and rates a round trip never gains value.
- **Verify:** `uv run pytest tests/fx/test_quotes.py -q`

#### 8.3 Conversions · M · after 8.2, 4.3
- **Files:** `src/corridor/fx/conversions.py`, `src/corridor/api/routers/fx.py`,
  `tests/fx/test_conversions.py`
- **Done when:** a conversion posts one entry balanced in each asset using the stored
  amounts; an expired or already-used quote is refused; another user's quote is refused.
- **Verify:** `uv run pytest tests/fx/test_conversions.py -q`

### Phase 9: Risk

#### 9.1 Limits · M
- **Files:** `src/corridor/risk/{models,limits,service}.py`,
  `migrations/versions/0011_risk.py`, `tests/risk/test_limits.py`
- **Done when:** per-transaction and rolling 24-hour limits apply by tier, user and agent
  with the most specific rule winning; usage is valued in USD across assets; concurrent
  sends in two assets cannot together exceed the daily limit.
- **Verify:** `uv run pytest tests/risk/test_limits.py -q`

#### 9.2 Screening · M · after 9.1
- **Files:** `src/corridor/risk/screening.py`, `src/corridor/payments/{deposits,withdrawals}.py`,
  `tests/risk/test_screening.py`
- **Done when:** a denied party or address is refused before funds move; a review outcome
  routes a deposit to suspense and holds a withdrawal for an operator.
- **Verify:** `uv run pytest tests/risk/test_screening.py -q`

#### 9.3 Restricted users · S · after 9.1
- **Files:** `src/corridor/risk/service.py`, `tests/risk/test_restricted.py`
- **Done when:** a restricted user can receive and cannot transfer, convert or withdraw; the
  check runs in every money-out path, proven by removing it from one and watching that
  path's test fail.
- **Verify:** `uv run pytest tests/risk/test_restricted.py -q`

#### 9.4 KYC tier endpoint · XS · after 9.1
- **Files:** `src/corridor/api/routers/admin.py`, `tests/api/test_admin_kyc.py`
- **Done when:** an admin can change a tier and the change is audited; a non-admin gets 403.
- **Verify:** `uv run pytest tests/api/test_admin_kyc.py -q`

### Phase 10: Reconciliation and operations

#### 10.1 Reconciliation run · M · after 7.4, 6.3
- **Files:** `src/corridor/recon/{models,service,__init__}.py`,
  `migrations/versions/0012_recon.py`, `tests/recon/test_run.py`
- **Done when:** a run over a clean window finds nothing; each break type is detected when
  the simulator is made to disagree; the settlement balance check accounts for in-transit
  items.
- **Verify:** `uv run pytest tests/recon/test_run.py -q`

#### 10.2 Repair and breaks API · S · after 10.1
- **Files:** `src/corridor/recon/repair.py`, `src/corridor/api/routers/admin.py`,
  `tests/recon/test_repair.py`
- **Done when:** a deposit whose webhook was dropped is credited by the run (S5); breaks can
  be listed and resolved with a note; the scheduled run executes on one worker only.
- **Verify:** `uv run pytest tests/recon/test_repair.py -q`

#### 10.3 Dead-letter administration · S · after 3.1
- **Files:** `src/corridor/ops/{service,__init__}.py`, `src/corridor/api/routers/admin.py`,
  `tests/ops/test_dead_letters.py`
- **Done when:** dead events can be listed and requeued by an admin; requeueing is audited.
- **Verify:** `uv run pytest tests/ops/test_dead_letters.py -q`

#### 10.4 Adjustments with dual approval · M
- **Files:** `src/corridor/ops/{models,adjustments}.py`,
  `migrations/versions/0013_ops.py`, `tests/ops/test_adjustments.py`
- **Done when:** an adjustment is a balanced entry requested by one admin and approved by a
  different one; the requester cannot approve their own; suspense funds can be released to a
  user or returned through the same path.
- **Verify:** `uv run pytest tests/ops/test_adjustments.py -q`

### Phase 11: Agents

#### 11.1 Agents and keys · M
- **Files:** `src/corridor/agents/{models,keys,service,__init__}.py`,
  `migrations/versions/0014_agents.py`, `src/corridor/api/{deps,routers/agents}.py`,
  `tests/agents/test_keys.py`
- **Done when:** a key is shown once and stored only as a keyed hash; a revoked, paused or
  expired key is refused; key lookup takes constant time for a wrong secret.
- **Verify:** `uv run pytest tests/agents/test_keys.py -q`

#### 11.2 Scopes on routes · S · after 11.1
- **Files:** `src/corridor/api/deps.py`, `tests/agents/test_scopes.py`
- **Done when:** for every agent-reachable route: no credential gives 401, a key without the
  scope gives 403, a key with only that scope succeeds on its owner's resources and is
  refused on another user's.
- **Verify:** `uv run pytest tests/agents/test_scopes.py -q`

#### 11.3 Spend policy · S · after 11.1, 9.1
- **Files:** `src/corridor/agents/policy.py`, `tests/agents/test_policy.py`
- **Done when:** per-transaction and daily caps and the recipient allow list are enforced
  server-side; audit events name both the agent and its owner.
- **Verify:** `uv run pytest tests/agents/test_policy.py -q`

#### 11.4 Approval requests · M · after 11.3
- **Files:** `src/corridor/agents/approvals.py`, `src/corridor/api/routers/approvals.py`,
  `tests/agents/test_approvals.py`
- **Done when:** a payment above the threshold moves nothing and creates a request; the
  owner's approval executes it exactly once even if approved twice concurrently; an agent
  cannot approve.
- **Verify:** `uv run pytest tests/agents/test_approvals.py -q`

### Checkpoint C: feature complete

`uv run poe check` and the verifier are clean. S5 is met.

### Phase 12: Hardening

#### 12.1 Metrics · S
- **Files:** `src/corridor/platform/metrics.py`, `src/corridor/api/health.py`,
  `tests/platform/test_metrics.py`
- **Done when:** the metrics in section 15 of the architecture are exposed; route labels use
  templates, not raw paths.
- **Verify:** `uv run pytest tests/platform/test_metrics.py -q`

#### 12.2 Redaction and headers · S
- **Files:** `src/corridor/platform/logging.py`, `src/corridor/api/middleware.py`,
  `tests/platform/test_redaction.py`
- **Done when:** a log line carrying a password, token, key or account number shows none of
  it; security headers are present on every response.
- **Verify:** `uv run pytest tests/platform/test_redaction.py -q`

#### 12.3 Assembly and security matrix · M
- **Files:** `tests/assembly/*`, `tests/security/*`
- **Done when:** the route manifest has no duplicate or unauthenticated route; every
  ownership check has a test against another user's resource; each money-path guard has been
  removed once and its test seen to fail.
- **Verify:** `uv run pytest tests/assembly tests/security -q`

#### 12.4 Quality gates · S
- **Files:** `pyproject.toml`, `.pre-commit-config.yaml`, `.gitleaks.toml`
- **Done when:** `poe check` fails below the coverage thresholds (shown by lowering coverage
  once); the dependency audit reports nothing high; the secret scan is clean.
- **Verify:** `uv run poe check`

#### 12.5 Load baseline · S
- **Files:** `tests/load/locustfile.py`, `docs/load-baseline.md`
- **Done when:** a mixed workload runs against real processes; throughput and latency
  percentiles are recorded with the machine they came from; the verifier is clean afterwards.
- **Verify:** `uv run poe load && uv run corridor verify-ledger`

### Phase 13: Local stack

#### 13.1 Container image · S
- **Files:** `Dockerfile`, `.dockerignore`
- **Done when:** the image is multi-stage, installs from the lockfile and runs as a non-root
  user; the Dockerfile linter is clean. **The image is not built in the workspace.**
- **Verify:** `uv run poe lint-docker`

#### 13.2 Compose stack · S · after 13.1
- **Files:** `compose.yaml`, `.env.example`
- **Done when:** the file validates against the Compose schema and defines database, cache,
  migration, API, worker and simulators with health checks. **Not started in the workspace.**
- **Verify:** `uv run poe lint-compose`

#### 13.3 End-to-end runner · M
- **Files:** `scripts/e2e.py`, `tests/e2e/*`
- **Done when:** API, worker and simulators start as real processes against real stores and
  a scripted scenario passes (S7).
- **Verify:** `uv run poe e2e`

#### 13.4 Demo scenario · S · after 13.3
- **Files:** `src/corridor/demo.py`, `src/corridor/cli.py`
- **Done when:** the narrated deposit, conversion, transfer and withdrawal run against a
  live stack and end with a clean verification.
- **Verify:** `uv run poe demo`

### Phase 14: Deployment

#### 14.1 CI workflow · S
- **Files:** `.github/workflows/ci.yml`, `.github/dependabot.yml`
- **Done when:** lint, types, contracts, tests on PostgreSQL 16, 17 and 18, image build,
  secret scan and dependency audit are defined; actions are pinned to commit SHAs looked up
  from the source repositories; the workflow passes its schema and security linters.
  **Not run until the owner pushes.**
- **Verify:** `uv run poe lint-ci`

#### 14.2 Infrastructure: network · S
- **Files:** `infra/` (network module)
- **Done when:** a VPC across two zones, public and private subnets, and security groups that
  admit only the paths in section 18 are defined.
- **Verify:** `uv run poe lint-infra`

#### 14.3 Infrastructure: data stores · S · after 14.2
- **Files:** `infra/` (database and cache modules)
- **Done when:** PostgreSQL 16 and the cache are private, encrypted, and backed up; no
  resource is publicly accessible.
- **Verify:** `uv run poe lint-infra`

#### 14.4 Infrastructure: compute · M · after 14.3
- **Files:** `infra/` (cluster, services, load balancer, migration task)
- **Done when:** API and worker services, a one-off migration task and the load balancer are
  defined; only `/v1` and `/healthz` are routed publicly.
- **Verify:** `uv run poe lint-infra`

#### 14.5 Infrastructure: identity and secrets · S · after 14.4
- **Files:** `infra/` (roles, secrets, log groups), `infra/README.md`
- **Done when:** each task role reaches only its own secrets and logs; the README gives the
  commands, the cost table and a destroy procedure.
- **Verify:** `uv run poe lint-infra`

#### 14.6 Deploy workflow · S · after 14.1, 14.5
- **Files:** `.github/workflows/deploy.yml`
- **Done when:** the workflow authenticates with OIDC, builds, pushes, runs the migration
  task and updates the services; it runs only on manual dispatch.
- **Verify:** `uv run poe lint-ci`

### Phase 15: Documentation

#### 15.1 README · S
- **Done when:** a new reader can start the stack and run the demo from the README alone.

#### 15.2 Architecture as built · S
- **Files:** `docs/architecture.md`, `docs/adr/*`
- **Done when:** every statement in the architecture matches the code, and each decision in
  section 20 has a short record with its context and consequences.

#### 15.3 API guide · S
- **Files:** `docs/api.md`, exported `docs/openapi.json`
- **Done when:** every endpoint has a worked request and response, generated from a running
  instance.

#### 15.4 Runbook · S
- **Files:** `docs/runbook.md`
- **Done when:** each alert in section 15 has a diagnosis and a response; dead letters,
  reconciliation breaks and a ledger finding each have a procedure.

#### 15.5 Claude Code guide · XS
- **Files:** `CLAUDE.md`
- **Done when:** it holds the commands, the conventions, the lock order and the
  never-do list, each checked against the repository.

- **Verify (phase):** `uv run poe lint-docs`, which checks links and renders every diagram.

### Phase 16: Final verification

#### 16.1 Full gates · S
- **Done when:** `uv run poe check`, `uv run poe e2e` and `uv run corridor verify-ledger`
  pass from a clean checkout. S1 to S8 are each ticked with the command that showed it.

#### 16.2 Independent review · S
- **Done when:** a reviewer with no view of the build, given only the code and the
  architecture, has examined the ledger, idempotency, withdrawal and agent paths; each
  finding is fixed or recorded as accepted.

#### 16.3 Handoff · S
- **Done when:** this plan is moved to `plans/archive/` with an execution record; the project
  is in the owner's folder; everything not verified is listed in the final report.

---

## Risks

| Risk | Likelihood | Response |
|---|---|---|
| A library has changed since its documentation was last read (SQLAlchemy 2.1, redis-py 8, Starlette 1.x, pytest-asyncio 1.x are all recent majors) | High | Read the installed package and its changelog before using an API; never code from memory of an older version |
| The image or the Compose file fails on first run, since neither can be exercised here | Medium | Lint both; run the identical commands as local processes in the end-to-end step; list the first-run commands and likely failure points in the README |
| The infrastructure definition has an error only a real validation would find | Medium | The checks in Q2; CI validates on first push; nothing is applied automatically |
| Sub-agent output diverges from the design | Medium | Decision-complete briefs, disjoint files, every result re-run and every diff read before commit |
| Scope outruns one session | Medium | Checkpoints are complete, verified states; the progress table says exactly where work stopped |

## Decision log

Design decisions D1 to D20 are in
[section 20 of the architecture](../../docs/architecture.md#20-decision-log). Changes made
during the build are appended here with the date and the reason.

| Date | Decision | Why |
|---|---|---|
| 2026-10-05 | Audit log is its own bottom-layer module rather than part of `risk` | `identity` sits below `risk` and still needs its events audited |
| 2026-10-05 | `agents` sits above `payments` | An approved request executes a transfer; `payments` stays unaware of agents |
| 2026-10-05 | Entry points own transactions | One visible commit boundary per request, event or job |
| 2026-10-05 | The simulator package lives at `src/corridor_sim`, beside `src/corridor` | One source root keeps packaging and imports simple; a contract forbids `corridor` from importing it |
| 2026-10-05 | Business time comes from an application clock and is passed into SQL as a parameter | Tests can move time without sleeping; one time source for every rule that has a deadline |
| 2026-10-05 | Advisory locks are taken in ascending order of the lock key, which is derived from the user id | A total order on the keys themselves cannot deadlock even if two ids hash to one key |
| 2026-10-05 | The metrics module and `/metrics` exist from phase 0; step 12.1 completes the set | The transaction-retry counter was needed by the unit of work |
| 2026-10-05 | Tests use their own roles, `corridor_test_owner` and `corridor_test_app`; the application role's name is configuration | Running the suite cannot disturb a developer's own roles on the same server |
| 2026-10-05 | The engine hides bound parameters in error messages | Statement errors would otherwise carry email addresses and password hashes into logs |
| 2026-10-05 | `create_app` configures logging itself | The redacting pipeline is in place however the app is started, not only through the CLI |
| 2026-10-05 | S1 is checked after every test, not once per run: the `db` fixture runs the verifier at teardown and fails the test on any finding | Each test has its own database, so a single run at the end would have nothing to verify. `corridor verify-ledger` is exercised against a live stack in phase 13 |
| 2026-10-05 | `post_entry` has no savepoint | Every check precedes the first write, so a refusal has nothing to roll back; the savepoint cost two round trips per entry and no test could tell it was there |
| 2026-10-05 | An account appears at most once in an entry | Keeps `balance_after` unambiguous; callers combine amounts |
| 2026-10-05 | Accepted untested guard: `ORDER BY account_id` on the balance lock | With one `IN (...)` query PostgreSQL returns the rows in the same order to every session whatever the list order, so removing the clause changes nothing a test can see. It stays so that lock order does not depend on the planner. A control test shows the deadlock is real when rows are locked in posting order |
| 2026-10-06 | Phases 2 to 5 were built by sub-agents in isolated worktrees, earlier than the plan's "from phase 5" | The owner asked for sub-agents wherever possible; the orchestrator re-runs every gate on the combined tree and makes every commit |
| 2026-10-06 | Trigger functions pin `search_path` with the temporary schema last; balance rows are locked `FOR NO KEY UPDATE` | A session's temporary table named `postings` could otherwise hide an unbalanced entry from the balance check (reproduced, then fixed); the weaker lock is all posting needs |
| 2026-10-06 | Idempotency takes an advisory lock on (actor, key) before reading the key row | Concurrent duplicates queue and replay cleanly instead of racing to the primary key; the primary key remains the safety net |
| 2026-10-06 | Cursor pagination lives in `platform`, not `api` | Modules below the API return pages; a worker stopped on the layer contract rather than break it |
| 2026-10-06 | Not yet done from review notes: `TEMPORARY` is not revoked from the application role; the minimum transfer fee is one number for all assets; restricting a user does not take the money-out lock | Each needs a decision outside the slice that found it; tracked here until phase 12 |
| 2026-10-06 | Review findings deferred to phase 12, each open: a third party can keep an account locked by failing logins; registration and transfer-by-email reveal which emails exist; a closed or demoted user's access token works until it expires; no request body size limit; IPv6 clients are rate-limited per address, not per /64; the application role could update any column of `users` and `account_balances` if SQL injection ever appeared; appending a balanced pair of postings to an old entry is caught by the verifier, not refused by the database; restricting a user does not wait for that user's in-flight transfer; metrics are unauthenticated | None loses or creates money. Each needs a design decision rather than a patch |
| 2026-10-06 | New withdrawal state `submitting`, committed before the provider call; cancel only from `held` | A cancellation during the provider call released funds for a payout that then went out |
| 2026-10-06 | A provider refusal is checked against what the provider holds before funds are released | The failure-injection suite found two cases where a refusal followed a payout that had in fact been made |
| 2026-10-06 | Open after checkpoint B: a dropped deposit webhook is not recovered until reconciliation (phase 10); a withdrawal left `submitting` whose event went dead needs an operator requeue; expired quotes are never purged; the minimum transfer fee is one number for all assets; revisions 0005 and 0011 were edited in place | Recorded so they are not mistaken for done |
