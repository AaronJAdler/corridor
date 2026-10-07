# 0009. Withdrawal holds are ledger movements

**Status:** accepted

## Context

A withdrawal takes time: the provider is asked, and the answer arrives seconds or days
later. Meanwhile the money must not be spendable, and it must come back if the payout
fails. A common design keeps a `holds` table and computes "available" as balance minus
holds. Every balance read then has to remember the holds, and a hold is outside the
ledger's invariants.

## Decision

Each user has two ledger accounts per asset: `user_available` and `user_held`. Reserving
funds is a journal entry that moves the amount and the fee from the first to the second.
Settling the withdrawal debits `user_held`. Releasing it moves the money back.

## Consequences

- A hold is in the same history as everything else and is covered by the same triggers and
  the same verifier.
- The available balance is one number. Nothing can forget to subtract a hold.
- A user's wallet shows `available`, `held` and `total` directly.
- Each withdrawal writes two or three entries (hold, then settle or release) instead of
  one.
- Both accounts are constrained, so a release or a settlement can never take more out of
  `user_held` than was put in.
