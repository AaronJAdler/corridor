# 0022. Query plans are tested, and statements are measured in production

**Status:** accepted

## Context

The hot paths each have an index built for them: the outbox claim has two partial indexes
over the rows that are due, an account statement has `(account_id, seq)`, an idempotency
lookup has its primary key and its purge has `created_at`, the audit log has
`(principal_id, occurred_at)`. Nothing checked that the statements still used them. An
index dropped in a migration, a predicate rewritten so that it no longer matches a partial
index, or a statement changed past its index passes every functional test and arrives as a
slow production. The RDS parameter group logged statements slower than a second, which
reports the damage after the fact, and there was no per-statement view to read when it did.

## Decision

- **Plans are tests.** `tests/plans` runs each hot path as the application runs it, captures
  the statements it issues at the driver, and hands each back to PostgreSQL under
  `EXPLAIN (FORMAT JSON)`. The test reads the plan: no sequential scan of the large table,
  and the index built for the statement among those used. The statements under test are
  the code's own; a test holds no copy of them.
- **The seed is the steady state, with statistics.** Plans are read from a database with a
  few thousand rows in each hot table in production's proportions (a queue that is kept up
  with, a purge that runs often), after `ANALYZE` as the owner. On an empty table, or
  without statistics, a sequential scan is the right plan and proves nothing.
- **Statements are measured in production.** `pg_stat_statements` is preloaded through the
  RDS parameter group and in compose; the extension is created by the master user and the
  application role may read the view (`pg_read_all_stats`). The runbook has the procedure.
- **A plan test fails only on the plan's shape.** Costs and times are not asserted: they
  move with hardware and settings, and a shape is what the index was built for.

## Consequences

- Removing or breaking an index on a hot path fails the suite with the plan in the message.
  Proven by mutation: with `ix_postings_account_id_seq` dropped, the statement test shows a
  sequential scan.
- The planner is allowed its judgement. The tests say "through this index", not "with this
  node type": a bitmap scan and an ordered index scan both pass, as they should.
- The planner's choice depends on the distribution, so the seed is part of the test. With a
  backlog where most outbox rows are due, PostgreSQL walks the primary key and finds due
  rows at once; the partial indexes are for the steady state, and that is what is seeded.
- The plan tests take about ten seconds and run with the suite on every PostgreSQL line in
  the matrix, so a new major that plans a statement differently is seen before an upgrade.
- `pg_stat_statements` costs a little shared memory and a few percent of statement time; it
  cannot be enabled without a reboot, which the maintenance window provides.

## Alternatives considered

- **Slow-query logging only.** Kept, at one second; it reports after the fact and gives no
  counts or totals.
- **Asserting costs or `EXPLAIN ANALYZE` timings.** They vary by machine and setting; a
  threshold either fails on CI's hardware or passes on anything.
- **`auto_explain`.** Logs plans of slow statements in production; useful later, but it
  does not stop the change that makes a statement slow from merging.
