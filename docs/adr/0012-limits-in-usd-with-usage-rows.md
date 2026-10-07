# 0012. Limits in USD at reference rates, with one usage row per movement

**Status:** accepted

## Context

A wallet needs a per-transaction limit and a rolling daily limit, by KYC tier, with
overrides for a user and for an agent. A user holds several assets, and a daily limit per
asset is easy to walk around by spreading a sum over assets. A single running counter per
user is simple and cannot answer "what moved in the last 24 hours", cannot be given back
when a withdrawal fails, and is one more row everyone's movements update.

## Decision

- A limit rule (`risk_limits`) is in whole US cents and belongs to a tier, a user or an
  agent, for one kind of movement or for all. The most specific rule wins.
- Every authorised movement writes one row to `risk_usage` in its own transaction, with its
  value in US cents at a fixed reference rate (`risk_reference_rates`), rounded up.
- The 24-hour total is the sum of the user's unreleased usage rows in the window, read
  under the user's money-out lock.
- A withdrawal that is canceled, failed or released sets `released_at` on its usage row,
  and the row stops counting. The row itself is never rewritten.
- `(kind, movement_id)` is unique, so authorising a movement twice counts it once.
- An agent's rule is a second limit on what that agent alone has moved.

## Consequences

- One limit covers all of a user's assets, and the sum is exact because of the lock.
- A movement that rolls back leaves no usage behind. A withdrawal that fails gives its
  usage back.
- The usage table is also a record of what was authorised and when.
- The reference rates are fixed numbers set by a migration. They are not market rates, and
  a large move in a currency would make the limits looser or tighter than intended until
  someone updates them.
- Conversions count toward the daily limit although no money leaves the wallet. That is an
  accepted limit of this build.
- The sum is a query over a user's last day of rows on every movement. An index on
  `(user_id, created_at)` serves it.
