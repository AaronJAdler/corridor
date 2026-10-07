# 0010. A `submitting` state, committed before the provider is called

**Status:** accepted

## Context

In the first version a withdrawal went from `held` straight to `submitted` once the
provider had accepted the payout, and a user could cancel a `held` withdrawal. The
failure-injection suite found the hole: a user cancels while the worker is in the middle of
the call to the provider. The cancellation sees `held`, releases the funds, and commits.
The provider then accepts the payout. The money has gone out and is also back in the
wallet.

Holding a database lock across the provider call would close the hole and break the rule
that no transaction spans a network call.

## Decision

Add a state between `held` and `submitted`. The worker marks the withdrawal `submitting`
in a transaction that commits **before** the provider is called. A cancellation is accepted
only from `held`.

## Consequences

- A cancellation and a submission cannot both win. Either the cancellation commits first
  and the worker finds the withdrawal canceled, or the mark commits first and the
  cancellation is refused with `409`.
- `held` now means "the provider is certain not to have this". Everything that gives funds
  back without asking the provider (cancel, a rejected review, a returned deposit, an
  account that is no longer active) is allowed only from `held`.
- A worker that dies after the mark leaves a withdrawal in `submitting`. The outbox retries
  it under the same idempotency key, and the sweeper asks for it again if the event went
  dead.
- A settlement webhook can arrive while the withdrawal is still `submitting`, so settlement
  is accepted from `submitting` as well as from `submitted`.
- A user cannot cancel during the short time the provider is being asked.
