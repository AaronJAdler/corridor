# 0021. One PostgreSQL major everywhere the stack runs as production runs, and how it moves

**Status:** accepted

## Context

Production is an RDS instance on PostgreSQL 16 (`engine_version` in the data module). The
compose stack and the sandbox scenario are the two other places the whole stack runs, and
each named its own image: compose said 16, the sandbox job said 18. Nothing declared which
was production's, and nothing failed when they disagreed. The unit-test matrix in `ci.yml`
runs 16, 17 and 18 on purpose, so the suite was never the gap; the sandbox scenario was
being proven on a server production does not have.

A major upgrade of the production database has no written procedure either. RDS allows one
in place, and `allow_major_version_upgrade` is `false` so that it never happens by accident,
but "never by accident" needs a "how, on purpose".

## Decision

- **The RDS instance declares the production major.** `infra/modules/data/main.tf` is the
  one place it is written as a decision; the parameter group family follows it.
- **Compose and the sandbox scenario run that major.** `tests/assembly/test_postgres_version.py`
  reads the three files and fails when they drift.
- **The unit-test matrix keeps looking ahead.** It must include the production major and at
  least one newer line, so the next upgrade is rehearsed on every push. A failure on a
  newer line only is a warning about the future, not a block on the present.
- **A major upgrade is a unit of work, in this order:**
  1. The newer major has been green in the matrix for a while, and the plan tests
     (ADR 0022) pass on it: a new major may plan a statement differently.
  2. Rehearse on a restored snapshot (runbook, "Restoring from a backup"): upgrade the
     copy, run `corridor verify-ledger` and the suite against it, read the slow-query view.
  3. Change `engine_version`, the parameter-group family and `allow_major_version_upgrade`
     in one commit; apply in the maintenance window; RDS runs `pg_upgrade` in place.
     Blue/green is the alternative when the window is too short for the data size.
  4. `ANALYZE` every table after the upgrade: `pg_upgrade` carries the data and not the
     planner statistics, and the first hours otherwise run on default estimates.
  5. The same commit moves compose and the sandbox job; the drift test says when it is
     complete. Set `allow_major_version_upgrade` back to `false`.

## Consequences

- A change is proven end to end on the server production has. The matrix still says, on
  every push, whether the next line would break anything.
- Three files change together on an upgrade, and a test says so if one is forgotten.
- The procedure's cost is one rehearsal on a snapshot per upgrade; the benefit is knowing
  the plans and the ledger survive before production does it.
- Nothing is tested on a major older than production's. There is no reason to.

## Alternatives considered

- **Test and run the sandbox on the newest line, as the sandbox job did.** It finds
  tomorrow's problems and misses today's: a plan or a lock behaviour that differs between
  16 and 18 would be proven on the one production does not run.
- **Pin the matrix to the production major only.** Cheaper, and the first warning of an
  incompatibility would then come during the upgrade itself.
