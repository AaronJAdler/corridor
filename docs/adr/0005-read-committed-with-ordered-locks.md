# 0005. READ COMMITTED with ordered explicit locks and a per-user money-out lock

**Status:** accepted

## Context

Concurrent requests against one balance must not both succeed when only one can be
afforded. PostgreSQL offers `SERIALIZABLE` isolation, which aborts one of two conflicting
transactions, and explicit row locks under `READ COMMITTED`, which make one wait.

There is a second problem that row locks on balances do not solve. Limits are per user
across all assets, and balance rows are per asset. Two sends in two assets at the same
moment would each read the same 24-hour total and both pass.

## Decision

Use `READ COMMITTED` and take explicit locks, always in one order:

1. the idempotency key (an advisory lock on the actor and the key);
2. the money-out lock of each user whose money moves out (transaction-scoped advisory
   locks, in ascending key order);
3. the business row being advanced (`FOR UPDATE`);
4. balance rows, in ascending account id (`FOR NO KEY UPDATE`).

Every path that takes money out of a wallet takes that user's money-out lock first:
transfers, withdrawal requests, conversions, approvals of agent requests, adjustments that
debit a user, and returned deposits. Restricting a user takes it as well.

A unit of work that still meets a deadlock or a serialisation failure is re-run up to three
times, and a metric counts each re-run.

## Consequences

- Contending requests queue for a few milliseconds instead of aborting and retrying.
- The code says what it protects. A reader can see which lock makes a given check safe.
- The limit check and the balance check are race-free for one user, and no user waits for
  another.
- The lock order is a rule every new money path must follow. It is written in `CLAUDE.md`
  and checked in review; nothing mechanical enforces it.
- One user's outgoing movements are serialised, including across assets. That is the
  intended cost.
- A transfer takes the recipient's money-out lock together with the sender's, and a
  deposit takes its owner's, because closing an account reads that it is empty under that
  lock: a credit must not land between the read and the closing. Payments to one person
  therefore queue behind each other, and behind that person's own outgoing movements, for
  the length of one short transaction.
