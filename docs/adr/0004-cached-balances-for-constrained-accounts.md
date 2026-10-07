# 0004. Cached balances only for accounts that must not go negative

**Status:** accepted

## Context

Refusing an overdraft needs a check that is atomic with the debit, which means a row lock on
a balance. A user's own balance is touched by that user's movements only, so a lock on it
is cheap. A system account is different: `fee_revenue` is credited by every movement that
charges a fee, and a settlement account by every deposit and payout. If those had a locked
balance row, unrelated users would queue behind each other.

## Decision

Accounts are of two kinds.

- **Constrained** accounts (`user_available`, `user_held`, `user_receivable`) may not go
  below zero. Each has a row in `account_balances`. Posting locks the row, checks the new
  balance, and updates it. Each posting records `balance_after`.
- **Unconstrained** accounts (settlement, omnibus, suspense, FX position, revenue, expense)
  have no balance row and take no lock. Their balance is the sum of their postings,
  computed when it is read.

## Consequences

- Contention is per user. There is no row that every transaction locks.
- A user's statement can show the balance after each entry without recomputing it.
- Reading a system account's balance is a sum over its postings. That is fine at this
  scale; at volume it would be served from periodic snapshots plus the postings since.
- An unconstrained account can go negative. For settlement accounts that is correct (a
  provider balance can be overdrawn). For `suspense` it would be a fault, so every debit of
  suspense is tied to a deposit whose status is checked under a row lock, and the verifier
  reports a negative suspense balance.
- The verifier has to check both kinds: that each constrained account has a balance row
  that matches its postings, and that no unconstrained account has one.
