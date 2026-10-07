# 0014. FX rates cached in Redis carry an HMAC

**Status:** accepted

## Context

A quote needs a mid-market rate, and asking the rate source on every quote is slow. A
short cache in Redis is shared by every API process. A cached rate sets the price of a
conversion, though, and Redis is not a trusted store: it has one credential shared by
everything that uses it, and anyone who can write to it could set a rate and convert at
it.

## Decision

A rate is cached in Redis only with a message authentication code.

- The cached value is a JSON document with the rate, its time and an HMAC-SHA256 over the
  pair, the rate and the time.
- The HMAC key is derived from the setting `fx_cache_mac_key` under a fixed label, so the
  configured key is never used directly on data.
- On read, the HMAC is checked in constant time before anything in the entry is used. An
  entry that does not verify is treated as absent and logged.
- The pair is part of what is authenticated, so an entry copied to another pair's key does
  not verify.
- Without the setting, rates are cached inside each process and Redis is not used for them.
- A rate older than 15 seconds is refused wherever it came from.

## Consequences

- Write access to Redis is not enough to set a price.
- Redis stays untrusted, consistent with the rule that it is never a source of truth.
- One more secret to configure. A deployment that omits it still works, with a per-process
  cache.
- Rotating the key makes existing entries fail verification. They expire after 5 seconds
  anyway, so rotation costs a few extra calls to the rate source.
