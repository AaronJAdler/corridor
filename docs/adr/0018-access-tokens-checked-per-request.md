# 0018. ES256 access tokens, checked against the database on every request

**Status:** accepted

## Context

Access tokens are JWTs. A JWT is valid until it expires, whatever has happened since it was
signed. With a 15-minute lifetime that leaves a window in which a closed account, a demoted
administrator or a logged-out session still has a working token. A revocation list in
Redis closes the window only while Redis is reachable.

A symmetric signature (HS256) would also mean that every service able to verify a token
could mint one.

## Decision

- Access tokens are ES256 JWTs that live 15 minutes. The verifier allows that one
  algorithm and picks the key by `kid` from the configured keys. Public keys are served as
  a JWKS, and retired public keys can stay configured so their tokens verify until they
  expire.
- On every request, after the signature check, the API reads the user's row by primary key
  and refuses the token if the account is closed, if the role differs from the token's, or
  if the token was issued before `users.tokens_valid_after`.
- Changing a role or closing an account sets `tokens_valid_after`.
- Refresh tokens are opaque, stored as hashes, and rotated on every use. Reuse of a rotated
  token revokes the whole session.
- Logout revokes the session in PostgreSQL and also sets a mark in Redis that is checked
  per request. The mark is a hint; the per-request database check does not depend on it.

## Consequences

- Closing an account or changing a role takes effect at the next request, with or without
  Redis.
- A service split out later can verify tokens with the public key alone.
- Every authenticated request costs one primary-key read. That is the price of immediate
  revocation, and it is paid on purpose.
- A plain logout still depends on the Redis mark for the remaining minutes of the access
  token, because logging out one session does not end the user's other tokens.
- A stolen refresh token reveals itself the first time both holders use it.
- Rotating the signing key is a configuration change: the new key signs, and the old public
  key stays listed until its tokens have expired.
