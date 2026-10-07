# 0008. Webhooks are verified, stored and acknowledged before they are processed

**Status:** accepted

## Context

Providers report deposits and payout results by webhook. They deliver at least once, in no
particular order, and retry when the receiver is slow or fails. Processing an event can
take locks and post ledger entries. If that happens inside the webhook request, a slow or
failing handler makes the provider retry, and an event that failed half-way is gone unless
the provider sends it again.

## Decision

The webhook endpoint does four things and nothing else:

1. reads the raw body and verifies the HMAC signature over it, before parsing anything;
2. validates the envelope strictly;
3. inserts the event into `webhook_events` (unique on provider and event id) and an outbox
   row, in one transaction;
4. answers `200`.

The worker applies the event later, through the same payment functions the sweeper and
reconciliation use. A duplicate delivery inserts nothing and is acknowledged.

## Consequences

- The provider gets a fast, stable answer whatever the event leads to.
- Every accepted event is on record and can be processed again.
- A failing handler is retried by the outbox, with backoff, and ends as a dead letter that
  an operator can requeue. The provider is not involved.
- There is a short delay between a delivery and its effect.
- The stored payload contains personal fields (a sender's name, a payment reference, a
  sending address). They are set to null 30 days after the event was processed.
- The signature covers the body and a timestamp, not the provider's name, so the two
  providers must not share a secret. The API refuses to start if they do.
