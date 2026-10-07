# 0003. Append-only double-entry ledger, enforced by database triggers

**Status:** accepted

## Context

The ledger is the record of who holds what. A design with mutable balance columns and a
log beside them has two sources of truth, and a bug in one code path can change a balance
with no trace. The application can promise never to edit history, but an application bug,
a migration or a person with a database console can break that promise.

## Decision

Every balance change is a journal entry made of postings. Entries balance per asset.
History is never changed; a correction is a new entry. The database enforces this itself:

- A deferred constraint trigger checks at commit that an entry has at least two postings
  and that debits equal credits in every asset.
- Statement-level triggers refuse `UPDATE`, `DELETE` and `TRUNCATE` on `journal_entries` and
  `postings`. They stop the schema owner too, not only the application role.
- A trigger refuses a posting whose entry was written by a transaction that has ended, so a
  balanced pair cannot be appended to an old entry.
- The application role has `SELECT` and `INSERT` on these tables and nothing else.
- `UNIQUE (source_type, source_id, kind)` means one business event posts at most once.
- A verifier recomputes every invariant and reports what differs. It runs hourly in the
  worker, after every test, and on demand as `corridor verify-ledger`.
- `ledger_accounts` is written once as well: the same trigger refuses any change to an
  account, and a constraint holds its category and normal side to its kind.
- The verifier compares rows that exist. A whole entry deleted together with its postings,
  with the balances rewritten to match, would need a hash chain over the journal to be
  seen, and there is none.

`ledger.post_entry` is the only function that writes these tables.

## Consequences

- Money is conserved even if the application has a bug: the database refuses the write.
- Every balance can be re-derived from postings and compared with its cache.
- Idempotency comes for free at the lowest level. A handler that runs twice posts once.
- The tables only grow. Partitioning `postings` is the answer at volume.
- A data-only restore has to deal with the triggers. The sealed-entry trigger looks at
  transaction ids, which a restore does not preserve; the runbook says how to restore.
- Mistakes are corrected with reversal or adjustment entries, which need two
  administrators. Nothing can be fixed quietly.
