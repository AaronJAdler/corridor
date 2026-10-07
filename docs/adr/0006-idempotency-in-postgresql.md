# 0006. Idempotency keys in PostgreSQL, committed with the effect

**Status:** accepted

## Context

A client that does not get an answer to a money-moving request must be able to retry it
without moving the money twice. The usual tool is an `Idempotency-Key` header: the server
remembers the key and replays the first answer.

Where the key is remembered matters. If it is in Redis and the effect is in PostgreSQL, the
two writes cannot be atomic. Key first, then a crash: the key exists and the money did not
move, so the retry is wrongly answered or stuck. Effect first, then a crash: the money moved
and the retry moves it again.

## Decision

Store the key in the table `idempotency_keys`, in the same PostgreSQL transaction as the
work.

- The transaction first takes an advisory lock on the actor and the key, so a concurrent
  duplicate waits and then replays.
- The key row is inserted with a fingerprint of the request: the method, the route
  template, the canonical JSON body and, for a route with parameters, the path that was
  asked for, so that a key used on one resource is refused on another.
- The work runs inside a savepoint. If it raises a domain refusal, the savepoint undoes the
  work and the refusal is stored as the response.
- The response is written onto the key row, and everything commits together.
- A key belongs to one actor. Keys are deleted after 24 hours.

## Consequences

- There is no moment at which the key exists without the effect, or the effect without the
  key.
- A refusal is replayed like a success. A client that wants to try again after fixing the
  cause must use a new key.
- An unexpected error rolls the key back with everything else, so the client can retry.
- Each money-moving request costs one more row and one advisory lock.
- The key table needs a purge job. The worker runs it, with one SQL statement against a
  table that otherwise belongs to the API.
- A request that calls a provider cannot use this, because no transaction may be open
  during the call. Saving a beneficiary passes the key to the bank instead.
