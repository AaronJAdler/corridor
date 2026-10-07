# 0011. A provider's refusal is checked against what the provider holds before funds are released

**Status:** accepted

## Context

When a provider answers a payout request with a `4xx`, the contract says nothing happened,
and the obvious response is to release the user's funds. The failure-injection suite found
two cases where that pays out twice:

- The first attempt succeeded at the provider and its answer was lost. The retry is then
  refused, for example because the retry's body differs or something between Corridor and
  the provider answers `4xx`.
- The provider answers `409 idempotency_conflict`, which says something about the key and
  nothing about whether a payout exists.

In both, a payout exists and the refusal is true only of the request it answered.

## Decision

A refusal does not release funds on its own word.

- An `idempotency_conflict` is treated as an unknown outcome. The funds stay reserved and
  the event is retried.
- For any other refusal, Corridor first asks the provider what it holds under the
  withdrawal's reference (`GET /payouts?reference=` or `GET /withdrawals?reference=`). If
  the provider holds a payout, the submission is recorded and the withdrawal goes on. Only
  if it holds none are the funds released and the withdrawal marked `failed`.
- If the provider cannot be asked, nothing changes and the event is retried.
- More than one payout under one reference is raised as an unknown outcome, for a person.

## Consequences

- Funds are never released for a payout the provider has made.
- A refusal costs one more read of the provider.
- It depends on the provider being able to list payouts by Corridor's reference. That is
  part of the provider contract, and a real provider would have to offer the same.
- A provider that is down keeps the withdrawal `submitting` until it is back. The user's
  funds stay reserved in the meantime, which is the safe side.
