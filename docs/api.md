# Corridor API guide

This guide describes the HTTP API that `corridor serve` exposes: how to authenticate, how
errors, idempotency, pagination and rate limits work, and every endpoint with a worked
request and response.

The machine-readable description is [openapi.json](openapi.json). It is exported from the
application, and `uv run poe lint-docs` fails when it no longer matches the code. Outside
production the running API also serves it at `/openapi.json`, with an interactive page at
`/docs`.

**Where the examples come from.** The requests and responses below were recorded from a
running stack: the API, the worker and the provider simulators as real processes, with a
scratch PostgreSQL database and Redis. Access tokens, refresh tokens, agent keys, passwords
and signature digests were replaced with placeholders such as `<access token>`. Every
person, account number and address is synthetic. Ids and timestamps are the ones that run
produced, so an id that appears in one example is the same object in the next. Long opaque
values (cursors, public key material, transaction hashes) are cut to 16 characters and end
with `…`.

## Contents

- [Conventions](#conventions)
- [Authentication](#authentication)
- [Errors](#errors)
- [Idempotency](#idempotency)
- [Pagination](#pagination)
- [Rate limits](#rate-limits)
- [Service endpoints](#service-endpoints)
- [Auth](#auth)
- [Wallets](#wallets)
- [Deposits](#deposits)
- [FX](#fx)
- [Transfers](#transfers)
- [Beneficiaries and withdrawals](#beneficiaries-and-withdrawals)
- [Agents](#agents)
- [Approvals](#approvals)
- [Webhooks](#webhooks)
- [Admin](#admin)

## Conventions

- **Transport.** JSON over HTTP. Every path that does work is under `/v1`. Field names are
  snake_case.
- **Amounts.** An amount is a decimal string in major units with an asset code beside it:
  `{"asset": "USD", "amount": "12.50"}`. A JSON number is refused, because it would have
  passed through a floating-point value. An amount with more decimal places than the asset
  has is refused and never rounded. Responses always carry exactly the asset's decimal
  places.
- **Assets.** `USD`, `MXN` and `BRL` have 2 decimal places and move over a bank rail. `USDC`
  has 6 and moves on a simulated chain.
- **Identifiers.** UUIDs, version 7. They sort by creation time.
- **Time.** ISO-8601 in UTC, for example `2026-10-07T12:46:18.778556Z`.
- **Unknown fields.** A request body with a field the endpoint does not know is refused
  with `422`. A misspelled field is reported instead of ignored.
- **Request ids.** Every response carries `X-Request-ID`. A client may send its own id (8 to
  64 characters from letters, digits, `.`, `_` and `-`), and the API uses it. Otherwise the
  API generates one. The same id is in every log line the request produced and in the
  `request_id` field of an error.

```http
GET /healthz
```

```http
HTTP 200
X-Request-ID: docs-example-0001

{
  "status": "ok"
}
```

- **Response headers.** Every response carries `Cache-Control: no-store`,
  `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`,
  `Cross-Origin-Resource-Policy: same-origin`, `Strict-Transport-Security` and a
  `Content-Security-Policy` that allows nothing. CORS is not enabled: clients send bearer
  credentials, not cookies.
- **Body size.** A request body larger than `CORRIDOR_MAX_REQUEST_BODY_BYTES` (64 KiB by
  default) is refused with `413 payload_too_large` before any of it is parsed.

## Authentication

Every endpoint needs a credential except the service endpoints, `POST /v1/auth/register`,
`POST /v1/auth/login`, `POST /v1/auth/refresh`, `GET /.well-known/jwks.json` and
`POST /v1/webhooks/{provider}` (which is authenticated by a signature instead).

A credential is sent as `Authorization: Bearer <credential>`. There are two kinds.

### Access tokens

A user logs in with an email address and a password and receives an access token and a
refresh token.

- The access token is a JWT signed with ES256 (ECDSA on P-256). It lives for 15 minutes
  (`CORRIDOR_ACCESS_TOKEN_TTL_SECONDS`). Its claims are `iss`, `aud`, `sub` (the user id),
  `sid` (the session id), `role`, `scope`, `iat`, `exp` and `jti`. The header names the
  signing key in `kid`.
- The API checks more than the signature. On every request it reads the user's row and
  refuses the token if the account is closed, if the user's role is no longer the one in
  the token, or if the token was issued before the user's tokens were last ended. A role
  change or an account closure therefore takes effect at once, not when the token expires.
- The refresh token is an opaque random string that lives for 30 days. Each one can be
  exchanged once. Presenting a refresh token that was already exchanged ends the whole
  session, because a second use means the token was copied.
- Logging out revokes the session. The API also puts a mark in Redis so that the session's
  access token is refused immediately. If Redis is unavailable when the mark is written or
  read, the access token keeps working until it expires, at most 15 minutes later.

A user's own session holds every scope. Only a user's own session can manage agents,
decide approval requests, log out, read `/v1/me` or call the admin endpoints.

### Agent API keys

A user can create an *agent*: a named principal that acts on the user's wallet with its own
API key, its own scopes and a spend policy the user sets. See [Agents](#agents).

- A key looks like `ck_<environment>_<prefix>_<secret>`. The environment label is `dev`,
  `test` or `live`. The prefix is 12 lower-case letters and digits and is stored; the key is
  found by it. The secret is 32 random bytes and is never stored. Corridor keeps only the
  HMAC-SHA256 of the secret under a server-side key (`CORRIDOR_API_KEY_HASH_KEY`).
- The key is shown once, in the response that creates it.
- A key that is unknown, wrong, revoked or expired, a key of a paused or revoked agent, and
  a key whose owner's account is not active all get the same `401` answer.
- A key reaches only the endpoints that name a scope, and only with a key that holds it:

| Scope | Endpoints |
|---|---|
| `wallet:read` | `GET /v1/wallets`, `GET /v1/wallets/{asset}/entries` |
| `transfers:create` | `POST /v1/transfers` |
| `transfers:read` | `GET /v1/transfers`, `GET /v1/transfers/{transfer_id}` |
| `deposits:read` | `GET /v1/deposit-instructions`, `GET /v1/deposits`, `GET /v1/deposits/{deposit_id}` |
| `withdrawals:create` | `POST /v1/withdrawals`, `POST /v1/withdrawals/{withdrawal_id}/cancel` |
| `withdrawals:read` | `GET /v1/withdrawals`, `GET /v1/withdrawals/{withdrawal_id}` |
| `beneficiaries:write` | `POST /v1/beneficiaries` |
| `beneficiaries:read` | `GET /v1/beneficiaries` |
| `fx:convert` | `POST /v1/fx/quotes`, `POST /v1/fx/conversions` |
| `fx:read` | `GET /v1/fx/conversions/{conversion_id}` |

A request without a credential:

```http
GET /v1/me
```

```http
HTTP 401
WWW-Authenticate: Bearer

{
  "code": "unauthenticated",
  "detail": "This endpoint needs a bearer credential.",
  "request_id": "01a11666-5b4a-7378-9196-ccbfd4c342dc",
  "status": 401,
  "title": "Authentication required",
  "type": "https://corridor.example/problems/unauthenticated"
}
```

An agent key without the scope an endpoint needs:

```http
GET /v1/withdrawals
Authorization: Bearer <agent key>
```

```http
HTTP 403

{
  "code": "insufficient_scope",
  "detail": "This credential does not have the withdrawals:read scope.",
  "request_id": "01a11666-b342-7230-8a80-a49aea663502",
  "status": 403,
  "title": "Insufficient scope",
  "type": "https://corridor.example/problems/insufficient-scope"
}
```

An agent key on an endpoint that only the owner's own session may use:

```http
GET /v1/agents
Authorization: Bearer <agent key>
```

```http
HTTP 403

{
  "code": "insufficient_scope",
  "detail": "This action needs the account owner's own session.",
  "request_id": "01a11666-b34a-7221-ab0e-782c6a2c3602",
  "status": 403,
  "title": "Insufficient scope",
  "type": "https://corridor.example/problems/insufficient-scope"
}
```

A resource that belongs to another user is answered exactly like one that does not exist,
so an id cannot be probed:

```http
GET /v1/deposits/01a11666-67fd-74d2-a9ca-ae7288c0687a
Authorization: Bearer <access token>
```

```http
HTTP 404

{
  "code": "deposit_not_found",
  "detail": "There is no such deposit.",
  "request_id": "01a11666-6857-751f-b481-3248b0f8c50a",
  "status": 404,
  "title": "Deposit not found",
  "type": "https://corridor.example/problems/deposit-not-found"
}
```

## Errors

Every error is an RFC 9457 problem document with the media type
`application/problem+json`.

| Member | Meaning |
|---|---|
| `type` | A URI for the kind of problem: `https://corridor.example/problems/` followed by the code with hyphens. It is an identifier, not a page. |
| `title` | A short, fixed description of the kind of problem. |
| `status` | The HTTP status code. |
| `code` | The stable machine-readable code. Branch on this. |
| `detail` | A sentence about this occurrence. Present on most errors. Never contains what the client sent. |
| `request_id` | The id of the request, also in the `X-Request-ID` header. |
| other members | Some errors add fields, for example `errors` (validation), `field`, `limit` and `scope`. |

A refusal by a business rule:

```http
POST /v1/transfers
Authorization: Bearer <access token>
Idempotency-Key: docs-62e578c6-fd32-4929-81a6-a7f39827e6ec

{
  "amount": "900.00",
  "asset": "USD",
  "recipient": "01a11666-5390-7735-ac92-2e5f3ca67233"
}
```

```http
HTTP 402

{
  "code": "insufficient_funds",
  "detail": "Available balance is 350.00 USD; 900.00 USD is required.",
  "request_id": "01a11666-80a4-7228-9ce7-a31b7d847617",
  "status": 402,
  "title": "Insufficient funds",
  "type": "https://corridor.example/problems/insufficient-funds"
}
```

A body that does not match the endpoint's schema lists each problem by field. The rejected
input is never echoed, because it may be a password:

```http
POST /v1/auth/register

{
  "display_name": "",
  "email": "not-an-address",
  "handle": "x",
  "password": "<password>"
}
```

```http
HTTP 422

{
  "code": "invalid_request",
  "detail": "The request did not match the schema for this endpoint.",
  "errors": [
    {
      "field": "body.email",
      "message": "value is not a valid email address: An email address must have an @-sign.",
      "type": "value_error"
    },
    {
      "field": "body.display_name",
      "message": "String should have at least 1 character",
      "type": "string_too_short"
    }
  ],
  "request_id": "01a11666-5c22-7584-8273-77dc4c41fab7",
  "status": 422,
  "title": "Invalid request",
  "type": "https://corridor.example/problems/invalid-request"
}
```

A limit refusal names the limit that was hit and whose it is:

```http
HTTP 422

{
  "code": "limit_exceeded",
  "detail": "This is more than the limit of 1000.00 USD for one movement.",
  "limit": "per_transaction",
  "request_id": "01a11666-80b7-773c-ac46-49aa50bf254d",
  "scope": "account",
  "status": 422,
  "title": "Limit exceeded",
  "type": "https://corridor.example/problems/limit-exceeded"
}
```

An unknown path and a wrong method:

```http
HTTP 404

{
  "code": "not_found",
  "request_id": "01a11666-c104-74e5-a773-691a1c7a8dc6",
  "status": 404,
  "title": "Not Found",
  "type": "https://corridor.example/problems/not-found"
}
```

```http
HTTP 405

{
  "code": "method_not_allowed",
  "request_id": "01a11666-c106-761c-ba23-ee9e202502f2",
  "status": 405,
  "title": "Method Not Allowed",
  "type": "https://corridor.example/problems/method-not-allowed"
}
```

An unexpected failure is `500 internal_error`. The body says only that the request could
not be completed and gives the request id; the cause is in the server log under that id.

### Error codes

This table is collected from the code: every `DomainError` subclass, plus the codes the API
layer assigns to framework errors.

| Code | Status | Title | Raised by |
|---|---|---|---|
| `adjustment_not_found` | 404 | Adjustment not found | ops |
| `adjustment_not_pending` | 409 | Adjustment is not pending | ops |
| `agent_key_limit_reached` | 409 | Agent key limit reached | agents |
| `agent_key_not_found` | 404 | Agent key not found | agents |
| `agent_keys_unavailable` | 503 | Agent keys unavailable | agents |
| `agent_limit_reached` | 409 | Agent limit reached | agents |
| `agent_not_active` | 409 | Agent not active | agents |
| `agent_not_found` | 404 | Agent not found | agents |
| `agent_revoked` | 409 | Agent revoked | agents |
| `amount_too_small` | 422 | Amount too small | fx |
| `approval_already_decided` | 409 | Approval request already decided | agents |
| `approval_expired` | 409 | Approval request expired | agents |
| `approval_limit_reached` | 409 | Approval request limit reached | agents |
| `approval_not_found` | 404 | Approval request not found | agents |
| `bad_request` | 400 | Bad request | api, platform |
| `beneficiary_asset_mismatch` | 422 | Beneficiary is in another asset | payments |
| `beneficiary_limit_reached` | 409 | Beneficiary limit reached | payments |
| `beneficiary_not_found` | 404 | Beneficiary not found | payments |
| `beneficiary_rejected` | 422 | Beneficiary rejected | payments |
| `conflict` | 409 | Conflict | platform |
| `conversion_not_found` | 404 | Conversion not found | fx |
| `dead_letter_not_found` | 404 | Dead letter not found | ops |
| `deposit_not_found` | 404 | Deposit not found | payments |
| `deposit_not_in_suspense` | 409 | Deposit is not in suspense | payments |
| `email_taken` | 409 | Email already registered | identity |
| `handle_taken` | 409 | Handle already taken | identity |
| `http_error` | varies | Any other HTTP status the framework raises | api |
| `idempotency_key_required` | 400 | Idempotency key required | api |
| `idempotency_key_reused` | 422 | Idempotency key reused | api, payments |
| `insufficient_funds` | 402 | Insufficient funds | ledger |
| `insufficient_scope` | 403 | Insufficient scope | identity |
| `internal_error` | 500 | Internal server error | api |
| `invalid_account` | 422 | Invalid account | payments |
| `invalid_address` | 422 | Invalid address | payments |
| `invalid_adjustment` | 422 | Invalid adjustment | ops |
| `invalid_amount` | 422 | Invalid amount | platform |
| `invalid_credentials` | 401 | Invalid credentials | api |
| `invalid_cursor` | 422 | Invalid cursor | platform |
| `invalid_expiry` | 422 | Invalid expiry | agents |
| `invalid_handle` | 422 | Invalid handle | identity |
| `invalid_idempotency_key` | 422 | Invalid idempotency key | api |
| `invalid_memo` | 422 | Invalid memo | payments |
| `invalid_note` | 422 | Invalid note | recon |
| `invalid_policy` | 422 | Invalid policy | agents |
| `invalid_request` | 422 | Invalid request | platform |
| `invalid_scopes` | 422 | Invalid scopes | agents |
| `invalid_signature` | 401 | Invalid signature | webhooks |
| `invalid_token` | 401 | Invalid token | api, identity |
| `invalid_withdrawal_target` | 422 | Invalid withdrawal target | payments |
| `limit_exceeded` | 422 | Limit exceeded | risk |
| `malformed_event` | 422 | Malformed event | webhooks |
| `method_not_allowed` | 405 | (the HTTP status phrase) | api |
| `not_found` | 404 | Not found | api, platform, webhooks |
| `party_not_allowed` | 403 | Not allowed | risk |
| `payload_too_large` | 413 | Payload too large | api, webhooks |
| `permission_denied` | 403 | Permission denied | api, platform |
| `provider_unavailable` | 503 | Provider unavailable | payments |
| `quote_already_used` | 409 | Quote already used | fx |
| `quote_expired` | 409 | Quote expired | fx |
| `quote_not_found` | 404 | Quote not found | fx |
| `rate_limited` | 429 | Too many requests | platform |
| `rate_limiter_unavailable` | 503 | Service unavailable | api |
| `rate_unavailable` | 503 | Rate unavailable | fx |
| `recipient_not_allowed` | 403 | Recipient not allowed | agents |
| `recipient_not_found` | 404 | Recipient not found | payments, risk |
| `recon_break_not_found` | 404 | Reconciliation break not found | recon |
| `recon_break_not_open` | 409 | Reconciliation break is not open | recon |
| `request_in_progress` | 409 | Request in progress | api |
| `review_already_resolved` | 409 | Review already resolved | risk |
| `review_has_no_user` | 409 | Review has no user | ops |
| `review_not_found` | 404 | Review not found | risk |
| `risk_denied` | 403 | Not allowed | risk |
| `same_asset` | 422 | Same asset | fx |
| `self_approval` | 403 | Self-approval is not allowed | ops |
| `service_unavailable` | 503 | Service unavailable | platform |
| `transfer_not_found` | 404 | Transfer not found | payments |
| `transfer_to_self` | 422 | Cannot transfer to yourself | payments |
| `unauthenticated` | 401 | Authentication required | api, platform |
| `unknown_asset` | 422 | Unknown asset | platform |
| `unsupported_asset` | 422 | Unsupported asset | payments |
| `unsupported_media_type` | 415 | (the HTTP status phrase) | api |
| `user_not_found` | 404 | User not found | identity |
| `user_restricted` | 403 | Account restricted | payments, risk |
| `wallet_not_found` | 404 | Wallet not found | wallets |
| `weak_password` | 422 | Weak password | identity |
| `withdrawal_not_cancelable` | 409 | Withdrawal cannot be canceled | payments |
| `withdrawal_not_found` | 404 | Withdrawal not found | payments |

Notes on reading the table:

- A `404` for a resource (`transfer_not_found`, `deposit_not_found` and so on) is also the
  answer for a resource that exists and belongs to someone else.
- `recipient_not_found` is also the answer for a recipient whose account is closed.
- `user_restricted` does not say why the account is restricted.
- `invalid_token`, `invalid_credentials` and `invalid_signature` never say which check
  failed.
- `email_taken` is in the table because the class exists, but the registration endpoint
  does not return it. See [`POST /v1/auth/register`](#post-v1authregister).

## Idempotency

A request that creates a money movement must carry an `Idempotency-Key` header. The key
makes the request safe to retry: the first request does the work, and every later request
with the same key gets the first one's answer.

**Which endpoints need a key.** `POST /v1/transfers`, `POST /v1/withdrawals`,
`POST /v1/fx/conversions`, `POST /v1/beneficiaries`, and the five `POST` endpoints under
`/v1/admin/adjustments`. Other state-changing endpoints (cancel a withdrawal, approve or
reject an approval request, decide a review, requeue a dead letter, resolve a break) do not
take a key. Each of them changes a row from one state to another exactly once, and a
repeat finds the row already changed and is refused.

**The rules.**

- A key is 1 to 255 visible ASCII characters with no spaces. A UUID is a good key.
- A key belongs to the actor that sent it: a user, or one agent. Two actors can use the
  same key without affecting each other.
- The key is bound to a fingerprint of the request: a SHA-256 over the method, the path
  (including the ids in it) and the JSON body in canonical form. Whitespace and key order
  in the body do not matter.
- The key row and the effect of the request are written in one database transaction.
  There is no moment at which one exists without the other.
- The stored answer is the status code and body of the first completed attempt. That
  includes a refusal by a business rule: retrying a transfer that was refused for
  insufficient funds with the same key returns the same refusal. Use a new key for a new
  attempt.
- A replayed answer carries the header `Idempotent-Replayed: true`.
- Nothing is stored when the request fails before the key is read (no credential, a body
  that does not match the schema, an amount that cannot be parsed) or when the server fails
  unexpectedly. Those requests can be retried with the same key.
- Keys are deleted 24 hours after they were created. After that a key is new again.

| Situation | Answer |
|---|---|
| The header is missing | `400 idempotency_key_required` |
| The key is not 1 to 255 visible ASCII characters | `422 invalid_idempotency_key` |
| Same key, same request, first request finished | The stored answer, with `Idempotent-Replayed: true` |
| Same key, a different method, path or body | `422 idempotency_key_reused` |
| Same key, first request still running after the lock wait (5 seconds by default) | `409 request_in_progress` with `Retry-After: 1` |

A first request and its replay. The second response is the stored one and no second
conversion was made:

```http
POST /v1/fx/conversions
Authorization: Bearer <access token>
Idempotency-Key: docs-ebfcc673-b77a-4446-bab2-a463d894bf6b

{
  "quote_id": "01a11666-8014-74a0-92d2-884ee0b6abce"
}
```

```http
HTTP 201

{
  "buy_amount": "1714.48",
  "buy_asset": "MXN",
  "created_at": "2026-10-07T12:46:18.661512Z",
  "id": "01a11666-8023-73c6-9132-11ca8a50c939",
  "quote_id": "01a11666-8014-74a0-92d2-884ee0b6abce",
  "rate": "17.144877835",
  "sell_amount": "100.00",
  "sell_asset": "USD"
}
```

```http
POST /v1/fx/conversions
Authorization: Bearer <access token>
Idempotency-Key: docs-ebfcc673-b77a-4446-bab2-a463d894bf6b

{
  "quote_id": "01a11666-8014-74a0-92d2-884ee0b6abce"
}
```

```http
HTTP 201
Idempotent-Replayed: true

{
  "buy_amount": "1714.48",
  "buy_asset": "MXN",
  "created_at": "2026-10-07T12:46:18.661512Z",
  "id": "01a11666-8023-73c6-9132-11ca8a50c939",
  "quote_id": "01a11666-8014-74a0-92d2-884ee0b6abce",
  "rate": "17.144877835",
  "sell_amount": "100.00",
  "sell_asset": "USD"
}
```

The same key with a different body:

```http
POST /v1/fx/conversions
Authorization: Bearer <access token>
Idempotency-Key: docs-ebfcc673-b77a-4446-bab2-a463d894bf6b

{
  "quote_id": "ad400fe2-446f-4968-95cb-4eb0f692bcf9"
}
```

```http
HTTP 422

{
  "code": "idempotency_key_reused",
  "detail": "This idempotency key was already used for a different request.",
  "request_id": "01a11666-8058-73e2-bf3c-bcdf84ec4376",
  "status": 422,
  "title": "Idempotency key reused",
  "type": "https://corridor.example/problems/idempotency-key-reused"
}
```

No key:

```http
POST /v1/fx/conversions
Authorization: Bearer <access token>

{
  "quote_id": "01a11666-8014-74a0-92d2-884ee0b6abce"
}
```

```http
HTTP 400

{
  "code": "idempotency_key_required",
  "detail": "This request needs an Idempotency-Key header.",
  "request_id": "01a11666-8066-762a-9ce5-4a82bdfa8166",
  "status": 400,
  "title": "Idempotency key required",
  "type": "https://corridor.example/problems/idempotency-key-required"
}
```

`POST /v1/beneficiaries` is the one exception in how the key is used. The key is passed to
the bank instead of being stored by Corridor, so that no database transaction is open
during the call to the bank. A repeat returns the same beneficiary, but without the
`Idempotent-Replayed` header.

## Pagination

Every list endpoint pages by keyset cursor, newest first.

- `limit` is the page size. The default is 50 and the maximum is 200. A larger value is
  served as 200. A value below 1 is refused with `422 invalid_request`.
- The response has `items` and `next_cursor`. Send `next_cursor` back as `cursor` to get the
  next page. `next_cursor` is `null` on the last page.
- A cursor is opaque. It is tied to the list and to the owner it was issued for; a cursor
  from another list, another user or another filter is refused with `422 invalid_cursor`.
- An item created after a page was read is newer than everything on that page, so paging
  never repeats or skips an item.

```http
GET /v1/wallets/USD/entries?limit=2
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "items": [
    {
      "amount": "50.00",
      "asset": "USD",
      "balance_after": "350.00",
      "direction": "debit",
      "id": "01a11666-8097-701d-91a1-f164851a07d6",
      "kind": "transfer",
      "posted_at": "2026-10-07T12:46:18.775323Z"
    },
    {
      "amount": "100.00",
      "asset": "USD",
      "balance_after": "400.00",
      "direction": "debit",
      "id": "01a11666-8037-71b5-a1a3-0c3a7fe34cca",
      "kind": "conversion",
      "posted_at": "2026-10-07T12:46:18.679394Z"
    }
  ],
  "next_cursor": "eyJrIjoid2FsbGV0…"
}
```

```http
GET /v1/wallets/USD/entries?limit=2&cursor=eyJrIjoid2FsbGV0…
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "items": [
    {
      "amount": "500.00",
      "asset": "USD",
      "balance_after": "500.00",
      "direction": "credit",
      "id": "01a11666-6809-72c5-8c06-9a7402a093f2",
      "kind": "deposit",
      "posted_at": "2026-10-07T12:46:12.489466Z"
    }
  ],
  "next_cursor": null
}
```

A cursor from the wallet statement, sent to the transfers list:

```http
HTTP 422

{
  "code": "invalid_cursor",
  "detail": "The cursor is not valid for this list. Start again without one.",
  "request_id": "01a11666-80f9-7073-8ee3-b469864bfca2",
  "status": 422,
  "title": "Invalid cursor",
  "type": "https://corridor.example/problems/invalid-cursor"
}
```

## Rate limits

Limits are token buckets kept in Redis. A bucket of `n` per minute allows a burst of `n`
requests and refills at `n / 60` per second. A refused request gets `429 rate_limited` with
a `Retry-After` header in seconds.

| Group | Counted by | Default | Applies to |
|---|---|---|---|
| `global` | Client address | 600 per minute | Every request except `/healthz`, `/readyz` and `/metrics` |
| `auth` | Client address | 10 per minute | Every endpoint under `/v1/auth` |
| `webhooks` | Client address | 600 per minute | `POST /v1/webhooks/{provider}` |
| `money_write` | Actor (the user, or one agent) | 120 per minute | `POST` on `/v1/transfers`, `/v1/withdrawals`, `/v1/beneficiaries`, `/v1/fx` and `POST /v1/approvals/{approval_id}/approve` |
| `money_read` | Actor | 600 per minute | `GET` on `/v1/transfers`, `/v1/withdrawals`, `/v1/beneficiaries`, `/v1/fx` and `/v1/deposit-instructions` |

A client address is an IPv4 address or an IPv6 /64 network. The address comes from the
connection, or from `X-Forwarded-For` when the request arrived through a proxy listed in
`CORRIDOR_FORWARDED_ALLOW_IPS`.

A user and each of the user's agents have separate `money_write` and `money_read` buckets,
so an agent that runs away uses up its own allowance and not its owner's.

```http
POST /v1/auth/login

{
  "email": "nobody@example.com",
  "password": "<password>"
}
```

```http
HTTP 429
Retry-After: 1

{
  "code": "rate_limited",
  "request_id": "01a11666-e2e2-7376-be5b-c16e8bc2027a",
  "status": 429,
  "title": "Too many requests",
  "type": "https://corridor.example/problems/rate-limited"
}
```

**When Redis is unavailable.** The limits counted by client address fail open: the request
is served and a metric counts the failure. The `money_read` limit also fails open. The
`money_write` limit fails closed: a request that moves money is refused, because a request
that cannot be counted could be repeated without limit. The refusal looks like this (this
example is written from the code, not recorded, because the shared Redis server could not
be stopped):

```http
HTTP 503
Retry-After: 5

{
  "type": "https://corridor.example/problems/rate-limiter-unavailable",
  "title": "Service unavailable",
  "status": 503,
  "code": "rate_limiter_unavailable",
  "detail": "This cannot be done at the moment. Try again shortly.",
  "request_id": "<request id>"
}
```

Nothing was done when this is returned, so the request can be retried with the same
idempotency key.

Logins have two more controls that are not rate limits. They are described under
[`POST /v1/auth/login`](#post-v1authlogin).

## Service endpoints

These are not under `/v1` and need no credential.

### `GET /healthz`

Liveness: the process is running. It touches neither data store.

```http
GET /healthz
```

```http
HTTP 200

{
  "status": "ok"
}
```

### `GET /readyz`

Readiness: the process can serve requests. PostgreSQL is required: if it cannot be reached
the status is `unavailable` and the HTTP status is `503`. Redis is not required: if it
cannot be reached the status is `degraded` and the HTTP status is still `200`, because
reads and logins still work. Money-moving requests do not (see [Rate limits](#rate-limits)).

```http
GET /readyz
```

```http
HTTP 200

{
  "checks": {
    "postgres": "ok",
    "redis": "ok"
  },
  "status": "ok"
}
```

### `GET /metrics`

Prometheus metrics. The endpoint has no authentication and the API port is the public one,
so it answers `404` unless `CORRIDOR_METRICS_PUBLIC` is set to `true`. The worker serves its
own metrics on a separate port (`CORRIDOR_WORKER_METRICS_PORT`). This endpoint is not in
`openapi.json`.

```http
GET /metrics
```

```http
HTTP 404

{
  "code": "not_found",
  "request_id": "01a11666-5106-73c8-be72-6e6e5827181e",
  "status": 404,
  "title": "Not Found",
  "type": "https://corridor.example/problems/not-found"
}
```

### `GET /.well-known/jwks.json`

The public keys that verify access tokens, as a JSON Web Key Set. The `kid` of a key is the
RFC 7638 thumbprint of the key.

```http
GET /.well-known/jwks.json
```

```http
HTTP 200

{
  "keys": [
    {
      "alg": "ES256",
      "crv": "P-256",
      "kid": "uY40i3DWKUwoK5fQ…",
      "kty": "EC",
      "use": "sig",
      "x": "Ii034lGctYB3VrLq…",
      "y": "Z-09M8G6sKT-TPXT…"
    }
  ]
}
```

## Auth

All four endpoints under `/v1/auth` share the `auth` rate limit.

### `POST /v1/auth/register`

Creates a user and the user's wallets, one per asset, in one transaction.

- `handle` is 3 to 30 characters from lower-case letters, digits and underscores. Upper
  case is folded to lower case and a leading `@` is dropped.
- `password` is 12 to 128 characters. There are no composition rules.
- A new user has role `user`, KYC tier 0 and status `active`.

```http
POST /v1/auth/register

{
  "display_name": "Ana Lima",
  "email": "ana_829dd0@example.com",
  "handle": "ana_829dd0",
  "password": "<password>"
}
```

```http
HTTP 201

{
  "created_at": "2026-10-07T12:46:06.777751Z",
  "display_name": "Ana Lima",
  "email": "ana_829dd0@example.com",
  "handle": "ana_829dd0",
  "id": "01a11666-51b9-767b-ad38-024269d5c766",
  "kyc_tier": 0,
  "role": "user",
  "status": "active"
}
```

**An email address that is already registered also gets `201`.** The response looks like a
new user, but nothing was created, the id names no row, and logging in with the password
from that request fails like any wrong password. Answering "already registered" would let
anyone ask which addresses have an account. The refusal is written to the audit log.

A handle that is taken is refused openly, because handles are public:

```http
POST /v1/auth/register

{
  "display_name": "Someone Else",
  "email": "someone_829dd0@example.com",
  "handle": "ana_829dd0",
  "password": "<password>"
}
```

```http
HTTP 409

{
  "code": "handle_taken",
  "detail": "That handle is already taken.",
  "request_id": "01a11666-5c2d-7232-8dfa-ac7cb7ec5cbd",
  "status": 409,
  "title": "Handle already taken",
  "type": "https://corridor.example/problems/handle-taken"
}
```

### `POST /v1/auth/login`

Exchanges an email address and a password for a token pair.

```http
POST /v1/auth/login

{
  "email": "ana_829dd0@example.com",
  "password": "<password>"
}
```

```http
HTTP 200

{
  "access_token": "<access token>",
  "expires_in": 900,
  "refresh_token": "<refresh token>",
  "token_type": "Bearer"
}
```

Every failed login gets the same answer: an unknown address, a wrong password, a closed
account and a locked-out client cannot be told apart.

```http
POST /v1/auth/login

{
  "email": "ana_829dd0@example.com",
  "password": "<password>"
}
```

```http
HTTP 401
WWW-Authenticate: Bearer

{
  "code": "invalid_credentials",
  "detail": "The email address or the password is not correct.",
  "request_id": "01a11666-5b4d-75ca-a2aa-394e49499192",
  "status": 401,
  "title": "Invalid credentials",
  "type": "https://corridor.example/problems/invalid-credentials"
}
```

Two controls slow down password guessing. Both count failures against the address that was
typed, whether or not it is registered.

- **Lockout, per client.** After 5 consecutive failures for one address from one client
  (`CORRIDOR_LOGIN_LOCKOUT_THRESHOLD`), that client is refused for that address for 60
  seconds, doubling with each further failure up to one hour. While it lasts even the right
  password is refused. Other clients are not affected, so a stranger cannot lock the owner
  out.
- **Throttle, across clients.** After 10 failures for one address from any clients within
  15 minutes (`CORRIDOR_LOGIN_THROTTLE_THRESHOLD`), every answer about that address is
  delayed by 0.5 seconds, doubling with each further failure up to 8 seconds. The right
  password is delayed too, and never refused. A successful login resets the count.

The password check always runs once, with Argon2id, even for an unknown address, so that
response time does not reveal whether an account exists.

### `POST /v1/auth/refresh`

Exchanges a refresh token for a new pair. The old refresh token cannot be used again.

```http
POST /v1/auth/refresh

{
  "refresh_token": "<refresh token>"
}
```

```http
HTTP 200

{
  "access_token": "<access token>",
  "expires_in": 900,
  "refresh_token": "<refresh token>",
  "token_type": "Bearer"
}
```

Presenting the old token again ends the session. The new pair stops working too:

```http
POST /v1/auth/refresh

{
  "refresh_token": "<refresh token>"
}
```

```http
HTTP 401
WWW-Authenticate: Bearer

{
  "code": "invalid_token",
  "detail": "The refresh token is not valid.",
  "request_id": "01a11666-5cff-772d-8a3f-61f90a3b05fe",
  "status": 401,
  "title": "Invalid token",
  "type": "https://corridor.example/problems/invalid-token"
}
```

### `POST /v1/auth/logout`

Ends the session the access token belongs to. The response has no body.

```http
POST /v1/auth/logout
Authorization: Bearer <access token>
```

```http
HTTP 204
```

The same access token afterwards:

```http
GET /v1/me
Authorization: Bearer <access token>
```

```http
HTTP 401
WWW-Authenticate: Bearer

{
  "code": "invalid_token",
  "detail": "The access token is not valid.",
  "request_id": "01a11666-5de0-75a5-9c8d-07dd0c2368bb",
  "status": 401,
  "title": "Invalid token",
  "type": "https://corridor.example/problems/invalid-token"
}
```

### `GET /v1/me`

The user the credential acts for.

```http
GET /v1/me
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "created_at": "2026-10-07T12:46:06.777751Z",
  "display_name": "Ana Lima",
  "email": "ana_829dd0@example.com",
  "handle": "ana_829dd0",
  "id": "01a11666-51b9-767b-ad38-024269d5c766",
  "kyc_tier": 0,
  "role": "user",
  "status": "active"
}
```

## Wallets

A user has one wallet per asset. A wallet has an *available* balance and a *held* balance.
Held money is reserved for a withdrawal that has not finished.

### `GET /v1/wallets`

Scope: `wallet:read`.

```http
GET /v1/wallets
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "wallets": [
    {
      "asset": "BRL",
      "available": "0.00",
      "held": "0.00",
      "total": "0.00"
    },
    {
      "asset": "MXN",
      "available": "0.00",
      "held": "0.00",
      "total": "0.00"
    },
    {
      "asset": "USD",
      "available": "500.00",
      "held": "0.00",
      "total": "500.00"
    },
    {
      "asset": "USDC",
      "available": "0.000000",
      "held": "0.000000",
      "total": "0.000000"
    }
  ]
}
```

A wallet with a withdrawal in flight shows the amount and the fee under `held`:

```http
HTTP 200

{
  "wallets": [
    {
      "asset": "BRL",
      "available": "0.00",
      "held": "0.00",
      "total": "0.00"
    },
    {
      "asset": "MXN",
      "available": "0.00",
      "held": "0.00",
      "total": "0.00"
    },
    {
      "asset": "USD",
      "available": "84.75",
      "held": "40.25",
      "total": "125.00"
    },
    {
      "asset": "USDC",
      "available": "0.000000",
      "held": "0.000000",
      "total": "0.000000"
    }
  ]
}
```

### `GET /v1/wallets/{asset}/entries`

Scope: `wallet:read`. The statement of one wallet's available balance, newest first. Each
item is one journal entry as it affected this wallet. `direction` is `credit` when the
balance grew and `debit` when it shrank, and `balance_after` is the available balance once
the entry was posted. `kind` is the kind of journal entry: `deposit`, `deposit_release`,
`deposit_return`, `transfer`, `conversion`, `withdrawal_hold`, `withdrawal_release`,
`adjustment` or `reversal`.

The worked example is under [Pagination](#pagination).

## Deposits

A deposit starts outside Corridor. The user sends money to a bank account or a chain
address that Corridor got from the provider, the provider tells Corridor by webhook, and
the worker credits the wallet. There is no endpoint that creates a deposit.

### `GET /v1/deposit-instructions`

Scope: `deposits:read`. Query parameter: `asset`. Where to send an asset to deposit it. The
first request for an asset asks the provider for a virtual account (fiat) or an address
(stablecoin) and stores it. Later requests return the stored one. If the provider cannot be
reached the answer is `503 provider_unavailable`.

```http
GET /v1/deposit-instructions?asset=USD
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "asset": "USD",
  "details": {
    "account_number": "900082184665",
    "bank_name": "Sim Bank",
    "rail": "ach",
    "routing_number": "021000021"
  },
  "kind": "bank"
}
```

```http
GET /v1/deposit-instructions?asset=USDC
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "asset": "USDC",
  "details": {
    "address": "sim152hggnwx6zd2blua7ujz2gm76i4ia7lve585fb26",
    "network": "simchain"
  },
  "kind": "chain"
}
```

### `GET /v1/deposits`

Scope: `deposits:read`. The user's deposits, newest first.

| Status | Meaning |
|---|---|
| `pending` | An on-chain deposit was seen and is not final yet. Nothing has been credited. |
| `completed` | Credited to the wallet. |
| `failed` | An on-chain deposit was dropped before it was final. It was never credited. |
| `returned` | The sending bank took the deposit back. See below. |

A deposit that could not be attributed to a user, or that was held for review, is in
*suspense* and is not visible to any user until an operator releases it.

In this response the on-chain deposit has been detected and has no confirmations yet:

```http
GET /v1/deposits
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "items": [
    {
      "amount": "120.000000",
      "asset": "USDC",
      "created_at": "2026-10-07T12:46:12.729903Z",
      "id": "01a11666-68f9-7592-bac7-8e3ca6345af9",
      "kind": "chain",
      "status": "pending",
      "tx_hash": "88d191ba7dada0cb…",
      "updated_at": "2026-10-07T12:46:12.729903Z"
    },
    {
      "amount": "500.00",
      "asset": "USD",
      "created_at": "2026-10-07T12:46:12.477574Z",
      "id": "01a11666-67fd-74d2-a9ca-ae7288c0687a",
      "kind": "bank",
      "status": "completed",
      "tx_hash": null,
      "updated_at": "2026-10-07T12:46:12.500238Z"
    }
  ],
  "next_cursor": null
}
```

### `GET /v1/deposits/{deposit_id}`

Scope: `deposits:read`.

```http
GET /v1/deposits/01a11666-67fd-74d2-a9ca-ae7288c0687a
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "amount": "500.00",
  "asset": "USD",
  "created_at": "2026-10-07T12:46:12.477574Z",
  "id": "01a11666-67fd-74d2-a9ca-ae7288c0687a",
  "kind": "bank",
  "status": "completed",
  "tx_hash": null,
  "updated_at": "2026-10-07T12:46:12.500238Z"
}
```

**Returned deposits.** A bank can recall a deposit after it was credited. Corridor takes
back what is still in the wallet, first releasing any of the user's withdrawals of that
asset that have not been sent. If the wallet no longer holds the full amount, the shortfall
is recorded as a debt and the account becomes `restricted`: it can still receive money, and
it cannot transfer, convert or withdraw (`403 user_restricted`).

## FX

A conversion has two steps. A quote fixes both amounts. A conversion executes a quote
exactly as quoted or not at all.

### `POST /v1/fx/quotes`

Scope: `fx:convert`. No idempotency key: a quote moves nothing.

- `rate` is the units of the buy asset for one unit of the sell asset, after Corridor's
  spread (50 basis points by default, `CORRIDOR_FX_SPREAD_BPS`).
- `buy_amount` is rounded down to the buy asset's smallest unit. A conversion can never
  create value by rounding.
- A quote expires 30 seconds after it was made (`CORRIDOR_FX_QUOTE_TTL_SECONDS`).
- If no rate newer than 15 seconds is available the answer is `503 rate_unavailable`.

```http
POST /v1/fx/quotes
Authorization: Bearer <access token>

{
  "buy_asset": "MXN",
  "sell_amount": "100.00",
  "sell_asset": "USD"
}
```

```http
HTTP 201

{
  "buy_amount": "1714.48",
  "buy_asset": "MXN",
  "expires_at": "2026-10-07T12:46:48.644187Z",
  "id": "01a11666-8014-74a0-92d2-884ee0b6abce",
  "rate": "17.144877835",
  "sell_amount": "100.00",
  "sell_asset": "USD"
}
```

### `POST /v1/fx/conversions`

Scope: `fx:convert`. Needs an `Idempotency-Key`. Sells and buys the amounts stored on the
quote in one journal entry. Nothing is recalculated.

```http
POST /v1/fx/conversions
Authorization: Bearer <access token>
Idempotency-Key: docs-ebfcc673-b77a-4446-bab2-a463d894bf6b

{
  "quote_id": "01a11666-8014-74a0-92d2-884ee0b6abce"
}
```

```http
HTTP 201

{
  "buy_amount": "1714.48",
  "buy_asset": "MXN",
  "created_at": "2026-10-07T12:46:18.661512Z",
  "id": "01a11666-8023-73c6-9132-11ca8a50c939",
  "quote_id": "01a11666-8014-74a0-92d2-884ee0b6abce",
  "rate": "17.144877835",
  "sell_amount": "100.00",
  "sell_asset": "USD"
}
```

| Refusal | When |
|---|---|
| `404 quote_not_found` | No such quote, or it belongs to another user |
| `409 quote_already_used` | The quote was converted before |
| `409 quote_expired` | The quote is older than its lifetime |
| `402 insufficient_funds` | The wallet does not hold the sell amount |
| `403 user_restricted` | The account is restricted or closed |
| `422 limit_exceeded` | The sell amount is over a limit. Conversions count toward the daily limit |

```http
POST /v1/fx/conversions
Authorization: Bearer <access token>
Idempotency-Key: docs-879cf34c-48cb-4e8a-ad64-cfc9672334e9

{
  "quote_id": "01a11666-8014-74a0-92d2-884ee0b6abce"
}
```

```http
HTTP 409

{
  "code": "quote_already_used",
  "detail": "This quote has already been converted.",
  "request_id": "01a11666-8070-7395-a9ad-763b2ff57e2b",
  "status": 409,
  "title": "Quote already used",
  "type": "https://corridor.example/problems/quote-already-used"
}
```

An agent can convert only if its owner has set a policy for it, and only up to the
policy's per-transaction cap. Conversions by an agent are never sent for approval.

### `GET /v1/fx/conversions/{conversion_id}`

Scope: `fx:read`.

```http
GET /v1/fx/conversions/01a11666-8023-73c6-9132-11ca8a50c939
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "buy_amount": "1714.48",
  "buy_asset": "MXN",
  "created_at": "2026-10-07T12:46:18.661512Z",
  "id": "01a11666-8023-73c6-9132-11ca8a50c939",
  "quote_id": "01a11666-8014-74a0-92d2-884ee0b6abce",
  "rate": "17.144877835",
  "sell_amount": "100.00",
  "sell_asset": "USD"
}
```

## Transfers

A transfer moves money between two Corridor users. It is complete when the response
arrives: the debit, the credit, the fee and the audit record are one database transaction.

### `POST /v1/transfers`

Scope: `transfers:create`. Needs an `Idempotency-Key`.

- `recipient` is a handle (with or without `@`), an email address or a user id.
- `memo` is optional, at most 140 characters, with no control characters.
- The sender pays `amount` plus `fee`. The recipient receives `amount`. The fee is
  `CORRIDOR_TRANSFER_FEE_BPS` basis points of the amount, never less than the asset's entry
  in `CORRIDOR_TRANSFER_MIN_FEE`. Both default to nothing.

```http
POST /v1/transfers
Authorization: Bearer <access token>
Idempotency-Key: docs-9014502b-9356-4aed-8e58-edf1bbd7dde0

{
  "amount": "50.00",
  "asset": "USD",
  "memo": "Lunch",
  "recipient": "@bruno_829dd0"
}
```

```http
HTTP 201

{
  "amount": "50.00",
  "asset": "USD",
  "created_at": "2026-10-07T12:46:18.778556Z",
  "fee": "0.00",
  "id": "01a11666-808e-73bd-aba0-2f3607764f0f",
  "memo": "Lunch",
  "recipient": {
    "handle": "bruno_829dd0",
    "id": "01a11666-5390-7735-ac92-2e5f3ca67233"
  },
  "sender": {
    "handle": "ana_829dd0",
    "id": "01a11666-51b9-767b-ad38-024269d5c766"
  },
  "status": "completed"
}
```

| Refusal | When |
|---|---|
| `404 recipient_not_found` | Nobody answers to the recipient, or that account is closed |
| `422 transfer_to_self` | The recipient is the sender |
| `402 insufficient_funds` | The wallet does not hold the amount plus the fee |
| `403 user_restricted` | The sender's account is restricted or closed |
| `422 limit_exceeded` | Over the per-transaction or the 24-hour limit |
| `422 invalid_amount`, `422 unknown_asset` | The amount or the asset cannot be read |

```http
POST /v1/transfers
Authorization: Bearer <access token>
Idempotency-Key: docs-dd744d2d-58d6-483e-93f5-01059e1a44f2

{
  "amount": "1.00",
  "asset": "USD",
  "recipient": "@nobody_here"
}
```

```http
HTTP 404

{
  "code": "recipient_not_found",
  "detail": "There is no such recipient.",
  "request_id": "01a11666-80c2-7234-ab5c-0c1c4ff6c337",
  "status": 404,
  "title": "Recipient not found",
  "type": "https://corridor.example/problems/recipient-not-found"
}
```

An amount sent as a JSON number, and an amount with too many decimal places:

```http
POST /v1/transfers
Authorization: Bearer <access token>
Idempotency-Key: docs-872ea34c-bcd6-4d9b-9911-b19ad17be4d6

{
  "amount": 12.5,
  "asset": "USD",
  "recipient": "01a11666-5390-7735-ac92-2e5f3ca67233"
}
```

```http
HTTP 422

{
  "code": "invalid_request",
  "detail": "The request did not match the schema for this endpoint.",
  "errors": [
    {
      "field": "body.amount",
      "message": "Input should be a valid string",
      "type": "string_type"
    }
  ],
  "request_id": "01a11666-80ca-7123-8aa0-796e649eae61",
  "status": 422,
  "title": "Invalid request",
  "type": "https://corridor.example/problems/invalid-request"
}
```

```http
POST /v1/transfers
Authorization: Bearer <access token>
Idempotency-Key: docs-78decc55-45bf-47a1-8ac7-f0e233759b97

{
  "amount": "1.005",
  "asset": "USD",
  "recipient": "01a11666-5390-7735-ac92-2e5f3ca67233"
}
```

```http
HTTP 422

{
  "code": "invalid_amount",
  "detail": "USD has 2 decimal places.",
  "request_id": "01a11666-80cf-71ff-9f9b-403fe93673fc",
  "status": 422,
  "title": "Invalid amount",
  "type": "https://corridor.example/problems/invalid-amount"
}
```

When an agent asks for more than its owner's approval threshold, nothing moves and the
answer is `202` with an approval request. See [Agents](#agents).

### `GET /v1/transfers`

Scope: `transfers:read`. Transfers the user sent and received, newest first.

```http
GET /v1/transfers?limit=1
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "items": [
    {
      "amount": "50.00",
      "asset": "USD",
      "created_at": "2026-10-07T12:46:18.778556Z",
      "fee": "0.00",
      "id": "01a11666-808e-73bd-aba0-2f3607764f0f",
      "memo": "Lunch",
      "recipient": {
        "handle": "bruno_829dd0",
        "id": "01a11666-5390-7735-ac92-2e5f3ca67233"
      },
      "sender": {
        "handle": "ana_829dd0",
        "id": "01a11666-51b9-767b-ad38-024269d5c766"
      },
      "status": "completed"
    }
  ],
  "next_cursor": null
}
```

### `GET /v1/transfers/{transfer_id}`

Scope: `transfers:read`. Either party can read a transfer.

```http
GET /v1/transfers/01a11666-808e-73bd-aba0-2f3607764f0f
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "amount": "50.00",
  "asset": "USD",
  "created_at": "2026-10-07T12:46:18.778556Z",
  "fee": "0.00",
  "id": "01a11666-808e-73bd-aba0-2f3607764f0f",
  "memo": "Lunch",
  "recipient": {
    "handle": "bruno_829dd0",
    "id": "01a11666-5390-7735-ac92-2e5f3ca67233"
  },
  "sender": {
    "handle": "ana_829dd0",
    "id": "01a11666-51b9-767b-ad38-024269d5c766"
  },
  "status": "completed"
}
```

## Beneficiaries and withdrawals

A withdrawal sends money out of Corridor: to a saved bank account (a *beneficiary*) for a
fiat asset, or to a chain address for a stablecoin.

### `POST /v1/beneficiaries`

Scope: `beneficiaries:write`. Needs an `Idempotency-Key` (see the exception under
[Idempotency](#idempotency)). Saves a bank account to withdraw to.

The account number goes to the bank and is not stored by Corridor. Corridor keeps the
bank's token for the account and a masked value. `routing_number` is for `USD` only. For
`MXN` the account number is an 18-digit CLABE; for `BRL` it is a PIX key.

```http
POST /v1/beneficiaries
Authorization: Bearer <access token>
Idempotency-Key: docs-86fe2109-aa9e-4f61-a651-eb7d0ce318a4

{
  "account_number": "000123456789",
  "asset": "USD",
  "holder_name": "Bruno Costa",
  "routing_number": "021000021"
}
```

```http
HTTP 201

{
  "account_mask": "••••6789",
  "asset": "USD",
  "created_at": "2026-10-07T12:46:18.893648Z",
  "holder_name": "Bruno Costa",
  "id": "01a11666-810d-73e0-aa79-a6123eefddf8"
}
```

Refusals: `422 invalid_account` (the bank does not accept these details),
`422 unsupported_asset` (not a fiat asset), `409 beneficiary_limit_reached` (50 per user by
default), `403 user_restricted`, `503 provider_unavailable`.

### `GET /v1/beneficiaries`

Scope: `beneficiaries:read`.

```http
GET /v1/beneficiaries
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "items": [
    {
      "account_mask": "••••6789",
      "asset": "USD",
      "created_at": "2026-10-07T12:46:18.893648Z",
      "holder_name": "Bruno Costa",
      "id": "01a11666-810d-73e0-aa79-a6123eefddf8"
    }
  ],
  "next_cursor": null
}
```

### `POST /v1/withdrawals`

Scope: `withdrawals:create`. Needs an `Idempotency-Key`. The answer is `202`: the money is
reserved and the withdrawal is recorded, and the worker sends it to the provider
afterwards. Read the withdrawal to learn what became of it.

- A fiat asset needs `beneficiary_id`. A stablecoin needs `to_address`. Exactly one.
- The wallet is debited `amount` plus `fee`. The fee is `CORRIDOR_WITHDRAWAL_FEE_BPS` basis
  points of the amount, never less than the asset's entry in `CORRIDOR_WITHDRAWAL_MIN_FEE`
  (by default `0.25` USD, `5.00` MXN and `0.15` USDC).

```http
POST /v1/withdrawals
Authorization: Bearer <access token>
Idempotency-Key: docs-8996898b-bfc5-41d0-917d-6f3c68d8b181

{
  "amount": "40.00",
  "asset": "USD",
  "beneficiary_id": "01a11666-810d-73e0-aa79-a6123eefddf8"
}
```

```http
HTTP 202

{
  "amount": "40.00",
  "asset": "USD",
  "beneficiary_id": "01a11666-810d-73e0-aa79-a6123eefddf8",
  "created_at": "2026-10-07T12:46:18.924693Z",
  "failure_reason": null,
  "fee": "0.25",
  "id": "01a11666-8120-7111-bca0-974a4781667b",
  "kind": "bank",
  "status": "held",
  "to_address": null,
  "updated_at": "2026-10-07T12:46:18.924693Z"
}
```

An on-chain withdrawal:

```http
POST /v1/withdrawals
Authorization: Bearer <access token>
Idempotency-Key: docs-f1cd2f61-20aa-472f-a5e7-7fef60d1ac57

{
  "amount": "25.000000",
  "asset": "USDC",
  "to_address": "sim1ddddddddddddddddddddddddddddddddfbbbb6de"
}
```

```http
HTTP 202

{
  "amount": "25.000000",
  "asset": "USDC",
  "beneficiary_id": null,
  "created_at": "2026-10-07T12:46:21.341166Z",
  "failure_reason": null,
  "fee": "0.150000",
  "id": "01a11666-8a8c-76fc-a0f9-e14206cf8079",
  "kind": "chain",
  "status": "held",
  "to_address": "sim1ddddddddddddddddddddddddddddddddfbbbb6de",
  "updated_at": "2026-10-07T12:46:21.341166Z"
}
```

The statuses a withdrawal goes through:

| Status | Meaning | Final |
|---|---|---|
| `held` | The amount and the fee are reserved. Nothing has been sent. The user can still cancel. A withdrawal that screening held for review also shows as `held` until an operator decides | no |
| `submitting` | The worker is asking the provider. The provider may or may not have the payout. It can no longer be canceled | no |
| `submitted` | The provider accepted the payout | no |
| `completed` | The provider paid out. The held money is gone from the wallet | yes |
| `failed` | The payout was refused or failed, or an operator rejected its review. The amount and the fee are back in the available balance. `failure_reason` says why | yes |
| `canceled` | The user canceled it while it was `held`. The money is back | yes |

| Refusal | When |
|---|---|
| `422 invalid_withdrawal_target` | A beneficiary for a stablecoin, an address for a fiat asset, both or neither |
| `404 beneficiary_not_found` | No such beneficiary, or it belongs to another user |
| `422 beneficiary_asset_mismatch` | The beneficiary receives a different asset |
| `422 invalid_address` | Not a valid address on the network |
| `403 party_not_allowed` | The destination is on the deny list |
| `402 insufficient_funds`, `403 user_restricted`, `422 limit_exceeded` | As for a transfer |

```http
POST /v1/withdrawals
Authorization: Bearer <access token>
Idempotency-Key: docs-2ed1d588-5b1e-4803-a2f0-06c894948412

{
  "amount": "5.00",
  "asset": "USD",
  "to_address": "sim1cccccccccccccccccccccccccccccccccd93782b"
}
```

```http
HTTP 422

{
  "code": "invalid_withdrawal_target",
  "detail": "A withdrawal of this asset goes to a saved beneficiary.",
  "field": "beneficiary_id",
  "request_id": "01a11666-8a7b-7099-9a2d-67bf0ac392d7",
  "status": 422,
  "title": "Invalid withdrawal target",
  "type": "https://corridor.example/problems/invalid-withdrawal-target"
}
```

```http
HTTP 403

{
  "code": "party_not_allowed",
  "detail": "Money cannot be sent to this destination.",
  "request_id": "01a11666-b2d7-72b5-a76b-8bfd71477e34",
  "status": 403,
  "title": "Not allowed",
  "type": "https://corridor.example/problems/party-not-allowed"
}
```

### `GET /v1/withdrawals/{withdrawal_id}`

Scope: `withdrawals:read`. The same withdrawal as above, after the bank paid it out:

```http
GET /v1/withdrawals/01a11666-8120-7111-bca0-974a4781667b
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "amount": "40.00",
  "asset": "USD",
  "beneficiary_id": "01a11666-810d-73e0-aa79-a6123eefddf8",
  "created_at": "2026-10-07T12:46:18.924693Z",
  "failure_reason": null,
  "fee": "0.25",
  "id": "01a11666-8120-7111-bca0-974a4781667b",
  "kind": "bank",
  "status": "completed",
  "to_address": null,
  "updated_at": "2026-10-07T12:46:21.025779Z"
}
```

The on-chain withdrawal after three confirmations:

```http
HTTP 200

{
  "amount": "25.000000",
  "asset": "USDC",
  "beneficiary_id": null,
  "created_at": "2026-10-07T12:46:21.341166Z",
  "failure_reason": null,
  "fee": "0.150000",
  "id": "01a11666-8a8c-76fc-a0f9-e14206cf8079",
  "kind": "chain",
  "status": "completed",
  "to_address": "sim1ddddddddddddddddddddddddddddddddfbbbb6de",
  "updated_at": "2026-10-07T12:46:28.316769Z"
}
```

### `GET /v1/withdrawals`

Scope: `withdrawals:read`.

```http
GET /v1/withdrawals
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "items": [
    {
      "amount": "40.00",
      "asset": "USD",
      "beneficiary_id": "01a11666-810d-73e0-aa79-a6123eefddf8",
      "created_at": "2026-10-07T12:46:18.924693Z",
      "failure_reason": null,
      "fee": "0.25",
      "id": "01a11666-8120-7111-bca0-974a4781667b",
      "kind": "bank",
      "status": "completed",
      "to_address": null,
      "updated_at": "2026-10-07T12:46:21.025779Z"
    }
  ],
  "next_cursor": null
}
```

### `POST /v1/withdrawals/{withdrawal_id}/cancel`

Scope: `withdrawals:create`. No idempotency key. Cancels a withdrawal that is still `held`
and returns the amount and the fee to the available balance. An agent can cancel only a
withdrawal that it started itself; the user's own session can cancel any of the user's
withdrawals.

A withdrawal is `held` only for a moment unless it is waiting for a review. This one was
held for review because its beneficiary's holder name is on the deny list with the outcome
`review`:

```http
POST /v1/withdrawals/01a11666-a87c-77d6-9df4-56667c097c0c/cancel
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "amount": "20.00",
  "asset": "USD",
  "beneficiary_id": "01a11666-a864-7205-8143-c49204859c33",
  "created_at": "2026-10-07T12:46:29.011071Z",
  "failure_reason": null,
  "fee": "0.25",
  "id": "01a11666-a87c-77d6-9df4-56667c097c0c",
  "kind": "bank",
  "status": "canceled",
  "to_address": null,
  "updated_at": "2026-10-07T12:46:30.547610Z"
}
```

Once the worker has started sending a withdrawal it cannot be canceled:

```http
POST /v1/withdrawals/01a11666-8120-7111-bca0-974a4781667b/cancel
Authorization: Bearer <access token>
```

```http
HTTP 409

{
  "code": "withdrawal_not_cancelable",
  "detail": "This withdrawal can no longer be canceled.",
  "request_id": "01a11666-8a73-7345-bd52-e481cf26361b",
  "status": 409,
  "title": "Withdrawal cannot be canceled",
  "type": "https://corridor.example/problems/withdrawal-not-cancelable"
}
```

## Agents

An agent lets software act on a user's wallet without the user's password. Every endpoint
in this section and the next needs the owner's own session. An agent key is refused on all
of them, so an agent cannot create a key, widen a policy or approve its own request.

How an agent's request is decided, in order:

1. **Scope.** The key must hold the scope of the endpoint.
2. **Policy.** For a transfer or a withdrawal, the destination must be on the policy's
   list of allowed recipients, unless the policy allows any recipient. An agent with no
   policy can pay nobody and cannot convert.
3. **Per-transaction cap.** The amount, valued in US dollars, must not exceed the policy's
   `per_tx_usd`.
4. **Approval threshold.** If the amount is above `approval_threshold_usd`, nothing moves.
   The API answers `202` with an approval request and the owner decides. Conversions skip
   this step.
5. **Limits.** When the movement is made, the agent's 24-hour total is checked against
   `daily_usd`, and the owner's own limits apply as well. An agent can never move what its
   owner could not.

### `POST /v1/agents`

Creates an agent. It starts `active`, with no keys and no policy. A user can have 20 agents
that are not revoked (`CORRIDOR_MAX_AGENTS_PER_USER`); beyond that the answer is
`409 agent_limit_reached`.

```http
POST /v1/agents
Authorization: Bearer <access token>

{
  "name": "Bill payer"
}
```

```http
HTTP 201

{
  "created_at": "2026-10-07T12:46:31.658816Z",
  "id": "01a11666-b2ea-76a1-84d3-422060f8f61e",
  "keys": [],
  "name": "Bill payer",
  "status": "active"
}
```

### `POST /v1/agents/{agent_id}/keys`

Issues a key. `scopes` lists what the key may do, from the table under
[Agent API keys](#agent-api-keys). `expires_at` is optional and must include a time zone.
The `key` field of the response is the only time the key is shown. An agent can have 10
working keys (`CORRIDOR_MAX_KEYS_PER_AGENT`).

```http
POST /v1/agents/01a11666-b2ea-76a1-84d3-422060f8f61e/keys
Authorization: Bearer <access token>

{
  "scopes": [
    "wallet:read",
    "transfers:create",
    "transfers:read"
  ]
}
```

```http
HTTP 201

{
  "agent_id": "01a11666-b2ea-76a1-84d3-422060f8f61e",
  "created_at": "2026-10-07T12:46:31.666309Z",
  "expires_at": null,
  "id": "01a11666-b2f6-7221-bd5f-f0ae258f8eef",
  "key": "<agent key, shown once>",
  "last_used_at": null,
  "prefix": "gwgtkqd5tvjx",
  "revoked_at": null,
  "scopes": [
    "transfers:create",
    "transfers:read",
    "wallet:read"
  ]
}
```

Refusals: `422 invalid_scopes`, `422 invalid_expiry`, `409 agent_key_limit_reached`,
`409 agent_revoked`, and `503 agent_keys_unavailable` when the deployment has no
`CORRIDOR_API_KEY_HASH_KEY`.

### `DELETE /v1/agents/{agent_id}/keys/{key_id}`

Revokes one key for good. The response has no body.

```http
DELETE /v1/agents/01a11666-b2ea-76a1-84d3-422060f8f61e/keys/01a11666-b2ff-7031-843a-f2817a179bda
Authorization: Bearer <access token>
```

```http
HTTP 204
```

### `GET /v1/agents/{agent_id}/policy`

The agent's policy. Before the owner has set one, the policy allows nothing and
`updated_at` is `null`:

```http
GET /v1/agents/01a11666-b2ea-76a1-84d3-422060f8f61e/policy
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "agent_id": "01a11666-b2ea-76a1-84d3-422060f8f61e",
  "allowed_recipients": [],
  "any_recipient": false,
  "approval_threshold_usd": null,
  "daily_usd": null,
  "per_tx_usd": null,
  "updated_at": null
}
```

With that policy the agent is refused whatever it asks to pay:

```http
POST /v1/transfers
Authorization: Bearer <agent key>
Idempotency-Key: docs-6a8522db-d20c-4b9a-af66-bac8553eff76

{
  "amount": "5.00",
  "asset": "USD",
  "recipient": "01a11666-5390-7735-ac92-2e5f3ca67233"
}
```

```http
HTTP 403

{
  "code": "recipient_not_allowed",
  "detail": "This agent's policy does not allow it to pay this recipient.",
  "request_id": "01a11666-b314-73b3-87bd-763483c7afca",
  "status": 403,
  "title": "Recipient not allowed",
  "type": "https://corridor.example/problems/recipient-not-allowed"
}
```

### `PUT /v1/agents/{agent_id}/policy`

Replaces the policy as a whole.

- `per_tx_usd`, `daily_usd` and `approval_threshold_usd` are decimal strings in US dollars.
  All three are required. `null` means "no limit of this kind", and has to be written out.
  `"0.00"` is a real limit: it lets nothing through.
- `allowed_recipients` lists who the agent may pay: `{"kind": "user", "id": ...}` or
  `{"kind": "beneficiary", "id": ...}` for one of the owner's own beneficiaries. At most 100.
- `any_recipient: true` lets the agent pay anyone. It defaults to `false`.
- Amounts in other assets are valued in US dollars at fixed reference rates for the purpose
  of these limits.

```http
PUT /v1/agents/01a11666-b2ea-76a1-84d3-422060f8f61e/policy
Authorization: Bearer <access token>

{
  "allowed_recipients": [
    {
      "id": "01a11666-5390-7735-ac92-2e5f3ca67233",
      "kind": "user"
    }
  ],
  "approval_threshold_usd": "20.00",
  "daily_usd": "200.00",
  "per_tx_usd": "100.00"
}
```

```http
HTTP 200

{
  "agent_id": "01a11666-b2ea-76a1-84d3-422060f8f61e",
  "allowed_recipients": [
    {
      "id": "01a11666-5390-7735-ac92-2e5f3ca67233",
      "kind": "user"
    }
  ],
  "any_recipient": false,
  "approval_threshold_usd": "20.00",
  "daily_usd": "200.00",
  "per_tx_usd": "100.00",
  "updated_at": "2026-10-07T12:46:31.715824Z"
}
```

### `GET /v1/agents`

The user's agents, newest first, each with its keys (never the key text).

```http
GET /v1/agents
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "items": [
    {
      "created_at": "2026-10-07T12:46:31.658816Z",
      "id": "01a11666-b2ea-76a1-84d3-422060f8f61e",
      "keys": [
        {
          "agent_id": "01a11666-b2ea-76a1-84d3-422060f8f61e",
          "created_at": "2026-10-07T12:46:31.666309Z",
          "expires_at": null,
          "id": "01a11666-b2f6-7221-bd5f-f0ae258f8eef",
          "last_used_at": null,
          "prefix": "gwgtkqd5tvjx",
          "revoked_at": null,
          "scopes": [
            "transfers:create",
            "transfers:read",
            "wallet:read"
          ]
        },
        {
          "agent_id": "01a11666-b2ea-76a1-84d3-422060f8f61e",
          "created_at": "2026-10-07T12:46:31.678031Z",
          "expires_at": null,
          "id": "01a11666-b2ff-7031-843a-f2817a179bda",
          "last_used_at": null,
          "prefix": "9ftnupxknn11",
          "revoked_at": null,
          "scopes": [
            "wallet:read"
          ]
        },
        {
          "agent_id": "01a11666-b2ea-76a1-84d3-422060f8f61e",
          "created_at": "2026-10-07T12:46:31.686931Z",
          "expires_at": null,
          "id": "01a11666-b309-7510-b52c-3bc711f02050",
          "last_used_at": "2026-10-07T12:46:31.702966Z",
          "prefix": "q3otmlm2y1z9",
          "revoked_at": null,
          "scopes": [
            "transfers:create",
            "transfers:read",
            "wallet:read"
          ]
        }
      ],
      "name": "Bill payer",
      "status": "active"
    }
  ],
  "next_cursor": null
}
```

### An agent using its key

The agent reads the wallet with `wallet:read`:

```http
GET /v1/wallets
Authorization: Bearer <agent key>
```

```http
HTTP 200

{
  "wallets": [
    {
      "asset": "BRL",
      "available": "0.00",
      "held": "0.00",
      "total": "0.00"
    }
  ]
}
```

The response had 4 entries in `wallets`; the first 1 are shown.

A transfer under the threshold moves at once:

```http
POST /v1/transfers
Authorization: Bearer <agent key>
Idempotency-Key: docs-648139ef-3787-4867-b89f-5a9d5eaa1bb7

{
  "amount": "10.00",
  "asset": "USD",
  "recipient": "01a11666-5390-7735-ac92-2e5f3ca67233"
}
```

```http
HTTP 201

{
  "amount": "10.00",
  "asset": "USD",
  "created_at": "2026-10-07T12:46:31.780080Z",
  "fee": "0.00",
  "id": "01a11666-b358-7358-8529-83644950daca",
  "memo": null,
  "recipient": {
    "handle": "bruno_829dd0",
    "id": "01a11666-5390-7735-ac92-2e5f3ca67233"
  },
  "sender": {
    "handle": "ana_829dd0",
    "id": "01a11666-51b9-767b-ad38-024269d5c766"
  },
  "status": "completed"
}
```

A recipient that is not on the list:

```http
HTTP 403

{
  "code": "recipient_not_allowed",
  "detail": "This agent's policy does not allow it to pay this recipient.",
  "request_id": "01a11666-b369-7380-af23-c5cf3b52549d",
  "status": 403,
  "title": "Recipient not allowed",
  "type": "https://corridor.example/problems/recipient-not-allowed"
}
```

An amount over the per-transaction cap is refused outright, and the owner is not asked:

```http
HTTP 422

{
  "code": "limit_exceeded",
  "detail": "This is more than the limit of 100.00 USD for one movement.",
  "limit": "per_transaction",
  "request_id": "01a11666-b375-743e-b14c-571ebc554266",
  "scope": "agent",
  "status": 422,
  "title": "Limit exceeded",
  "type": "https://corridor.example/problems/limit-exceeded"
}
```

An amount over the approval threshold moves nothing. The `202` carries the approval
request that the owner will see. The agent has at most 20 requests waiting at a time
(`CORRIDOR_MAX_PENDING_APPROVALS_PER_AGENT`); beyond that it gets
`409 approval_limit_reached`.

```http
POST /v1/transfers
Authorization: Bearer <agent key>
Idempotency-Key: docs-4c69eeeb-a3aa-4b1b-925b-48f20bd8eeee

{
  "amount": "30.00",
  "asset": "USD",
  "memo": "Electricity",
  "recipient": "01a11666-5390-7735-ac92-2e5f3ca67233"
}
```

```http
HTTP 202

{
  "approval_request": {
    "agent_id": "01a11666-b2ea-76a1-84d3-422060f8f61e",
    "created_at": "2026-10-07T12:46:31.820212Z",
    "decided_at": null,
    "expires_at": "2026-10-08T12:46:31.820212Z",
    "failure_code": null,
    "id": "01a11666-b38c-7616-aa15-833a159d46b0",
    "kind": "transfer",
    "movement_id": null,
    "request": {
      "amount": "30.00",
      "asset": "USD",
      "beneficiary_id": null,
      "memo": "Electricity",
      "recipient_id": "01a11666-5390-7735-ac92-2e5f3ca67233",
      "to_address": null
    },
    "status": "pending"
  }
}
```

### `POST /v1/agents/{agent_id}/pause`

Stops every key of the agent from working until it is resumed.

```http
POST /v1/agents/01a11666-b2ea-76a1-84d3-422060f8f61e/pause
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "created_at": "2026-10-07T12:46:31.658816Z",
  "id": "01a11666-b2ea-76a1-84d3-422060f8f61e",
  "keys": [
    {
      "agent_id": "01a11666-b2ea-76a1-84d3-422060f8f61e",
      "created_at": "2026-10-07T12:46:31.666309Z",
      "expires_at": null,
      "id": "01a11666-b2f6-7221-bd5f-f0ae258f8eef",
      "last_used_at": null,
      "prefix": "gwgtkqd5tvjx",
      "revoked_at": null,
      "scopes": [
        "transfers:create",
        "transfers:read",
        "wallet:read"
      ]
    },
    {
      "agent_id": "01a11666-b2ea-76a1-84d3-422060f8f61e",
      "created_at": "2026-10-07T12:46:31.678031Z",
      "expires_at": null,
      "id": "01a11666-b2ff-7031-843a-f2817a179bda",
      "last_used_at": null,
      "prefix": "9ftnupxknn11",
      "revoked_at": null,
      "scopes": [
        "wallet:read"
      ]
    },
    {
      "agent_id": "01a11666-b2ea-76a1-84d3-422060f8f61e",
      "created_at": "2026-10-07T12:46:31.686931Z",
      "expires_at": null,
      "id": "01a11666-b309-7510-b52c-3bc711f02050",
      "last_used_at": "2026-10-07T12:46:31.702966Z",
      "prefix": "q3otmlm2y1z9",
      "revoked_at": null,
      "scopes": [
        "transfers:create",
        "transfers:read",
        "wallet:read"
      ]
    }
  ],
  "name": "Bill payer",
  "status": "paused"
}
```

A paused agent's key:

```http
GET /v1/wallets
Authorization: Bearer <agent key>
```

```http
HTTP 401
WWW-Authenticate: Bearer

{
  "code": "unauthenticated",
  "detail": "This credential is not accepted.",
  "request_id": "01a11666-b3f4-74a8-ae1e-70b6cd1c2cd8",
  "status": 401,
  "title": "Authentication required",
  "type": "https://corridor.example/problems/unauthenticated"
}
```

### `POST /v1/agents/{agent_id}/resume`

```http
POST /v1/agents/01a11666-b2ea-76a1-84d3-422060f8f61e/resume
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "created_at": "2026-10-07T12:46:31.658816Z",
  "id": "01a11666-b2ea-76a1-84d3-422060f8f61e",
  "keys": [
    {
      "agent_id": "01a11666-b2ea-76a1-84d3-422060f8f61e",
      "created_at": "2026-10-07T12:46:31.666309Z",
      "expires_at": null,
      "id": "01a11666-b2f6-7221-bd5f-f0ae258f8eef",
      "last_used_at": null,
      "prefix": "gwgtkqd5tvjx",
      "revoked_at": null,
      "scopes": [
        "transfers:create",
        "transfers:read",
        "wallet:read"
      ]
    }
  ],
  "name": "Bill payer",
  "status": "active"
}
```

The response had 3 entries in `keys`; the first 1 are shown.

### `POST /v1/agents/{agent_id}/revoke`

Stops the agent for good. It cannot be resumed.

```http
POST /v1/agents/01a11666-b2ea-76a1-84d3-422060f8f61e/revoke
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "created_at": "2026-10-07T12:46:31.658816Z",
  "id": "01a11666-b2ea-76a1-84d3-422060f8f61e",
  "keys": [
    {
      "agent_id": "01a11666-b2ea-76a1-84d3-422060f8f61e",
      "created_at": "2026-10-07T12:46:31.666309Z",
      "expires_at": null,
      "id": "01a11666-b2f6-7221-bd5f-f0ae258f8eef",
      "last_used_at": null,
      "prefix": "gwgtkqd5tvjx",
      "revoked_at": null,
      "scopes": [
        "transfers:create",
        "transfers:read",
        "wallet:read"
      ]
    }
  ],
  "name": "Bill payer",
  "status": "revoked"
}
```

The response had 3 entries in `keys`; the first 1 are shown.

```http
POST /v1/agents/01a11666-b2ea-76a1-84d3-422060f8f61e/resume
Authorization: Bearer <access token>
```

```http
HTTP 409

{
  "code": "agent_revoked",
  "detail": "This agent has been revoked, and that cannot be undone.",
  "request_id": "01a11666-b40e-7341-bcc9-81bb9b6c5964",
  "status": 409,
  "title": "Agent revoked",
  "type": "https://corridor.example/problems/agent-revoked"
}
```

## Approvals

### `GET /v1/approvals`

What the user's agents asked to move above their thresholds, newest first.

| Status | Meaning |
|---|---|
| `pending` | Waiting for the owner. It expires 24 hours after it was made |
| `executed` | Approved, and the movement was made. `movement_id` is the transfer or the withdrawal |
| `failed` | Approved, and the movement was then refused. `failure_code` is the code of the refusal |
| `rejected` | The owner said no |
| `expired` | Nobody decided in time |

```http
GET /v1/approvals
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "items": [
    {
      "agent_id": "01a11666-b2ea-76a1-84d3-422060f8f61e",
      "created_at": "2026-10-07T12:46:31.840177Z",
      "decided_at": null,
      "expires_at": "2026-10-08T12:46:31.840177Z",
      "failure_code": null,
      "id": "01a11666-b3a0-73cd-a201-fd25d89028b9",
      "kind": "transfer",
      "movement_id": null,
      "request": {
        "amount": "25.00",
        "asset": "USD",
        "beneficiary_id": null,
        "memo": null,
        "recipient_id": "01a11666-5390-7735-ac92-2e5f3ca67233",
        "to_address": null
      },
      "status": "pending"
    },
    {
      "agent_id": "01a11666-b2ea-76a1-84d3-422060f8f61e",
      "created_at": "2026-10-07T12:46:31.820212Z",
      "decided_at": null,
      "expires_at": "2026-10-08T12:46:31.820212Z",
      "failure_code": null,
      "id": "01a11666-b38c-7616-aa15-833a159d46b0",
      "kind": "transfer",
      "movement_id": null,
      "request": {
        "amount": "30.00",
        "asset": "USD",
        "beneficiary_id": null,
        "memo": "Electricity",
        "recipient_id": "01a11666-5390-7735-ac92-2e5f3ca67233",
        "to_address": null
      },
      "status": "pending"
    }
  ],
  "next_cursor": null
}
```

### `POST /v1/approvals/{approval_id}/approve`

Approves a request and makes its movement in the same transaction, as the agent acting for
its owner. No idempotency key: a request is decided once, and its movement was given an id
when the request was made, so a second approval cannot move the money again. This endpoint
counts against the `money_write` rate limit.

The policy and the limits are checked again at approval, because they may have changed
since the agent asked. If the movement is refused now (for example
`402 insufficient_funds`), the request is recorded as `failed` and the answer is that
refusal. If the agent has been paused or revoked the refusal is `409 agent_not_active`. If
the request has expired it is `409 approval_expired`.

```http
POST /v1/approvals/01a11666-b38c-7616-aa15-833a159d46b0/approve
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "agent_id": "01a11666-b2ea-76a1-84d3-422060f8f61e",
  "created_at": "2026-10-07T12:46:31.820212Z",
  "decided_at": "2026-10-07T12:46:31.869072Z",
  "expires_at": "2026-10-08T12:46:31.820212Z",
  "failure_code": null,
  "id": "01a11666-b38c-7616-aa15-833a159d46b0",
  "kind": "transfer",
  "movement_id": "01a11666-b38c-7616-aa15-833b6c6a9b64",
  "request": {
    "amount": "30.00",
    "asset": "USD",
    "beneficiary_id": null,
    "memo": "Electricity",
    "recipient_id": "01a11666-5390-7735-ac92-2e5f3ca67233",
    "to_address": null
  },
  "status": "executed"
}
```

A second approval:

```http
POST /v1/approvals/01a11666-b38c-7616-aa15-833a159d46b0/approve
Authorization: Bearer <access token>
```

```http
HTTP 409

{
  "code": "approval_already_decided",
  "detail": "This approval request has already been decided.",
  "request_id": "01a11666-b3d5-723c-ac9d-00da7aea7109",
  "status": 409,
  "title": "Approval request already decided",
  "type": "https://corridor.example/problems/approval-already-decided"
}
```

An agent trying to approve its own request:

```http
POST /v1/approvals/01a11666-b38c-7616-aa15-833a159d46b0/approve
Authorization: Bearer <agent key>
```

```http
HTTP 403

{
  "code": "insufficient_scope",
  "detail": "This action needs the account owner's own session.",
  "request_id": "01a11666-b3b0-7676-8ddb-70dcacd21879",
  "status": 403,
  "title": "Insufficient scope",
  "type": "https://corridor.example/problems/insufficient-scope"
}
```

### `POST /v1/approvals/{approval_id}/reject`

Refuses a request for good. Nothing moved, and nothing will.

```http
POST /v1/approvals/01a11666-b3a0-73cd-a201-fd25d89028b9/reject
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "agent_id": "01a11666-b2ea-76a1-84d3-422060f8f61e",
  "created_at": "2026-10-07T12:46:31.840177Z",
  "decided_at": "2026-10-07T12:46:31.908260Z",
  "expires_at": "2026-10-08T12:46:31.840177Z",
  "failure_code": null,
  "id": "01a11666-b3a0-73cd-a201-fd25d89028b9",
  "kind": "transfer",
  "movement_id": null,
  "request": {
    "amount": "25.00",
    "asset": "USD",
    "beneficiary_id": null,
    "memo": null,
    "recipient_id": "01a11666-5390-7735-ac92-2e5f3ca67233",
    "to_address": null
  },
  "status": "rejected"
}
```

## Webhooks

### `POST /v1/webhooks/{provider}`

Where a provider reports what happened to a deposit or a payout. `provider` is `simbank` or
`simcustody`; any other name is `404`. The contract for the events themselves is in
[provider-api.md](provider-api.md).

The endpoint takes no bearer credential. A delivery is authenticated by its signature:

- The header is `X-Signature: t=<unix seconds>,v1=<hex digest>`.
- The digest is HMAC-SHA256, keyed with the secret shared with that provider, over the
  bytes `<t>.<raw request body>`.
- The timestamp must be within 5 minutes of the API's clock
  (`CORRIDOR_WEBHOOK_TOLERANCE_SECONDS`).
- Each provider can have several active secrets, so a secret can be rotated without
  dropping deliveries.

What the endpoint does, in this order: read the raw body (at most 64 KiB), verify the
signature, parse the body, store the event and an outbox row in one transaction, answer
`200`. The worker applies the event afterwards. A delivery whose event id was already
stored is answered `200` and stored nothing new.

A delivery as the simulator sent it during the recorded run. The request is rebuilt from
the simulator's record of the event; the response body is the fixed acknowledgement:

```http
POST /v1/webhooks/simbank
X-Signature: t=<unix seconds>,v1=<hex digest>
```

```json
{
  "created_at": "2026-10-07T12:46:20.992924Z",
  "data": {
    "amount": "40.00",
    "asset": "USD",
    "fee": "0.25",
    "payout_id": "po_9ecb87d927e1",
    "reference": "01a11666-8120-7111-bca0-974a4781667b",
    "settled_at": "2026-10-07T12:46:20.992833Z"
  },
  "id": "evt_dd29d671dd1e",
  "type": "payout.completed"
}
```

```http
HTTP 200

{
  "received": true
}
```

A delivery with a wrong signature. The answer never says which part was wrong:

```http
POST /v1/webhooks/simbank
X-Signature: t=1791377191,v1=<hex digest>

{
  "created_at": "2026-01-15T12:00:00Z",
  "data": {},
  "id": "evt_forged",
  "type": "payout.completed"
}
```

```http
HTTP 401

{
  "code": "invalid_signature",
  "request_id": "01a11666-b416-702b-bd22-b9d6e510bfe5",
  "status": 401,
  "title": "Invalid signature",
  "type": "https://corridor.example/problems/invalid-signature"
}
```

| Refusal | When |
|---|---|
| `404 not_found` | The provider name is not one Corridor takes webhooks from |
| `401 invalid_signature` | Missing, malformed, wrong or stale signature, or no secret is configured for the provider |
| `413 payload_too_large` | The body is longer than any event |
| `422 malformed_event` | The signature is right and the body is not an event |

## Admin

Every endpoint under `/v1/admin` needs the session of a user whose role is `admin`. Any
other credential gets `403`:

```http
PUT /v1/admin/users/01a11666-5390-7735-ac92-2e5f3ca67233/kyc-tier
Authorization: Bearer <access token>

{
  "kyc_tier": 1
}
```

```http
HTTP 403

{
  "code": "permission_denied",
  "detail": "This action needs an administrator.",
  "request_id": "01a11666-a648-7442-a4db-5be5afa8f6af",
  "status": 403,
  "title": "Permission denied",
  "type": "https://corridor.example/problems/permission-denied"
}
```

What an administrator reads or changes through these endpoints is written to the audit log
in the same transaction.

### `PUT /v1/admin/users/{user_id}/kyc-tier`

Sets a user's KYC tier (0, 1 or 2). The tier decides which default limits apply. Verifying
the user's identity happens outside Corridor; this records its result.

```http
PUT /v1/admin/users/01a11666-5390-7735-ac92-2e5f3ca67233/kyc-tier
Authorization: Bearer <access token>

{
  "kyc_tier": 1
}
```

```http
HTTP 200

{
  "created_at": "2026-10-07T12:46:07.248671Z",
  "display_name": "Bruno Costa",
  "email": "bruno_829dd0@example.com",
  "handle": "bruno_829dd0",
  "id": "01a11666-5390-7735-ac92-2e5f3ca67233",
  "kyc_tier": 1,
  "role": "user",
  "status": "active"
}
```

### `POST /v1/admin/users/{user_id}/role`

Makes a user an administrator, or stops them being one. The role is `user` or `admin`. The
user's access tokens stop working at once and the next refresh issues a token with the new
role. An administrator cannot change their own role. The request and response schemas are
in [openapi.json](openapi.json).

### `POST /v1/admin/users/{user_id}/close`

Closes an account for good. Every session is revoked and every access token stops working.
A closed account cannot log in, cannot be paid, and is answered as "not found" to other
users. Closing is refused with `409` while the user has a balance that is not zero or
money on hold, and an administrator cannot close their own account. The request and
response schemas are in [openapi.json](openapi.json).

### `PUT /v1/admin/risk/limits`

Sets the limit rule for a KYC tier or for one user. `scope` is `tier` or `user`, and the
body names exactly that subject (`tier` or `user_id`). `kind` restricts the rule to
`transfer`, `withdrawal` or `conversion`; without it the rule covers every kind. Both
amounts are required, in US dollars, and `null` means "no limit of this kind".

The most specific rule wins: a user's rule over the tier's, and a rule for one kind over a
rule for every kind. The seeded tier limits are 1,000 per transaction and 2,500 per 24
hours for tier 0; 10,000 and 25,000 for tier 1; 100,000 and 250,000 for tier 2.

An agent's limits are not set here. `scope: "agent"` is refused with `409`: the owner's
policy is the only thing that writes them. A `user_id` that names no user is refused.

```http
PUT /v1/admin/risk/limits
Authorization: Bearer <access token>

{
  "daily_usd": "500.00",
  "kind": "withdrawal",
  "per_transaction_usd": "200.00",
  "scope": "user",
  "user_id": "01a11666-551a-76ea-8119-02b7371d8737"
}
```

```http
HTTP 200

{
  "agent_id": null,
  "daily_usd": "500.00",
  "id": "01a11666-a688-749a-bceb-5d36c04c204e",
  "kind": "withdrawal",
  "per_transaction_usd": "200.00",
  "scope": "user",
  "tier": null,
  "user_id": "01a11666-551a-76ea-8119-02b7371d8737"
}
```

### `GET /v1/admin/risk/limits`

Every limit rule, newest first.

```http
GET /v1/admin/risk/limits
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "items": [
    {
      "agent_id": null,
      "daily_usd": "500.00",
      "id": "01a11666-a688-749a-bceb-5d36c04c204e",
      "kind": "withdrawal",
      "per_transaction_usd": "200.00",
      "scope": "user",
      "tier": null,
      "user_id": "01a11666-551a-76ea-8119-02b7371d8737"
    },
    {
      "agent_id": null,
      "daily_usd": "250000.00",
      "id": "01970000-0000-7000-8000-000000000002",
      "kind": null,
      "per_transaction_usd": "100000.00",
      "scope": "tier",
      "tier": 2,
      "user_id": null
    }
  ],
  "next_cursor": null
}
```

The response had 4 entries in `items`; the first 2 are shown.

### `POST /v1/admin/risk/denylist`

Lists a party, or changes what the list says about one that is already on it. `kind` is
`name`, `address` or `account`. `outcome` is `deny` (refuse the movement) or `review` (hold
it for an operator). The value is stored in the normalised form that screening compares:
names ignore case and spacing, addresses ignore case, account numbers keep only letters and
digits.

```http
POST /v1/admin/risk/denylist
Authorization: Bearer <access token>

{
  "kind": "name",
  "note": "Ask compliance first",
  "outcome": "review",
  "value": "Shell  Trading LLC"
}
```

```http
HTTP 201

{
  "created_at": "2026-10-07T12:46:28.521093Z",
  "id": "01a11666-a6a9-71c1-b81e-30351d8e1512",
  "kind": "name",
  "note": "Ask compliance first",
  "outcome": "review",
  "value": "shell trading llc"
}
```

### `GET /v1/admin/risk/denylist`

```http
GET /v1/admin/risk/denylist
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "items": [
    {
      "created_at": "2026-10-07T12:46:28.537530Z",
      "id": "01a11666-a6b9-76f7-8092-af893882e67b",
      "kind": "name",
      "note": null,
      "outcome": "deny",
      "value": "blocked person"
    },
    {
      "created_at": "2026-10-07T12:46:28.521093Z",
      "id": "01a11666-a6a9-71c1-b81e-30351d8e1512",
      "kind": "name",
      "note": "Ask compliance first",
      "outcome": "review",
      "value": "shell trading llc"
    }
  ],
  "next_cursor": null
}
```

### `GET /v1/admin/reviews`

The reviews waiting for a decision, newest first. A review exists when screening answered
`review` or `deny` for a deposit's sender, or `review` for a withdrawal's destination.

```http
GET /v1/admin/reviews
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "items": [
    {
      "created_at": "2026-10-07T12:46:28.815326Z",
      "id": "01a11666-a7cf-77e0-ad85-dff396d34a94",
      "resolved_at": null,
      "screening": "review",
      "status": "open",
      "subject_id": "01a11666-a7c5-707f-90c1-ac0f176ed058",
      "subject_type": "deposit",
      "user_id": "01a11666-551a-76ea-8119-02b7371d8737"
    }
  ],
  "next_cursor": null
}
```

### `POST /v1/admin/reviews/{review_id}/clear`

Clears a review, and its movement goes ahead. A withdrawal is sent to the provider. A
deposit is moved from suspense to the wallet of the user it arrived for.

A deposit that arrived at nobody's account has no user on its review and cannot be cleared
(`409 review_has_no_user`); release it with an adjustment. Clearing is refused with `409`
when the user's account is closed. A restricted account can still be credited.

```http
POST /v1/admin/reviews/01a11666-a7cf-77e0-ad85-dff396d34a94/clear
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "created_at": "2026-10-07T12:46:28.815326Z",
  "id": "01a11666-a7cf-77e0-ad85-dff396d34a94",
  "resolved_at": "2026-10-07T12:46:28.858323Z",
  "screening": "review",
  "status": "cleared",
  "subject_id": "01a11666-a7c5-707f-90c1-ac0f176ed058",
  "subject_type": "deposit",
  "user_id": "01a11666-551a-76ea-8119-02b7371d8737"
}
```

A review is decided once:

```http
POST /v1/admin/reviews/01a11666-a7cf-77e0-ad85-dff396d34a94/clear
Authorization: Bearer <access token>
```

```http
HTTP 409

{
  "code": "review_already_resolved",
  "detail": "This review has already been resolved.",
  "request_id": "01a11666-a816-73ab-bc4f-d0031f180f02",
  "status": 409,
  "title": "Review already resolved",
  "type": "https://corridor.example/problems/review-already-resolved"
}
```

### `POST /v1/admin/reviews/{review_id}/reject`

Rejects a review. A withdrawal that is still held is released back to its user and ends as
`failed` with the reason `review_rejected`. A deposit stays in suspense; sending it back is
an adjustment.

```http
POST /v1/admin/reviews/01a11666-aea9-75dc-8bcd-1fd6fca11051/reject
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "created_at": "2026-10-07T12:46:30.569487Z",
  "id": "01a11666-aea9-75dc-8bcd-1fd6fca11051",
  "resolved_at": "2026-10-07T12:46:31.595246Z",
  "screening": "review",
  "status": "rejected",
  "subject_id": "01a11666-ae9e-762a-8831-1e248522e770",
  "subject_type": "withdrawal",
  "user_id": "01a11666-551a-76ea-8119-02b7371d8737"
}
```

The withdrawal afterwards, as its user sees it:

```http
HTTP 200

{
  "amount": "30.00",
  "asset": "USD",
  "beneficiary_id": "01a11666-a864-7205-8143-c49204859c33",
  "created_at": "2026-10-07T12:46:30.568689Z",
  "failure_reason": "review_rejected",
  "fee": "0.25",
  "id": "01a11666-ae9e-762a-8831-1e248522e770",
  "kind": "bank",
  "status": "failed",
  "to_address": null,
  "updated_at": "2026-10-07T12:46:31.608274Z"
}
```

### `GET /v1/admin/recon/runs`

Reconciliation runs, newest first. `status` is `incomplete` when a provider could not be
read for everything the run asked. `breaks_found` counts the disagreements the run saw and
`breaks_opened` those that had no open break before it.

```http
GET /v1/admin/recon/runs?limit=2
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "items": [
    {
      "breaks_found": 1,
      "breaks_opened": 1,
      "finished_at": "2026-10-07T12:46:34.741497Z",
      "id": "01a11666-bef3-77b9-847a-947f20c20fce",
      "started_at": "2026-10-07T12:46:34.592947Z",
      "status": "completed",
      "window_end": "2026-10-07T12:46:34.592939Z",
      "window_start": "2026-10-07T11:46:34.592939Z"
    },
    {
      "breaks_found": 1,
      "breaks_opened": 1,
      "finished_at": "2026-10-07T12:46:12.138384Z",
      "id": "01a11666-66a7-70f5-81b0-0fcb61b3430f",
      "started_at": "2026-10-07T12:46:12.068066Z",
      "status": "completed",
      "window_end": "2026-10-07T12:46:12.068060Z",
      "window_start": "2026-10-07T11:46:12.068060Z"
    }
  ],
  "next_cursor": null
}
```

### `GET /v1/admin/recon/breaks`

Reconciliation breaks, newest first. Query parameter: `status` (`open` or `resolved`).
`expected` is what Corridor recorded and `actual` is what the provider reports; either can
be `null` when that side has nothing. The kinds are explained in the
[runbook](runbook.md#reconciliation-breaks).

In this run a deposit whose webhook was dropped was found and credited by reconciliation
(`resolved_by` is `system`), and a suspense return that the bank had not yet carried out
left the settlement balance 15.00 apart:

```http
GET /v1/admin/recon/breaks
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "items": [
    {
      "actual": "909.75",
      "asset": "USD",
      "created_at": "2026-10-07T12:46:34.739795Z",
      "expected": "894.75",
      "id": "01a11666-bef3-77b9-847a-9480ee4ed793",
      "kind": "settlement_balance",
      "note": null,
      "provider": "simbank",
      "provider_ref": "USD",
      "resolved_at": null,
      "resolved_by": null,
      "run_id": "01a11666-bef3-77b9-847a-947f20c20fce",
      "status": "open"
    },
    {
      "actual": "75.00",
      "asset": "USD",
      "created_at": "2026-10-07T12:46:12.135383Z",
      "expected": null,
      "id": "01a11666-66a7-70f5-81b0-0fcc0ef9f007",
      "kind": "missing_deposit",
      "note": "The deposit is on the books.",
      "provider": "simbank",
      "provider_ref": "dep_4d6aa163d94e",
      "resolved_at": "2026-10-07T12:46:12.176647Z",
      "resolved_by": "system",
      "run_id": "01a11666-66a7-70f5-81b0-0fcb61b3430f",
      "status": "resolved"
    }
  ],
  "next_cursor": null
}
```

### `POST /v1/admin/recon/breaks/{break_id}/resolve`

Closes an open break with a note of 1 to 500 characters that says why it is settled.
Resolving a break changes no money; it records a decision.

```http
POST /v1/admin/recon/breaks/01a11666-bef3-77b9-847a-9480ee4ed793/resolve
Authorization: Bearer <access token>

{
  "note": "The 15.00 USD was sent back by the bank on request; statement to follow."
}
```

```http
HTTP 200

{
  "actual": "909.75",
  "asset": "USD",
  "created_at": "2026-10-07T12:46:34.739795Z",
  "expected": "894.75",
  "id": "01a11666-bef3-77b9-847a-9480ee4ed793",
  "kind": "settlement_balance",
  "note": "The 15.00 USD was sent back by the bank on request; statement to follow.",
  "provider": "simbank",
  "provider_ref": "USD",
  "resolved_at": "2026-10-07T12:46:34.945485Z",
  "resolved_by": "01a11666-56e9-7735-83b0-8edadc81b20a",
  "run_id": "01a11666-bef3-77b9-847a-947f20c20fce",
  "status": "resolved"
}
```

```http
HTTP 409

{
  "code": "recon_break_not_open",
  "detail": "This break has already been resolved.",
  "request_id": "01a11666-bfc6-7638-b543-2952c0a1d3e1",
  "status": 409,
  "title": "Reconciliation break is not open",
  "type": "https://corridor.example/problems/recon-break-not-open"
}
```

### `GET /v1/admin/outbox/dead`

Outbox events that ran out of attempts, newest first. `last_error` is the error of the last
attempt with secrets removed.

```http
GET /v1/admin/outbox/dead
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "items": [
    {
      "attempts": 1,
      "created_at": "2026-10-07T12:46:34.960375Z",
      "finished_at": "2026-10-07T12:46:34.969742Z",
      "id": "01a11666-bfcb-72d3-bfd3-69ff826b9eaf",
      "last_error": "no handler for topic docs.unhandled_example",
      "payload": {
        "note": "no handler is registered for this topic"
      },
      "status": "dead",
      "topic": "docs.unhandled_example"
    }
  ],
  "next_cursor": null
}
```

### `POST /v1/admin/outbox/dead/{event_id}/requeue`

Gives a dead event a full set of attempts again, due at once. The worker picks it up at its
next poll, within 5 seconds by default.

```http
POST /v1/admin/outbox/dead/01a11666-bfcb-72d3-bfd3-69ff826b9eaf/requeue
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "attempts": 0,
  "created_at": "2026-10-07T12:46:34.960375Z",
  "finished_at": null,
  "id": "01a11666-bfcb-72d3-bfd3-69ff826b9eaf",
  "last_error": "no handler for topic docs.unhandled_example",
  "payload": {
    "note": "no handler is registered for this topic"
  },
  "status": "pending",
  "topic": "docs.unhandled_example"
}
```

An event that is not dead, or does not exist:

```http
HTTP 404

{
  "code": "dead_letter_not_found",
  "detail": "There is no such dead event.",
  "request_id": "01a11666-c0fe-7553-bf73-5666d83c8f3e",
  "status": 404,
  "title": "Dead letter not found",
  "type": "https://corridor.example/problems/dead-letter-not-found"
}
```

### `POST /v1/admin/adjustments`

Asks for a journal entry written by hand. Needs an `Idempotency-Key`. Nothing is posted
until a different administrator approves it.

- `reason` is 1 to 500 characters.
- `legs` is 2 to 50 postings. Each names a ledger account by id, the account's asset, a
  direction and an amount. An account appears once, and debits must equal credits in every
  asset. A request that does not balance is refused with `422 invalid_adjustment`.

```http
POST /v1/admin/adjustments
Authorization: Bearer <access token>
Idempotency-Key: docs-0e54d596-3c11-4b76-acec-08541522e37c

{
  "legs": [
    {
      "account_id": "01a11666-a7c7-7701-80e6-3b55103d361c",
      "amount": "1.00",
      "asset": "USD",
      "direction": "debit"
    },
    {
      "account_id": "01a11666-5533-7515-971d-3204f9f2a6cf",
      "amount": "1.00",
      "asset": "USD",
      "direction": "credit"
    }
  ],
  "reason": "Goodwill credit, to be rejected in this example"
}
```

```http
HTTP 201

{
  "approved_by": null,
  "created_at": "2026-10-07T12:46:34.069499Z",
  "decided_at": null,
  "entry_id": null,
  "id": "01a11666-bc54-7499-b9db-b62966e4bc92",
  "legs": [
    {
      "account_id": "01a11666-a7c7-7701-80e6-3b55103d361c",
      "amount": "1.00",
      "asset": "USD",
      "direction": "debit"
    },
    {
      "account_id": "01a11666-5533-7515-971d-3204f9f2a6cf",
      "amount": "1.00",
      "asset": "USD",
      "direction": "credit"
    }
  ],
  "reason": "Goodwill credit, to be rejected in this example",
  "requested_by": "01a11666-56e9-7735-83b0-8edadc81b20a",
  "status": "pending"
}
```

### `POST /v1/admin/adjustments/suspense-release`

Asks for a deposit in suspense to be credited to a user. Needs an `Idempotency-Key`. The
request names the deposit, and the legs are worked out by Corridor: a debit of the suspense
account and a credit of the user's available balance.

When the adjustment is approved, the deposit's row is locked and must still be in
suspense. The entry is posted and the deposit becomes `completed` and the user's, in one
transaction. A deposit that was already released, returned or taken back by its bank is
refused at approval, so the same money cannot be paid out twice. Release to a closed
account is refused with `409`.

```http
POST /v1/admin/adjustments/suspense-release
Authorization: Bearer <access token>
Idempotency-Key: docs-f94e02d9-73a5-4e36-81f1-cc43d6e83e37

{
  "amount": "60.00",
  "asset": "USD",
  "reason": "Sender confirmed the payee by phone",
  "user_id": "01a11666-551a-76ea-8119-02b7371d8737",
  "deposit_id": "<id of the deposit in suspense>"
}
```

```http
HTTP 201

{
  "approved_by": null,
  "created_at": "2026-10-07T12:46:33.981664Z",
  "decided_at": null,
  "entry_id": null,
  "id": "01a11666-bbf8-765e-be29-1a7ccc777771",
  "legs": [
    {
      "account_id": "01a11666-a7c7-7701-80e6-3b55103d361c",
      "amount": "60.00",
      "asset": "USD",
      "direction": "debit"
    },
    {
      "account_id": "01a11666-5533-7515-971d-3204f9f2a6cf",
      "amount": "60.00",
      "asset": "USD",
      "direction": "credit"
    }
  ],
  "reason": "Sender confirmed the payee by phone",
  "requested_by": "01a11666-56e9-7735-83b0-8edadc81b20a",
  "status": "pending"
}
```

### `POST /v1/admin/adjustments/suspense-return`

Asks for a deposit in suspense to be taken off the books as sent back through the provider
it arrived at. Needs an `Idempotency-Key`. Approval posts a debit of suspense and a credit
of the provider's settlement account, and marks the deposit `returned`. Sending the money
back is the operator's to do with the provider; until the provider's statement shows it,
reconciliation reports a `settlement_balance` break.

```http
POST /v1/admin/adjustments/suspense-return
Authorization: Bearer <access token>
Idempotency-Key: docs-c55c7b02-cfbc-4fe2-a70c-6f08e227e4e6

{
  "amount": "15.00",
  "asset": "USD",
  "reason": "Sender unknown; returned through the bank",
  "deposit_id": "<id of the deposit in suspense>"
}
```

```http
HTTP 201

{
  "approved_by": null,
  "created_at": "2026-10-07T12:46:34.040192Z",
  "decided_at": null,
  "entry_id": null,
  "id": "01a11666-bc31-7479-b3d4-ac2808f7ed2e",
  "legs": [
    {
      "account_id": "01a11666-a7c7-7701-80e6-3b55103d361c",
      "amount": "15.00",
      "asset": "USD",
      "direction": "debit"
    },
    {
      "account_id": "01a11666-66b3-72d1-91a8-6ac5703208f8",
      "amount": "15.00",
      "asset": "USD",
      "direction": "credit"
    }
  ],
  "reason": "Sender unknown; returned through the bank",
  "requested_by": "01a11666-56e9-7735-83b0-8edadc81b20a",
  "status": "pending"
}
```

### `POST /v1/admin/adjustments/{adjustment_id}/approve`

Approves a pending adjustment and posts its entry. Needs an `Idempotency-Key`. The approver
must not be the requester:

```http
POST /v1/admin/adjustments/01a11666-bbf8-765e-be29-1a7ccc777771/approve
Authorization: Bearer <access token>
Idempotency-Key: docs-f86bd94a-d444-467a-89bf-88881a56d1cf
```

```http
HTTP 403

{
  "code": "self_approval",
  "detail": "An adjustment is approved by a different administrator.",
  "request_id": "01a11666-bc05-7376-84a3-2429f3dee568",
  "status": 403,
  "title": "Self-approval is not allowed",
  "type": "https://corridor.example/problems/self-approval"
}
```

```http
POST /v1/admin/adjustments/01a11666-bbf8-765e-be29-1a7ccc777771/approve
Authorization: Bearer <access token>
Idempotency-Key: docs-c084c355-c0ed-469a-b556-b74cccc34ba0
```

```http
HTTP 200

{
  "approved_by": "01a11666-5868-7188-8b00-216a2267ed2d",
  "created_at": "2026-10-07T12:46:33.981664Z",
  "decided_at": "2026-10-07T12:46:34.018961Z",
  "entry_id": "01a11666-bc20-756e-9338-786ca86df502",
  "id": "01a11666-bbf8-765e-be29-1a7ccc777771",
  "legs": [
    {
      "account_id": "01a11666-a7c7-7701-80e6-3b55103d361c",
      "amount": "60.00",
      "asset": "USD",
      "direction": "debit"
    },
    {
      "account_id": "01a11666-5533-7515-971d-3204f9f2a6cf",
      "amount": "60.00",
      "asset": "USD",
      "direction": "credit"
    }
  ],
  "reason": "Sender confirmed the payee by phone",
  "requested_by": "01a11666-56e9-7735-83b0-8edadc81b20a",
  "status": "approved"
}
```

Refusals: `403 self_approval`, `409 adjustment_not_pending`, `402 insufficient_funds` when
a debited user balance no longer holds the amount, and `409 deposit_not_in_suspense` for a
suspense adjustment whose deposit has left suspense.

### `POST /v1/admin/adjustments/{adjustment_id}/reject`

Turns a pending adjustment down. Needs an `Idempotency-Key`. Any administrator can,
including the requester.

```http
POST /v1/admin/adjustments/01a11666-bc54-7499-b9db-b62966e4bc92/reject
Authorization: Bearer <access token>
Idempotency-Key: docs-3be56e1d-5f38-495d-aedd-d35e5f1cd8ea
```

```http
HTTP 200

{
  "approved_by": null,
  "created_at": "2026-10-07T12:46:34.069499Z",
  "decided_at": "2026-10-07T12:46:34.082501Z",
  "entry_id": null,
  "id": "01a11666-bc54-7499-b9db-b62966e4bc92",
  "legs": [
    {
      "account_id": "01a11666-a7c7-7701-80e6-3b55103d361c",
      "amount": "1.00",
      "asset": "USD",
      "direction": "debit"
    },
    {
      "account_id": "01a11666-5533-7515-971d-3204f9f2a6cf",
      "amount": "1.00",
      "asset": "USD",
      "direction": "credit"
    }
  ],
  "reason": "Goodwill credit, to be rejected in this example",
  "requested_by": "01a11666-56e9-7735-83b0-8edadc81b20a",
  "status": "rejected"
}
```

### `GET /v1/admin/adjustments/{adjustment_id}`

```http
GET /v1/admin/adjustments/01a11666-bbf8-765e-be29-1a7ccc777771
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "approved_by": "01a11666-5868-7188-8b00-216a2267ed2d",
  "created_at": "2026-10-07T12:46:33.981664Z",
  "decided_at": "2026-10-07T12:46:34.018961Z",
  "entry_id": "01a11666-bc20-756e-9338-786ca86df502",
  "id": "01a11666-bbf8-765e-be29-1a7ccc777771",
  "legs": [
    {
      "account_id": "01a11666-a7c7-7701-80e6-3b55103d361c",
      "amount": "60.00",
      "asset": "USD",
      "direction": "debit"
    },
    {
      "account_id": "01a11666-5533-7515-971d-3204f9f2a6cf",
      "amount": "60.00",
      "asset": "USD",
      "direction": "credit"
    }
  ],
  "reason": "Sender confirmed the payee by phone",
  "requested_by": "01a11666-56e9-7735-83b0-8edadc81b20a",
  "status": "approved"
}
```

### `GET /v1/admin/adjustments`

Adjustments, newest first. Query parameter: `status` (`pending`, `approved` or
`rejected`).

```http
GET /v1/admin/adjustments?status=approved&limit=1
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "items": [
    {
      "approved_by": "01a11666-5868-7188-8b00-216a2267ed2d",
      "created_at": "2026-10-07T12:46:34.040192Z",
      "decided_at": "2026-10-07T12:46:34.057321Z",
      "entry_id": "01a11666-bc46-76b3-ace2-dcf72501f4f2",
      "id": "01a11666-bc31-7479-b3d4-ac2808f7ed2e",
      "legs": [
        {
          "account_id": "01a11666-a7c7-7701-80e6-3b55103d361c",
          "amount": "15.00",
          "asset": "USD",
          "direction": "debit"
        },
        {
          "account_id": "01a11666-66b3-72d1-91a8-6ac5703208f8",
          "amount": "15.00",
          "asset": "USD",
          "direction": "credit"
        }
      ],
      "reason": "Sender unknown; returned through the bank",
      "requested_by": "01a11666-56e9-7735-83b0-8edadc81b20a",
      "status": "approved"
    }
  ],
  "next_cursor": "eyJrIjoiYWRqdXN0…"
}
```
