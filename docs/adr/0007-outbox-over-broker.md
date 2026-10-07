# 0007. Transactional outbox instead of a message broker

**Status:** accepted

## Context

Some work must happen after a commit: sending a withdrawal to a provider, applying a stored
webhook. If the request publishes a message to a broker and then commits (or commits and
then publishes), a crash between the two leaves a message without a state change, or a
state change without a message. A withdrawal whose "send it" message was lost holds a
user's funds for ever.

## Decision

Write the message as a row in `outbox_events` in the same transaction as the state change.
A dispatcher in the worker claims due rows with `SELECT … FOR UPDATE SKIP LOCKED`, runs the
handler for the row's topic, and records the outcome.

- A claim lasts 60 seconds and carries a claim id. A result is recorded only under the
  claim it was produced in.
- A failed event is retried with exponential backoff and full jitter, up to 8 attempts,
  then marked `dead` for an operator.
- An insert trigger sends `NOTIFY` so a worker wakes at once. A 5-second poll is the
  reliable path.
- Delivery is at least once and unordered. Every handler is idempotent and is guarded by
  the state of the row it advances.

## Consequences

- The event exists if and only if the state change committed. There is no dual write.
- Nothing extra to run. The queue is a table in the database that already exists.
- Several workers can drain the queue at once without coordination.
- Throughput is bounded by the database. At a volume where that matters, the outbox would
  be relayed to a broker by change data capture, and the handlers would not change.
- Handlers must tolerate running twice and out of order. That discipline is needed with a
  broker too; here it is explicit.
- Finished rows are purged after 7 days. Dead rows stay until someone looks at them.
