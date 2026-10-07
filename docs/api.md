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
  "request_id": "01a116dc-b0ea-70cf-a4ec-bbf0404590ea",
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
  "request_id": "01a116dd-03b6-7242-9751-9860bd195b55",
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
  "request_id": "01a116dd-03be-72f7-8cde-6bf19b3ab316",
  "status": 403,
  "title": "Insufficient scope",
  "type": "https://corridor.example/problems/insufficient-scope"
}
```

A resource that belongs to another user is answered exactly like one that does not exist,
so an id cannot be probed:

```http
GET /v1/deposits/01a116dc-bdd0-712c-a9ac-9aed171618db
Authorization: Bearer <access token>
```

```http
HTTP 404

{
  "code": "deposit_not_found",
  "detail": "There is no such deposit.",
  "request_id": "01a116dc-be11-7219-b076-7cfad4702d7e",
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
Idempotency-Key: docs-373636c7-b935-4e5e-ab80-b75bf661f57d

{
  "amount": "900.00",
  "asset": "USD",
  "recipient": "01a116dc-a089-7087-b906-78afceb61920"
}
```

```http
HTTP 402

{
  "code": "insufficient_funds",
  "detail": "Available balance is 350.00 USD; 900.00 USD is required.",
  "request_id": "01a116dc-d120-712f-b815-719e35e56de2",
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
  "request_id": "01a116dc-b18e-777c-b5b2-ca7af85e0a50",
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
  "request_id": "01a116dc-d136-726e-952f-e8e35ec275ff",
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
  "request_id": "01a116dd-0c4c-717f-9995-70468dad06b5",
  "status": 404,
  "title": "Not Found",
  "type": "https://corridor.example/problems/not-found"
}
```

```http
HTTP 405

{
  "code": "method_not_allowed",
  "request_id": "01a116dd-0c4e-73ca-861f-cdf64c2cd9b1",
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
| `account_holds_funds` | 409 | Account holds funds | ops |
| `adjustment_not_found` | 404 | Adjustment not found | ops |
| `adjustment_not_pending` | 409 | Adjustment is not pending | ops |
| `agent_key_limit_reached` | 409 | Agent key limit reached | agents |
| `agent_key_not_found` | 404 | Agent key not found | agents |
| `agent_keys_unavailable` | 503 | Agent keys unavailable | agents |
| `agent_limit_not_settable` | 409 | Agent limits are set by the owner | api |
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
| `deposit_owner_closed` | 409 | Account is closed | payments |
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
| `own_account` | 409 | Not on your own account | ops |
| `party_not_allowed` | 403 | Not allowed | risk |
| `payload_too_large` | 413 | Payload too large | api, webhooks |
| `permission_denied` | 403 | Permission denied | api, platform |
| `policy_not_set` | 403 | Agent has no policy | agents |
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
| `withdrawal_not_agents` | 403 | Withdrawal is not this agent's | payments |
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
repeat finds the row already changed and is refused. The endpoints that change a user's
standing (role, restriction, closing, KYC tier) take no key either: repeating one leaves
the user as the first request left them.

**The rules.**

- A key is 1 to 255 visible ASCII characters with no spaces. A UUID is a good key.
- A key belongs to the actor that sent it: a user, or one agent. Two actors can use the
  same key without affecting each other.
- The key is bound to a fingerprint of the request: a SHA-256 over the method, the route
  template (`/v1/admin/adjustments/{adjustment_id}/approve`), the JSON body in canonical
  form and, when the route has parameters, the path that was asked for. Whitespace and
  key order in the body do not matter. A key that was used for one adjustment is therefore
  refused for another, although both requests have the same template and an empty body.
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
Idempotency-Key: docs-453534ac-9feb-4091-a0e8-41d58aede001

{
  "quote_id": "01a116dc-d096-766e-aadb-b8847e17e173"
}
```

```http
HTTP 201

{
  "buy_amount": "1714.48",
  "buy_asset": "MXN",
  "created_at": "2026-10-07T14:55:32.521941Z",
  "id": "01a116dc-d0a7-71e5-8dfd-60f34777716d",
  "quote_id": "01a116dc-d096-766e-aadb-b8847e17e173",
  "rate": "17.144877835",
  "sell_amount": "100.00",
  "sell_asset": "USD"
}
```

```http
POST /v1/fx/conversions
Authorization: Bearer <access token>
Idempotency-Key: docs-453534ac-9feb-4091-a0e8-41d58aede001

{
  "quote_id": "01a116dc-d096-766e-aadb-b8847e17e173"
}
```

```http
HTTP 201
Idempotent-Replayed: true

{
  "buy_amount": "1714.48",
  "buy_asset": "MXN",
  "created_at": "2026-10-07T14:55:32.521941Z",
  "id": "01a116dc-d0a7-71e5-8dfd-60f34777716d",
  "quote_id": "01a116dc-d096-766e-aadb-b8847e17e173",
  "rate": "17.144877835",
  "sell_amount": "100.00",
  "sell_asset": "USD"
}
```

The same key with a different body:

```http
POST /v1/fx/conversions
Authorization: Bearer <access token>
Idempotency-Key: docs-453534ac-9feb-4091-a0e8-41d58aede001

{
  "quote_id": "623fbda2-2b42-4c45-be7d-80ad95b1edc4"
}
```

```http
HTTP 422

{
  "code": "idempotency_key_reused",
  "detail": "This idempotency key was already used for a different request.",
  "request_id": "01a116dc-d0de-7036-b5b1-1f1b9159e406",
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
  "quote_id": "01a116dc-d096-766e-aadb-b8847e17e173"
}
```

```http
HTTP 400

{
  "code": "idempotency_key_required",
  "detail": "This request needs an Idempotency-Key header.",
  "request_id": "01a116dc-d0e5-7399-8737-0af24de14d2a",
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
      "id": "01a116dc-d111-7672-8052-3f9c6a9eada5",
      "kind": "transfer",
      "posted_at": "2026-10-07T14:55:32.625295Z"
    },
    {
      "amount": "100.00",
      "asset": "USD",
      "balance_after": "400.00",
      "direction": "debit",
      "id": "01a116dc-d0c0-72ce-9bea-7d17cfb58790",
      "kind": "conversion",
      "posted_at": "2026-10-07T14:55:32.544738Z"
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
      "id": "01a116dc-bdd7-7298-9518-ca4685b5da3c",
      "kind": "deposit",
      "posted_at": "2026-10-07T14:55:27.703605Z"
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
  "request_id": "01a116dc-d18a-7387-8a6c-af6c5d1ad999",
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
| `money_write` | Actor (the user, or one agent) | 120 per minute | `POST /v1/transfers`, `POST /v1/withdrawals`, `POST /v1/beneficiaries`, `POST /v1/fx/quotes`, `POST /v1/fx/conversions` and `POST /v1/approvals/{approval_id}/approve` |
| `money_read` | Actor | 600 per minute | `GET` on `/v1/transfers`, `/v1/withdrawals`, `/v1/beneficiaries`, `/v1/fx/conversions/{conversion_id}` and `/v1/deposit-instructions`, and `POST /v1/withdrawals/{withdrawal_id}/cancel` |

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
  "request_id": "01a116dd-23ad-75f7-82e7-e6b946f6f04e",
  "status": 429,
  "title": "Too many requests",
  "type": "https://corridor.example/problems/rate-limited"
}
```

**When Redis is unavailable.** The limits counted by client address fail open: the request
is served and a metric counts the failure. The `money_read` limit also fails open. The
`money_write` limit fails closed: a request that moves money is refused, because a request
that cannot be counted could be repeated without limit. Canceling a withdrawal is counted
under `money_read` for this reason: a user must be able to stop a payout while Redis is
down, and a cancellation sends no money anywhere. The refusal looks like this (this
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
  "request_id": "01a116dc-9eb3-7102-b724-6f3ac99f3ec7",
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
      "kid": "3VA7cHwYHNgqi61L…",
      "kty": "EC",
      "use": "sig",
      "x": "ikrESjRXreLHkmee…",
      "y": "pID0AKBqOdeUiOBh…"
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
  "email": "ana_6b21b0@example.com",
  "handle": "ana_6b21b0",
  "password": "<password>"
}
```

```http
HTTP 201

{
  "created_at": "2026-10-07T14:55:19.871069Z",
  "display_name": "Ana Lima",
  "email": "ana_6b21b0@example.com",
  "handle": "ana_6b21b0",
  "id": "01a116dc-9f3f-7471-9a2e-1f15b77928e5",
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
  "email": "someone_6b21b0@example.com",
  "handle": "ana_6b21b0",
  "password": "<password>"
}
```

```http
HTTP 409

{
  "code": "handle_taken",
  "detail": "That handle is already taken.",
  "request_id": "01a116dc-b192-726f-b749-e5fce6ec3745",
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
  "email": "ana_6b21b0@example.com",
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
  "email": "ana_6b21b0@example.com",
  "password": "<password>"
}
```

```http
HTTP 401
WWW-Authenticate: Bearer

{
  "code": "invalid_credentials",
  "detail": "The email address or the password is not correct.",
  "request_id": "01a116dc-b0ed-7304-a49a-1b26415c1e3e",
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
  "request_id": "01a116dc-b23c-77f3-9b9a-4a7d4090ce4e",
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
  "request_id": "01a116dc-b2f4-760d-aac3-eccc3480de93",
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
  "created_at": "2026-10-07T14:55:19.871069Z",
  "display_name": "Ana Lima",
  "email": "ana_6b21b0@example.com",
  "handle": "ana_6b21b0",
  "id": "01a116dc-9f3f-7471-9a2e-1f15b77928e5",
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
      "created_at": "2026-10-07T14:55:27.958034Z",
      "id": "01a116dc-bed6-709e-848e-a3ed0da3415c",
      "kind": "chain",
      "status": "pending",
      "tx_hash": "88d191ba7dada0cb…",
      "updated_at": "2026-10-07T14:55:27.958034Z"
    },
    {
      "amount": "500.00",
      "asset": "USD",
      "created_at": "2026-10-07T14:55:27.696450Z",
      "id": "01a116dc-bdd0-712c-a9ac-9aed171618db",
      "kind": "bank",
      "status": "completed",
      "tx_hash": null,
      "updated_at": "2026-10-07T14:55:27.707101Z"
    }
  ],
  "next_cursor": null
}
```

### `GET /v1/deposits/{deposit_id}`

Scope: `deposits:read`.

```http
GET /v1/deposits/01a116dc-bdd0-712c-a9ac-9aed171618db
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "amount": "500.00",
  "asset": "USD",
  "created_at": "2026-10-07T14:55:27.696450Z",
  "id": "01a116dc-bdd0-712c-a9ac-9aed171618db",
  "kind": "bank",
  "status": "completed",
  "tx_hash": null,
  "updated_at": "2026-10-07T14:55:27.707101Z"
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
  "expires_at": "2026-10-07T14:56:02.502170Z",
  "id": "01a116dc-d096-766e-aadb-b8847e17e173",
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
Idempotency-Key: docs-453534ac-9feb-4091-a0e8-41d58aede001

{
  "quote_id": "01a116dc-d096-766e-aadb-b8847e17e173"
}
```

```http
HTTP 201

{
  "buy_amount": "1714.48",
  "buy_asset": "MXN",
  "created_at": "2026-10-07T14:55:32.521941Z",
  "id": "01a116dc-d0a7-71e5-8dfd-60f34777716d",
  "quote_id": "01a116dc-d096-766e-aadb-b8847e17e173",
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
Idempotency-Key: docs-118c9fce-952b-4046-9146-6b313096f617

{
  "quote_id": "01a116dc-d096-766e-aadb-b8847e17e173"
}
```

```http
HTTP 409

{
  "code": "quote_already_used",
  "detail": "This quote has already been converted.",
  "request_id": "01a116dc-d0eb-75f5-bc6a-358778171883",
  "status": 409,
  "title": "Quote already used",
  "type": "https://corridor.example/problems/quote-already-used"
}
```

An agent can convert only if its owner has set a policy for it, and only up to the
policy's per-transaction cap. Conversions by an agent are never sent for approval: one
above the approval threshold and under the cap is made. The examples below are an agent
with the scope `fx:convert`, before its owner set a policy and after the owner set one with
a cap of 100.00 USD for one movement.

Before any policy, `403 policy_not_set`:

```http
POST /v1/fx/conversions
Authorization: Bearer <agent key>
Idempotency-Key: docs-65f832e0-7a97-4246-b1f3-c173be4afbfd

{
  "quote_id": "01a116dd-0374-72fe-b633-8872b58dca37"
}
```

```http
HTTP 403

{
  "code": "policy_not_set",
  "detail": "This agent has no policy, so it cannot convert.",
  "request_id": "01a116dd-0377-7350-8b13-73d4ee2f566b",
  "status": 403,
  "title": "Agent has no policy",
  "type": "https://corridor.example/problems/policy-not-set"
}
```

Selling 150.00 USD under a cap of 100.00, `422 limit_exceeded` with `scope` set to `agent`:

```http
HTTP 422

{
  "code": "limit_exceeded",
  "detail": "This is more than the limit of 100.00 USD for one movement.",
  "limit": "per_transaction",
  "request_id": "01a116dd-03ce-7164-be37-aa429b8c84e0",
  "scope": "agent",
  "status": 422,
  "title": "Limit exceeded",
  "type": "https://corridor.example/problems/limit-exceeded"
}
```

Selling 20.00 USD:

```http
HTTP 201

{
  "buy_amount": "342.94",
  "buy_asset": "MXN",
  "created_at": "2026-10-07T14:55:45.651494Z",
  "id": "01a116dd-03f2-7589-9ab1-1758d7300a5a",
  "quote_id": "01a116dd-03e2-71ca-af05-a6f7b0c8e3e8",
  "rate": "17.14701808",
  "sell_amount": "20.00",
  "sell_asset": "USD"
}
```

### `GET /v1/fx/conversions/{conversion_id}`

Scope: `fx:read`.

```http
GET /v1/fx/conversions/01a116dc-d0a7-71e5-8dfd-60f34777716d
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "buy_amount": "1714.48",
  "buy_asset": "MXN",
  "created_at": "2026-10-07T14:55:32.521941Z",
  "id": "01a116dc-d0a7-71e5-8dfd-60f34777716d",
  "quote_id": "01a116dc-d096-766e-aadb-b8847e17e173",
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
Idempotency-Key: docs-00b0a767-3aed-48aa-aabc-405bd9b6d985

{
  "amount": "50.00",
  "asset": "USD",
  "memo": "Lunch",
  "recipient": "@bruno_6b21b0"
}
```

```http
HTTP 201

{
  "amount": "50.00",
  "asset": "USD",
  "created_at": "2026-10-07T14:55:32.629378Z",
  "fee": "0.00",
  "id": "01a116dc-d104-70bd-b387-f60ec2ba8d3f",
  "memo": "Lunch",
  "recipient": {
    "handle": "bruno_6b21b0",
    "id": "01a116dc-a089-7087-b906-78afceb61920"
  },
  "sender": {
    "handle": "ana_6b21b0",
    "id": "01a116dc-9f3f-7471-9a2e-1f15b77928e5"
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
Idempotency-Key: docs-278e31ae-3460-41d9-8c99-b78f3d9c6afe

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
  "request_id": "01a116dc-d143-753c-b459-e1aebc05ee8d",
  "status": 404,
  "title": "Recipient not found",
  "type": "https://corridor.example/problems/recipient-not-found"
}
```

An amount sent as a JSON number, and an amount with too many decimal places:

```http
POST /v1/transfers
Authorization: Bearer <access token>
Idempotency-Key: docs-85174fcc-6c87-440d-af21-3ce94c9b0cb3

{
  "amount": 12.5,
  "asset": "USD",
  "recipient": "01a116dc-a089-7087-b906-78afceb61920"
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
  "request_id": "01a116dc-d14e-77b6-aec4-ebdce1a67a11",
  "status": 422,
  "title": "Invalid request",
  "type": "https://corridor.example/problems/invalid-request"
}
```

```http
POST /v1/transfers
Authorization: Bearer <access token>
Idempotency-Key: docs-bfa57a2c-85a8-492e-83e9-3d33d0e0dd84

{
  "amount": "1.005",
  "asset": "USD",
  "recipient": "01a116dc-a089-7087-b906-78afceb61920"
}
```

```http
HTTP 422

{
  "code": "invalid_amount",
  "detail": "USD has 2 decimal places.",
  "request_id": "01a116dc-d154-7675-ae3f-b05c0a01693f",
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
      "created_at": "2026-10-07T14:55:32.629378Z",
      "fee": "0.00",
      "id": "01a116dc-d104-70bd-b387-f60ec2ba8d3f",
      "memo": "Lunch",
      "recipient": {
        "handle": "bruno_6b21b0",
        "id": "01a116dc-a089-7087-b906-78afceb61920"
      },
      "sender": {
        "handle": "ana_6b21b0",
        "id": "01a116dc-9f3f-7471-9a2e-1f15b77928e5"
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
GET /v1/transfers/01a116dc-d104-70bd-b387-f60ec2ba8d3f
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "amount": "50.00",
  "asset": "USD",
  "created_at": "2026-10-07T14:55:32.629378Z",
  "fee": "0.00",
  "id": "01a116dc-d104-70bd-b387-f60ec2ba8d3f",
  "memo": "Lunch",
  "recipient": {
    "handle": "bruno_6b21b0",
    "id": "01a116dc-a089-7087-b906-78afceb61920"
  },
  "sender": {
    "handle": "ana_6b21b0",
    "id": "01a116dc-9f3f-7471-9a2e-1f15b77928e5"
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
Idempotency-Key: docs-d8de3c5f-2818-41da-a654-d128cb7057b3

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
  "created_at": "2026-10-07T14:55:32.768893Z",
  "holder_name": "Bruno Costa",
  "id": "01a116dc-d1a0-7721-88f4-27962bfa12df"
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
      "created_at": "2026-10-07T14:55:32.768893Z",
      "holder_name": "Bruno Costa",
      "id": "01a116dc-d1a0-7721-88f4-27962bfa12df"
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
Idempotency-Key: docs-10ab0373-c710-4855-801f-857cf1daf17d

{
  "amount": "40.00",
  "asset": "USD",
  "beneficiary_id": "01a116dc-d1a0-7721-88f4-27962bfa12df"
}
```

```http
HTTP 202

{
  "amount": "40.00",
  "asset": "USD",
  "beneficiary_id": "01a116dc-d1a0-7721-88f4-27962bfa12df",
  "created_at": "2026-10-07T14:55:32.807378Z",
  "failure_reason": null,
  "fee": "0.25",
  "id": "01a116dc-d1ba-7201-8a69-771bbcd8b0c0",
  "kind": "bank",
  "status": "held",
  "to_address": null,
  "updated_at": "2026-10-07T14:55:32.807378Z"
}
```

An on-chain withdrawal:

```http
POST /v1/withdrawals
Authorization: Bearer <access token>
Idempotency-Key: docs-6b691a53-3894-46c4-9cb9-4c159183f97d

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
  "created_at": "2026-10-07T14:55:35.243320Z",
  "failure_reason": null,
  "fee": "0.150000",
  "id": "01a116dc-db3e-77c9-803a-c471e1643a62",
  "kind": "chain",
  "status": "held",
  "to_address": "sim1ddddddddddddddddddddddddddddddddfbbbb6de",
  "updated_at": "2026-10-07T14:55:35.243320Z"
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
Idempotency-Key: docs-e5f65153-c463-4378-bf8d-9f722294be92

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
  "request_id": "01a116dc-db24-703d-a6f9-5da4013520be",
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
  "request_id": "01a116dd-0319-7361-9ff0-e5aee5f742ca",
  "status": 403,
  "title": "Not allowed",
  "type": "https://corridor.example/problems/party-not-allowed"
}
```

### `GET /v1/withdrawals/{withdrawal_id}`

Scope: `withdrawals:read`. The same withdrawal as above, after the bank paid it out:

```http
GET /v1/withdrawals/01a116dc-d1ba-7201-8a69-771bbcd8b0c0
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "amount": "40.00",
  "asset": "USD",
  "beneficiary_id": "01a116dc-d1a0-7721-88f4-27962bfa12df",
  "created_at": "2026-10-07T14:55:32.807378Z",
  "failure_reason": null,
  "fee": "0.25",
  "id": "01a116dc-d1ba-7201-8a69-771bbcd8b0c0",
  "kind": "bank",
  "status": "completed",
  "to_address": null,
  "updated_at": "2026-10-07T14:55:34.998800Z"
}
```

The on-chain withdrawal after three confirmations:

```http
HTTP 200

{
  "amount": "25.000000",
  "asset": "USDC",
  "beneficiary_id": null,
  "created_at": "2026-10-07T14:55:35.243320Z",
  "failure_reason": null,
  "fee": "0.150000",
  "id": "01a116dc-db3e-77c9-803a-c471e1643a62",
  "kind": "chain",
  "status": "completed",
  "to_address": "sim1ddddddddddddddddddddddddddddddddfbbbb6de",
  "updated_at": "2026-10-07T14:55:42.267408Z"
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
      "beneficiary_id": "01a116dc-d1a0-7721-88f4-27962bfa12df",
      "created_at": "2026-10-07T14:55:32.807378Z",
      "failure_reason": null,
      "fee": "0.25",
      "id": "01a116dc-d1ba-7201-8a69-771bbcd8b0c0",
      "kind": "bank",
      "status": "completed",
      "to_address": null,
      "updated_at": "2026-10-07T14:55:34.998800Z"
    }
  ],
  "next_cursor": null
}
```

### `POST /v1/withdrawals/{withdrawal_id}/cancel`

Scope: `withdrawals:create`. No idempotency key. Cancels a withdrawal that is still `held`
and returns the amount and the fee to the available balance. An agent can cancel only a
withdrawal that it started itself: for the owner's own withdrawal, or another agent's, it
gets `403 withdrawal_not_agents`. The user's own session can cancel any of the user's
withdrawals. Canceling works while Redis is unavailable, when asking for a new withdrawal
does not (see [Rate limits](#rate-limits)).

A withdrawal is `held` only for a moment unless it is waiting for a review. This one was
held for review because its beneficiary's holder name is on the deny list with the outcome
`review`:

```http
POST /v1/withdrawals/01a116dc-f8cc-72e6-9143-947b8d9fd0d5/cancel
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "amount": "20.00",
  "asset": "USD",
  "beneficiary_id": "01a116dc-f8c2-7630-b0f0-4fd976106616",
  "created_at": "2026-10-07T14:55:42.809653Z",
  "failure_reason": null,
  "fee": "0.25",
  "id": "01a116dc-f8cc-72e6-9143-947b8d9fd0d5",
  "kind": "bank",
  "status": "canceled",
  "to_address": null,
  "updated_at": "2026-10-07T14:55:44.344207Z"
}
```

Once the worker has started sending a withdrawal it cannot be canceled:

```http
POST /v1/withdrawals/01a116dc-d1ba-7201-8a69-771bbcd8b0c0/cancel
Authorization: Bearer <access token>
```

```http
HTTP 409

{
  "code": "withdrawal_not_cancelable",
  "detail": "This withdrawal can no longer be canceled.",
  "request_id": "01a116dc-db1a-758f-b0be-6a877e5839ab",
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
   policy can pay nobody (`403 recipient_not_allowed`) and cannot convert
   (`403 policy_not_set`).
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
  "created_at": "2026-10-07T14:55:45.451895Z",
  "id": "01a116dd-032b-7784-befd-2d5892667a97",
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
POST /v1/agents/01a116dd-032b-7784-befd-2d5892667a97/keys
Authorization: Bearer <access token>

{
  "scopes": [
    "wallet:read",
    "transfers:create",
    "transfers:read",
    "fx:convert"
  ]
}
```

```http
HTTP 201

{
  "agent_id": "01a116dd-032b-7784-befd-2d5892667a97",
  "created_at": "2026-10-07T14:55:45.459641Z",
  "expires_at": null,
  "id": "01a116dd-0337-731d-a991-b69d60adf79a",
  "key": "<agent key, shown once>",
  "last_used_at": null,
  "prefix": "xiy1o4pf828n",
  "revoked_at": null,
  "scopes": [
    "fx:convert",
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
DELETE /v1/agents/01a116dd-032b-7784-befd-2d5892667a97/keys/01a116dd-0341-769f-83ae-29a535e3393b
Authorization: Bearer <access token>
```

```http
HTTP 204
```

### `GET /v1/agents/{agent_id}/policy`

The agent's policy. Before the owner has set one, the policy allows nothing and
`updated_at` is `null`:

```http
GET /v1/agents/01a116dd-032b-7784-befd-2d5892667a97/policy
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "agent_id": "01a116dd-032b-7784-befd-2d5892667a97",
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
Idempotency-Key: docs-18bf853a-6a74-4f8c-8560-643f06b6cd58

{
  "amount": "5.00",
  "asset": "USD",
  "recipient": "01a116dc-a089-7087-b906-78afceb61920"
}
```

```http
HTTP 403

{
  "code": "recipient_not_allowed",
  "detail": "This agent's policy does not allow it to pay this recipient.",
  "request_id": "01a116dd-035a-74f7-ac01-db08107ff280",
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
PUT /v1/agents/01a116dd-032b-7784-befd-2d5892667a97/policy
Authorization: Bearer <access token>

{
  "allowed_recipients": [
    {
      "id": "01a116dc-a089-7087-b906-78afceb61920",
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
  "agent_id": "01a116dd-032b-7784-befd-2d5892667a97",
  "allowed_recipients": [
    {
      "id": "01a116dc-a089-7087-b906-78afceb61920",
      "kind": "user"
    }
  ],
  "any_recipient": false,
  "approval_threshold_usd": "20.00",
  "daily_usd": "200.00",
  "per_tx_usd": "100.00",
  "updated_at": "2026-10-07T14:55:45.547430Z"
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
      "created_at": "2026-10-07T14:55:45.451895Z",
      "id": "01a116dd-032b-7784-befd-2d5892667a97",
      "keys": [
        {
          "agent_id": "01a116dd-032b-7784-befd-2d5892667a97",
          "created_at": "2026-10-07T14:55:45.459641Z",
          "expires_at": null,
          "id": "01a116dd-0337-731d-a991-b69d60adf79a",
          "last_used_at": null,
          "prefix": "xiy1o4pf828n",
          "revoked_at": null,
          "scopes": [
            "fx:convert",
            "transfers:create",
            "transfers:read",
            "wallet:read"
          ]
        },
        {
          "agent_id": "01a116dd-032b-7784-befd-2d5892667a97",
          "created_at": "2026-10-07T14:55:45.471877Z",
          "expires_at": null,
          "id": "01a116dd-0341-769f-83ae-29a535e3393b",
          "last_used_at": null,
          "prefix": "pyyk59uxk292",
          "revoked_at": null,
          "scopes": [
            "wallet:read"
          ]
        },
        {
          "agent_id": "01a116dd-032b-7784-befd-2d5892667a97",
          "created_at": "2026-10-07T14:55:45.481718Z",
          "expires_at": null,
          "id": "01a116dd-034c-7783-8266-16c585d32084",
          "last_used_at": "2026-10-07T14:55:45.501787Z",
          "prefix": "cfzjj64uw5l4",
          "revoked_at": null,
          "scopes": [
            "fx:convert",
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
Idempotency-Key: docs-56497730-6e9d-4af8-bc92-c527d0f76ae7

{
  "amount": "10.00",
  "asset": "USD",
  "recipient": "01a116dc-a089-7087-b906-78afceb61920"
}
```

```http
HTTP 201

{
  "amount": "10.00",
  "asset": "USD",
  "created_at": "2026-10-07T14:55:45.716784Z",
  "fee": "0.00",
  "id": "01a116dd-0423-7514-a57e-7b36344d4f43",
  "memo": null,
  "recipient": {
    "handle": "bruno_6b21b0",
    "id": "01a116dc-a089-7087-b906-78afceb61920"
  },
  "sender": {
    "handle": "ana_6b21b0",
    "id": "01a116dc-9f3f-7471-9a2e-1f15b77928e5"
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
  "request_id": "01a116dd-0440-7784-9220-7610ebd2257a",
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
  "request_id": "01a116dd-044e-75bd-b5d7-0033cd1fafb0",
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
Idempotency-Key: docs-bb90ea05-7fa4-477d-9ad9-c70452c7d28d

{
  "amount": "30.00",
  "asset": "USD",
  "memo": "Electricity",
  "recipient": "01a116dc-a089-7087-b906-78afceb61920"
}
```

```http
HTTP 202

{
  "approval_request": {
    "agent_id": "01a116dd-032b-7784-befd-2d5892667a97",
    "created_at": "2026-10-07T14:55:45.774988Z",
    "decided_at": null,
    "expires_at": "2026-10-08T14:55:45.774988Z",
    "failure_code": null,
    "id": "01a116dd-046e-7342-9308-91d7ea5adfc3",
    "kind": "transfer",
    "movement_id": null,
    "request": {
      "amount": "30.00",
      "asset": "USD",
      "beneficiary_id": null,
      "memo": "Electricity",
      "recipient_id": "01a116dc-a089-7087-b906-78afceb61920",
      "to_address": null
    },
    "status": "pending"
  }
}
```

### `POST /v1/agents/{agent_id}/pause`

Stops every key of the agent from working until it is resumed.

```http
POST /v1/agents/01a116dd-032b-7784-befd-2d5892667a97/pause
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "created_at": "2026-10-07T14:55:45.451895Z",
  "id": "01a116dd-032b-7784-befd-2d5892667a97",
  "keys": [
    {
      "agent_id": "01a116dd-032b-7784-befd-2d5892667a97",
      "created_at": "2026-10-07T14:55:45.459641Z",
      "expires_at": null,
      "id": "01a116dd-0337-731d-a991-b69d60adf79a",
      "last_used_at": null,
      "prefix": "xiy1o4pf828n",
      "revoked_at": null,
      "scopes": [
        "fx:convert",
        "transfers:create",
        "transfers:read",
        "wallet:read"
      ]
    },
    {
      "agent_id": "01a116dd-032b-7784-befd-2d5892667a97",
      "created_at": "2026-10-07T14:55:45.471877Z",
      "expires_at": null,
      "id": "01a116dd-0341-769f-83ae-29a535e3393b",
      "last_used_at": null,
      "prefix": "pyyk59uxk292",
      "revoked_at": null,
      "scopes": [
        "wallet:read"
      ]
    },
    {
      "agent_id": "01a116dd-032b-7784-befd-2d5892667a97",
      "created_at": "2026-10-07T14:55:45.481718Z",
      "expires_at": null,
      "id": "01a116dd-034c-7783-8266-16c585d32084",
      "last_used_at": "2026-10-07T14:55:45.501787Z",
      "prefix": "cfzjj64uw5l4",
      "revoked_at": null,
      "scopes": [
        "fx:convert",
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
  "request_id": "01a116dd-04e0-7260-b4a8-dca824d25101",
  "status": 401,
  "title": "Authentication required",
  "type": "https://corridor.example/problems/unauthenticated"
}
```

### `POST /v1/agents/{agent_id}/resume`

```http
POST /v1/agents/01a116dd-032b-7784-befd-2d5892667a97/resume
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "created_at": "2026-10-07T14:55:45.451895Z",
  "id": "01a116dd-032b-7784-befd-2d5892667a97",
  "keys": [
    {
      "agent_id": "01a116dd-032b-7784-befd-2d5892667a97",
      "created_at": "2026-10-07T14:55:45.459641Z",
      "expires_at": null,
      "id": "01a116dd-0337-731d-a991-b69d60adf79a",
      "last_used_at": null,
      "prefix": "xiy1o4pf828n",
      "revoked_at": null,
      "scopes": [
        "fx:convert",
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
POST /v1/agents/01a116dd-032b-7784-befd-2d5892667a97/revoke
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "created_at": "2026-10-07T14:55:45.451895Z",
  "id": "01a116dd-032b-7784-befd-2d5892667a97",
  "keys": [
    {
      "agent_id": "01a116dd-032b-7784-befd-2d5892667a97",
      "created_at": "2026-10-07T14:55:45.459641Z",
      "expires_at": null,
      "id": "01a116dd-0337-731d-a991-b69d60adf79a",
      "last_used_at": null,
      "prefix": "xiy1o4pf828n",
      "revoked_at": null,
      "scopes": [
        "fx:convert",
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
POST /v1/agents/01a116dd-032b-7784-befd-2d5892667a97/resume
Authorization: Bearer <access token>
```

```http
HTTP 409

{
  "code": "agent_revoked",
  "detail": "This agent has been revoked, and that cannot be undone.",
  "request_id": "01a116dd-0500-768e-a215-cf7a31a590a5",
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
      "agent_id": "01a116dd-032b-7784-befd-2d5892667a97",
      "created_at": "2026-10-07T14:55:45.798008Z",
      "decided_at": null,
      "expires_at": "2026-10-08T14:55:45.798008Z",
      "failure_code": null,
      "id": "01a116dd-0486-7201-a1a0-d0899490a5a7",
      "kind": "transfer",
      "movement_id": null,
      "request": {
        "amount": "25.00",
        "asset": "USD",
        "beneficiary_id": null,
        "memo": null,
        "recipient_id": "01a116dc-a089-7087-b906-78afceb61920",
        "to_address": null
      },
      "status": "pending"
    },
    {
      "agent_id": "01a116dd-032b-7784-befd-2d5892667a97",
      "created_at": "2026-10-07T14:55:45.774988Z",
      "decided_at": null,
      "expires_at": "2026-10-08T14:55:45.774988Z",
      "failure_code": null,
      "id": "01a116dd-046e-7342-9308-91d7ea5adfc3",
      "kind": "transfer",
      "movement_id": null,
      "request": {
        "amount": "30.00",
        "asset": "USD",
        "beneficiary_id": null,
        "memo": "Electricity",
        "recipient_id": "01a116dc-a089-7087-b906-78afceb61920",
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
POST /v1/approvals/01a116dd-046e-7342-9308-91d7ea5adfc3/approve
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "agent_id": "01a116dd-032b-7784-befd-2d5892667a97",
  "created_at": "2026-10-07T14:55:45.774988Z",
  "decided_at": "2026-10-07T14:55:45.831212Z",
  "expires_at": "2026-10-08T14:55:45.774988Z",
  "failure_code": null,
  "id": "01a116dd-046e-7342-9308-91d7ea5adfc3",
  "kind": "transfer",
  "movement_id": "01a116dd-046f-7649-b0ec-4a8e657db77f",
  "request": {
    "amount": "30.00",
    "asset": "USD",
    "beneficiary_id": null,
    "memo": "Electricity",
    "recipient_id": "01a116dc-a089-7087-b906-78afceb61920",
    "to_address": null
  },
  "status": "executed"
}
```

A second approval:

```http
POST /v1/approvals/01a116dd-046e-7342-9308-91d7ea5adfc3/approve
Authorization: Bearer <access token>
```

```http
HTTP 409

{
  "code": "approval_already_decided",
  "detail": "This approval request has already been decided.",
  "request_id": "01a116dd-04c3-737d-bcca-3493b43106c8",
  "status": 409,
  "title": "Approval request already decided",
  "type": "https://corridor.example/problems/approval-already-decided"
}
```

An agent trying to approve its own request:

```http
POST /v1/approvals/01a116dd-046e-7342-9308-91d7ea5adfc3/approve
Authorization: Bearer <agent key>
```

```http
HTTP 403

{
  "code": "insufficient_scope",
  "detail": "This action needs the account owner's own session.",
  "request_id": "01a116dd-0498-745f-968a-46a4f50ea86a",
  "status": 403,
  "title": "Insufficient scope",
  "type": "https://corridor.example/problems/insufficient-scope"
}
```

### `POST /v1/approvals/{approval_id}/reject`

Refuses a request for good. Nothing moved, and nothing will.

```http
POST /v1/approvals/01a116dd-0486-7201-a1a0-d0899490a5a7/reject
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "agent_id": "01a116dd-032b-7784-befd-2d5892667a97",
  "created_at": "2026-10-07T14:55:45.798008Z",
  "decided_at": "2026-10-07T14:55:45.874204Z",
  "expires_at": "2026-10-08T14:55:45.798008Z",
  "failure_code": null,
  "id": "01a116dd-0486-7201-a1a0-d0899490a5a7",
  "kind": "transfer",
  "movement_id": null,
  "request": {
    "amount": "25.00",
    "asset": "USD",
    "beneficiary_id": null,
    "memo": null,
    "recipient_id": "01a116dc-a089-7087-b906-78afceb61920",
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
  "created_at": "2026-10-07T14:55:34.967555Z",
  "data": {
    "amount": "40.00",
    "asset": "USD",
    "fee": "0.25",
    "payout_id": "po_9ecb87d927e1",
    "reference": "01a116dc-d1ba-7201-8a69-771bbcd8b0c0",
    "settled_at": "2026-10-07T14:55:34.967455Z"
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
X-Signature: t=1791384945,v1=<hex digest>

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
  "request_id": "01a116dd-050a-779e-b3bf-600997041694",
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
PUT /v1/admin/users/01a116dc-a089-7087-b906-78afceb61920/kyc-tier
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
  "request_id": "01a116dc-f6de-72a4-98f3-db9996970e3d",
  "status": 403,
  "title": "Permission denied",
  "type": "https://corridor.example/problems/permission-denied"
}
```

What an administrator reads or changes through these endpoints is written to the audit log
in the same transaction, and the log itself is read with
[`GET /v1/admin/audit`](#get-v1adminaudit).

The API makes an administrator only at the word of another one. The first is made from a
shell, with `corridor users make-admin --email <address> --yes`; see the
[runbook](runbook.md#tools).

### `PUT /v1/admin/users/{user_id}/kyc-tier`

Sets a user's KYC tier (0, 1 or 2). The tier decides which default limits apply. Verifying
the user's identity happens outside Corridor; this records its result.

```http
PUT /v1/admin/users/01a116dc-a089-7087-b906-78afceb61920/kyc-tier
Authorization: Bearer <access token>

{
  "kyc_tier": 1
}
```

```http
HTTP 200

{
  "created_at": "2026-10-07T14:55:20.201136Z",
  "display_name": "Bruno Costa",
  "email": "bruno_6b21b0@example.com",
  "handle": "bruno_6b21b0",
  "id": "01a116dc-a089-7087-b906-78afceb61920",
  "kyc_tier": 1,
  "role": "user",
  "status": "active"
}
```

### `POST /v1/admin/users/{user_id}/role`

Makes a user an administrator, or stops them being one. The role is `user` or `admin`. The
user's access tokens stop working at once and the next refresh issues a token with the new
role. No idempotency key: giving a user the role they have changes nothing.

```http
POST /v1/admin/users/01a116dd-0b19-74cb-b642-6d6ea4eb4875/role
Authorization: Bearer <access token>

{
  "role": "admin"
}
```

```http
HTTP 200

{
  "created_at": "2026-10-07T14:55:47.481681Z",
  "display_name": "Dora Reis",
  "email": "dora_6b21b0@example.com",
  "handle": "dora_6b21b0",
  "id": "01a116dd-0b19-74cb-b642-6d6ea4eb4875",
  "kyc_tier": 0,
  "role": "admin",
  "status": "active"
}
```

An administrator cannot change their own role (`409 own_account`). The same refusal
answers an administrator who tries to restrict or close their own account, or to lift
their own restriction:

```http
HTTP 409

{
  "code": "own_account",
  "detail": "An administrator's own role and account are changed by another.",
  "request_id": "01a116dd-0be6-71c7-86f9-a4a282fdc35f",
  "status": 409,
  "title": "Not on your own account",
  "type": "https://corridor.example/problems/own-account"
}
```

### `POST /v1/admin/users/{user_id}/restrict`

Restricts a user: they can still log in, read and be paid, and cannot transfer, convert or
withdraw until the restriction is lifted. `reason` is required, 1 to 500 characters, and is
kept on the account and in the audit log. The restriction waits for a movement of the
user's that is under way and applies to every one after it. Restricting a restricted user
replaces the reason. A closed account is refused with `409 conflict`, a user who does not
exist with `404 user_not_found`.

```http
POST /v1/admin/users/01a116dc-a089-7087-b906-78afceb61920/restrict
Authorization: Bearer <access token>

{
  "reason": "Chargeback under investigation"
}
```

```http
HTTP 200

{
  "created_at": "2026-10-07T14:55:20.201136Z",
  "display_name": "Bruno Costa",
  "email": "bruno_6b21b0@example.com",
  "handle": "bruno_6b21b0",
  "id": "01a116dc-a089-7087-b906-78afceb61920",
  "kyc_tier": 1,
  "role": "user",
  "status": "restricted"
}
```

What the user then gets for a transfer. The answer does not say why:

```http
HTTP 403

{
  "code": "user_restricted",
  "detail": "This account cannot send money at the moment.",
  "request_id": "01a116dd-0c06-7322-93d1-d6fea0f9cede",
  "status": 403,
  "title": "Account restricted",
  "type": "https://corridor.example/problems/user-restricted"
}
```

Corridor also restricts a user by itself, when a bank takes back a deposit the user has
already spent part of.

### `POST /v1/admin/users/{user_id}/lift-restriction`

Makes a restricted user active again. `reason` is required and is written to the audit
log. Lifting the restriction of a user who is not restricted changes nothing. A closed
account is refused with `409 conflict`.

```http
POST /v1/admin/users/01a116dc-a089-7087-b906-78afceb61920/lift-restriction
Authorization: Bearer <access token>

{
  "reason": "Investigation closed, nothing owed"
}
```

```http
HTTP 200

{
  "created_at": "2026-10-07T14:55:20.201136Z",
  "display_name": "Bruno Costa",
  "email": "bruno_6b21b0@example.com",
  "handle": "bruno_6b21b0",
  "id": "01a116dc-a089-7087-b906-78afceb61920",
  "kyc_tier": 1,
  "role": "user",
  "status": "active"
}
```

### `POST /v1/admin/users/{user_id}/close`

Closes an account for good. Every session is revoked and every access token stops working.
A closed account cannot log in, cannot be paid, and is answered as "not found" to other
users. Closing a closed account changes nothing. There is no request body.

```http
POST /v1/admin/users/01a116dd-0b19-74cb-b642-6d6ea4eb4875/close
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "created_at": "2026-10-07T14:55:47.481681Z",
  "display_name": "Dora Reis",
  "email": "dora_6b21b0@example.com",
  "handle": "dora_6b21b0",
  "id": "01a116dd-0b19-74cb-b642-6d6ea4eb4875",
  "kyc_tier": 0,
  "role": "user",
  "status": "closed"
}
```

Closing is refused while the user has a balance that is not zero or money on hold
(`409 account_holds_funds`), and an administrator cannot close their own account
(`409 own_account`):

```http
HTTP 409

{
  "code": "account_holds_funds",
  "detail": "An account is closed once nothing is in it and nothing is on hold.",
  "request_id": "01a116dd-0c1d-7791-9ea2-4a0329034f69",
  "status": 409,
  "title": "Account holds funds",
  "type": "https://corridor.example/problems/account-holds-funds"
}
```

### `PUT /v1/admin/risk/limits`

Sets the limit rule for a KYC tier or for one user. `scope` is `tier` or `user`, and the
body names exactly that subject (`tier` or `user_id`). `kind` restricts the rule to
`transfer`, `withdrawal` or `conversion`; without it the rule covers every kind. Both
amounts are required, in US dollars, and `null` means "no limit of this kind".

The most specific rule wins: a user's rule over the tier's, and a rule for one kind over a
rule for every kind. The seeded tier limits are 1,000 per transaction and 2,500 per 24
hours for tier 0; 10,000 and 25,000 for tier 1; 100,000 and 250,000 for tier 2.

```http
PUT /v1/admin/risk/limits
Authorization: Bearer <access token>

{
  "daily_usd": "500.00",
  "kind": "withdrawal",
  "per_transaction_usd": "200.00",
  "scope": "user",
  "user_id": "01a116dc-a1c5-7063-b579-8e23c5bf3d2e"
}
```

```http
HTTP 200

{
  "agent_id": null,
  "daily_usd": "500.00",
  "id": "01a116dc-f712-734d-a276-bbffb9e7921f",
  "kind": "withdrawal",
  "per_transaction_usd": "200.00",
  "scope": "user",
  "tier": null,
  "user_id": "01a116dc-a1c5-7063-b579-8e23c5bf3d2e"
}
```

An agent's limits are not set here. `scope: "agent"` is refused with
`409 agent_limit_not_settable`: the owner's policy is the only thing that writes them.

```http
PUT /v1/admin/risk/limits
Authorization: Bearer <access token>

{
  "agent_id": "052bea56-0c46-4f0b-9837-c65b571ca6ea",
  "daily_usd": "10.00",
  "per_transaction_usd": "10.00",
  "scope": "agent"
}
```

```http
HTTP 409

{
  "code": "agent_limit_not_settable",
  "detail": "An agent's limits are set by its owner's policy, and only there.",
  "request_id": "01a116dc-f719-755a-aa71-9dbda4a205b0",
  "status": 409,
  "title": "Agent limits are set by the owner",
  "type": "https://corridor.example/problems/agent-limit-not-settable"
}
```

A `user_id` that names no user is refused with `404 user_not_found`:

```http
HTTP 404

{
  "code": "user_not_found",
  "detail": "There is no such user.",
  "request_id": "01a116dc-f71f-719f-b8b1-c9475c61e22c",
  "status": 404,
  "title": "User not found",
  "type": "https://corridor.example/problems/user-not-found"
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
      "id": "01a116dc-f712-734d-a276-bbffb9e7921f",
      "kind": "withdrawal",
      "per_transaction_usd": "200.00",
      "scope": "user",
      "tier": null,
      "user_id": "01a116dc-a1c5-7063-b579-8e23c5bf3d2e"
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
  "created_at": "2026-10-07T14:55:42.389188Z",
  "id": "01a116dc-f735-77ed-b01c-f4f64fd5e35e",
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
      "created_at": "2026-10-07T14:55:42.403829Z",
      "id": "01a116dc-f743-7785-bdae-d64fb968cc7a",
      "kind": "name",
      "note": null,
      "outcome": "deny",
      "value": "blocked person"
    },
    {
      "created_at": "2026-10-07T14:55:42.389188Z",
      "id": "01a116dc-f735-77ed-b01c-f4f64fd5e35e",
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
      "created_at": "2026-10-07T14:55:42.536593Z",
      "id": "01a116dc-f7c8-73a0-a9b5-8ceb3a4d6672",
      "resolved_at": null,
      "screening": "review",
      "status": "open",
      "subject_id": "01a116dc-f7b8-7023-849a-c1002436e470",
      "subject_type": "deposit",
      "user_id": "01a116dc-a1c5-7063-b579-8e23c5bf3d2e"
    }
  ],
  "next_cursor": null
}
```

### `POST /v1/admin/reviews/{review_id}/clear`

Clears a review, and its movement goes ahead. A withdrawal is sent to the provider. A
deposit is moved from suspense to the wallet of the user it arrived for.

A deposit that arrived at nobody's account has no user on its review and cannot be cleared
(`409 review_has_no_user`); release it with an adjustment. Clearing is refused with
`409 deposit_owner_closed` when the user's account is closed, and with
`409 deposit_not_in_suspense` when the deposit has left suspense some other way; the review
stays open in both cases. A restricted account can still be credited.

```http
POST /v1/admin/reviews/01a116dc-f7c8-73a0-a9b5-8ceb3a4d6672/clear
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "created_at": "2026-10-07T14:55:42.536593Z",
  "id": "01a116dc-f7c8-73a0-a9b5-8ceb3a4d6672",
  "resolved_at": "2026-10-07T14:55:42.728977Z",
  "screening": "review",
  "status": "cleared",
  "subject_id": "01a116dc-f7b8-7023-849a-c1002436e470",
  "subject_type": "deposit",
  "user_id": "01a116dc-a1c5-7063-b579-8e23c5bf3d2e"
}
```

A review is decided once:

```http
POST /v1/admin/reviews/01a116dc-f7c8-73a0-a9b5-8ceb3a4d6672/clear
Authorization: Bearer <access token>
```

```http
HTTP 409

{
  "code": "review_already_resolved",
  "detail": "This review has already been resolved.",
  "request_id": "01a116dc-f89e-71df-8fde-f688395513af",
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
POST /v1/admin/reviews/01a116dc-fef2-722c-8bbc-9e3b0d524b3f/reject
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "created_at": "2026-10-07T14:55:44.370886Z",
  "id": "01a116dc-fef2-722c-8bbc-9e3b0d524b3f",
  "resolved_at": "2026-10-07T14:55:45.395359Z",
  "screening": "review",
  "status": "rejected",
  "subject_id": "01a116dc-fee4-712b-aa34-7a6527db27fc",
  "subject_type": "withdrawal",
  "user_id": "01a116dc-a1c5-7063-b579-8e23c5bf3d2e"
}
```

The withdrawal afterwards, as its user sees it:

```http
HTTP 200

{
  "amount": "30.00",
  "asset": "USD",
  "beneficiary_id": "01a116dc-f8c2-7630-b0f0-4fd976106616",
  "created_at": "2026-10-07T14:55:44.369648Z",
  "failure_reason": "review_rejected",
  "fee": "0.25",
  "id": "01a116dc-fee4-712b-aa34-7a6527db27fc",
  "kind": "bank",
  "status": "failed",
  "to_address": null,
  "updated_at": "2026-10-07T14:55:45.404634Z"
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
      "finished_at": "2026-10-07T14:55:47.112986Z",
      "id": "01a116dd-09a7-7551-853f-87e2d94795dc",
      "started_at": "2026-10-07T14:55:47.075240Z",
      "status": "completed",
      "window_end": "2026-10-07T14:55:47.073530Z",
      "window_start": "2026-10-07T13:55:47.073530Z"
    },
    {
      "breaks_found": 0,
      "breaks_opened": 0,
      "finished_at": "2026-10-07T14:55:46.046406Z",
      "id": "01a116dd-057e-7675-8fa8-ec6d35ab73a1",
      "started_at": "2026-10-07T14:55:46.001804Z",
      "status": "completed",
      "window_end": "2026-10-07T14:55:45.999755Z",
      "window_start": "2026-10-07T13:55:45.999755Z"
    }
  ],
  "next_cursor": "eyJrIjoicmVjb25f…"
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
      "created_at": "2026-10-07T14:55:47.111439Z",
      "expected": "894.75",
      "id": "01a116dd-09a7-7551-853f-87e387438ed6",
      "kind": "settlement_balance",
      "note": null,
      "provider": "simbank",
      "provider_ref": "USD",
      "resolved_at": null,
      "resolved_by": null,
      "run_id": "01a116dd-09a7-7551-853f-87e2d94795dc",
      "status": "open"
    },
    {
      "actual": "75.00",
      "asset": "USD",
      "created_at": "2026-10-07T14:55:27.129980Z",
      "expected": null,
      "id": "01a116dc-bb99-74ba-9b37-096c5e469508",
      "kind": "missing_deposit",
      "note": "The deposit is on the books.",
      "provider": "simbank",
      "provider_ref": "dep_4d6aa163d94e",
      "resolved_at": "2026-10-07T14:55:27.181464Z",
      "resolved_by": "system",
      "run_id": "01a116dc-bb99-74ba-9b37-096bcdd6176f",
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
POST /v1/admin/recon/breaks/01a116dd-09a7-7551-853f-87e387438ed6/resolve
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
  "created_at": "2026-10-07T14:55:47.111439Z",
  "expected": "894.75",
  "id": "01a116dd-09a7-7551-853f-87e387438ed6",
  "kind": "settlement_balance",
  "note": "The 15.00 USD was sent back by the bank on request; statement to follow.",
  "provider": "simbank",
  "provider_ref": "USD",
  "resolved_at": "2026-10-07T14:55:47.269306Z",
  "resolved_by": "01a116dc-a2fc-75c4-a9c3-dcd296b6c35e",
  "run_id": "01a116dd-09a7-7551-853f-87e2d94795dc",
  "status": "resolved"
}
```

```http
HTTP 409

{
  "code": "recon_break_not_open",
  "detail": "This break has already been resolved.",
  "request_id": "01a116dd-0a4a-76e0-a70e-888f55ae054f",
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
      "created_at": "2026-10-07T14:55:47.284887Z",
      "finished_at": "2026-10-07T14:55:47.294428Z",
      "id": "01a116dd-0a50-749a-a484-86b7cb1b8ba6",
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
POST /v1/admin/outbox/dead/01a116dd-0a50-749a-a484-86b7cb1b8ba6/requeue
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "attempts": 0,
  "created_at": "2026-10-07T14:55:47.284887Z",
  "finished_at": null,
  "id": "01a116dd-0a50-749a-a484-86b7cb1b8ba6",
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
  "request_id": "01a116dd-0a7d-76b2-9584-6940f7e02c53",
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
- No leg may debit a suspense account. Money leaves suspense only as the deposit it
  arrived as, through one of the two endpoints below.

```http
POST /v1/admin/adjustments
Authorization: Bearer <access token>
Idempotency-Key: docs-0731317f-26bb-4498-9432-50341adda1dd

{
  "legs": [
    {
      "account_id": "01a116dc-bbab-75cb-8215-7106971a3afe",
      "amount": "1.00",
      "asset": "USD",
      "direction": "debit"
    },
    {
      "account_id": "01a116dc-a1d4-70b0-aa4a-7485bae36ca6",
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
  "created_at": "2026-10-07T14:55:46.369649Z",
  "decided_at": null,
  "deposit_id": null,
  "entry_id": null,
  "id": "01a116dd-06be-74b0-9674-636a4140d3c4",
  "kind": "manual",
  "legs": [
    {
      "account_id": "01a116dc-bbab-75cb-8215-7106971a3afe",
      "amount": "1.00",
      "asset": "USD",
      "direction": "debit"
    },
    {
      "account_id": "01a116dc-a1d4-70b0-aa4a-7485bae36ca6",
      "amount": "1.00",
      "asset": "USD",
      "direction": "credit"
    }
  ],
  "reason": "Goodwill credit, to be rejected in this example",
  "requested_by": "01a116dc-a2fc-75c4-a9c3-dcd296b6c35e",
  "status": "pending",
  "user_id": null
}
```

A debit of suspense written by hand:

```http
HTTP 422

{
  "code": "invalid_adjustment",
  "detail": "Money leaves suspense by releasing or returning the deposit it arrived as.",
  "field": "legs",
  "request_id": "01a116dd-06aa-720c-8131-9d99b8001206",
  "status": 422,
  "title": "Invalid adjustment",
  "type": "https://corridor.example/problems/invalid-adjustment"
}
```

### `GET /v1/admin/deposits/suspense`

The deposits that are in suspense now, newest first: money that is on the books and is
nobody's. `received_at` is when Corridor recorded the deposit. `review_id` is the review
screening opened on it, whether that review is open or was rejected, and `null` for a
deposit that is in suspense because it arrived at an account or address Corridor never
issued. The `id` is what the two endpoints below take as `deposit_id`. Paged like every
list; each request is audited once.

```http
GET /v1/admin/deposits/suspense
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "items": [
    {
      "amount": "15.00",
      "asset": "USD",
      "id": "01a116dd-0596-7104-9866-cb0353e2282a",
      "provider": "simbank",
      "received_at": "2026-10-07T14:55:46.070214Z",
      "review_id": null
    },
    {
      "amount": "60.00",
      "asset": "USD",
      "id": "01a116dd-057d-7468-9f15-19687e193b8f",
      "provider": "simbank",
      "received_at": "2026-10-07T14:55:46.045549Z",
      "review_id": null
    }
  ],
  "next_cursor": null
}
```

### `POST /v1/admin/adjustments/suspense-release`

Asks for a deposit in suspense to be credited to a user. Needs an `Idempotency-Key`. The
body is `reason`, `deposit_id` and `user_id`, and nothing else: the asset, the amount and
the legs are the deposit's own, worked out by Corridor as a debit of the suspense account
and a credit of the user's available balance. The response says so in `kind`,
`deposit_id` and `user_id`.

The deposit must be in suspense when the adjustment is asked for, and again when it is
approved: its row is locked then and its status looked at. The entry is posted and the
deposit becomes `completed` and the user's, in one transaction. A deposit that was
released, returned or taken back by its bank in the meantime is refused at approval, so
the same money cannot be paid out twice; the adjustment stays `pending`, to be rejected.

```http
POST /v1/admin/adjustments/suspense-release
Authorization: Bearer <access token>
Idempotency-Key: docs-e5b2cc6d-8dfb-4df0-a050-b12de1d27427

{
  "deposit_id": "01a116dd-057d-7468-9f15-19687e193b8f",
  "reason": "Sender confirmed the payee by phone",
  "user_id": "01a116dc-a1c5-7063-b579-8e23c5bf3d2e"
}
```

```http
HTTP 201

{
  "approved_by": null,
  "created_at": "2026-10-07T14:55:46.236332Z",
  "decided_at": null,
  "deposit_id": "01a116dd-057d-7468-9f15-19687e193b8f",
  "entry_id": null,
  "id": "01a116dd-0636-70e5-be97-8b9691dfa284",
  "kind": "suspense_release",
  "legs": [
    {
      "account_id": "01a116dc-f7bb-7171-b26c-68371cba3213",
      "amount": "60.00",
      "asset": "USD",
      "direction": "debit"
    },
    {
      "account_id": "01a116dc-a1d4-70b0-aa4a-7485bae36ca6",
      "amount": "60.00",
      "asset": "USD",
      "direction": "credit"
    }
  ],
  "reason": "Sender confirmed the payee by phone",
  "requested_by": "01a116dc-a2fc-75c4-a9c3-dcd296b6c35e",
  "status": "pending",
  "user_id": "01a116dc-a1c5-7063-b579-8e23c5bf3d2e"
}
```

| Refusal | When |
|---|---|
| `404 deposit_not_found` | No such deposit |
| `409 deposit_not_in_suspense` | The deposit is not in suspense, or is no longer. Asked for or approved |
| `409 deposit_owner_closed` | The user's account is closed. Asked for or approved |
| `404 user_not_found` | No such user |
| `422 invalid_adjustment` | The reason is empty. One longer than 500 characters is `422 invalid_request` |

The deposit above, asked for a second time after it was released:

```http
HTTP 409

{
  "code": "deposit_not_in_suspense",
  "detail": "This deposit is no longer in suspense.",
  "request_id": "01a116dd-069f-758b-ba6b-7abe2bf305c9",
  "status": 409,
  "title": "Deposit is not in suspense",
  "type": "https://corridor.example/problems/deposit-not-in-suspense"
}
```

### `POST /v1/admin/adjustments/suspense-return`

Asks for a deposit in suspense to be taken off the books as sent back through the provider
it arrived at. Needs an `Idempotency-Key`. The body is `reason` and `deposit_id`. Approval
posts a debit of suspense and a credit of the provider's settlement account, and marks
the deposit `returned`, under the same lock and the same status rule as a release, and
with the same refusals except those about a user. Sending the money back is the
operator's to do with the provider; until the provider's statement shows it,
reconciliation reports a `settlement_balance` break.

```http
POST /v1/admin/adjustments/suspense-return
Authorization: Bearer <access token>
Idempotency-Key: docs-9bbb2e32-7900-424a-b2c8-ce8dd7df2d57

{
  "deposit_id": "01a116dd-0596-7104-9866-cb0353e2282a",
  "reason": "Sender unknown; returned through the bank"
}
```

```http
HTTP 201

{
  "approved_by": null,
  "created_at": "2026-10-07T14:55:46.301941Z",
  "decided_at": null,
  "deposit_id": "01a116dd-0596-7104-9866-cb0353e2282a",
  "entry_id": null,
  "id": "01a116dd-0678-750c-ab11-8af32da52a88",
  "kind": "suspense_return",
  "legs": [
    {
      "account_id": "01a116dc-f7bb-7171-b26c-68371cba3213",
      "amount": "15.00",
      "asset": "USD",
      "direction": "debit"
    },
    {
      "account_id": "01a116dc-bbab-75cb-8215-7106971a3afe",
      "amount": "15.00",
      "asset": "USD",
      "direction": "credit"
    }
  ],
  "reason": "Sender unknown; returned through the bank",
  "requested_by": "01a116dc-a2fc-75c4-a9c3-dcd296b6c35e",
  "status": "pending",
  "user_id": null
}
```

### `POST /v1/admin/adjustments/{adjustment_id}/approve`

Approves a pending adjustment and posts its entry. Needs an `Idempotency-Key`. The approver
must not be the requester:

```http
POST /v1/admin/adjustments/01a116dd-0636-70e5-be97-8b9691dfa284/approve
Authorization: Bearer <access token>
Idempotency-Key: docs-8a13f07f-8e52-4444-ae74-4e05ac12f53f
```

```http
HTTP 403

{
  "code": "self_approval",
  "detail": "An adjustment is approved by a different administrator.",
  "request_id": "01a116dd-0644-73bc-8339-57eb187abfc0",
  "status": 403,
  "title": "Self-approval is not allowed",
  "type": "https://corridor.example/problems/self-approval"
}
```

```http
POST /v1/admin/adjustments/01a116dd-0636-70e5-be97-8b9691dfa284/approve
Authorization: Bearer <access token>
Idempotency-Key: docs-953649f9-274d-4e41-9030-a19f4b787157
```

```http
HTTP 200

{
  "approved_by": "01a116dc-a435-73f1-8312-2520f5c8a4d8",
  "created_at": "2026-10-07T14:55:46.236332Z",
  "decided_at": "2026-10-07T14:55:46.281520Z",
  "deposit_id": "01a116dd-057d-7468-9f15-19687e193b8f",
  "entry_id": "01a116dd-0663-703d-92d7-a5aa7dbbfb81",
  "id": "01a116dd-0636-70e5-be97-8b9691dfa284",
  "kind": "suspense_release",
  "legs": [
    {
      "account_id": "01a116dc-f7bb-7171-b26c-68371cba3213",
      "amount": "60.00",
      "asset": "USD",
      "direction": "debit"
    },
    {
      "account_id": "01a116dc-a1d4-70b0-aa4a-7485bae36ca6",
      "amount": "60.00",
      "asset": "USD",
      "direction": "credit"
    }
  ],
  "reason": "Sender confirmed the payee by phone",
  "requested_by": "01a116dc-a2fc-75c4-a9c3-dcd296b6c35e",
  "status": "approved",
  "user_id": "01a116dc-a1c5-7063-b579-8e23c5bf3d2e"
}
```

Refusals: `403 self_approval`, `409 adjustment_not_pending`, `402 insufficient_funds` when
a debited user balance no longer holds the amount, `409 deposit_not_in_suspense` for a
suspense adjustment whose deposit has left suspense, `409 deposit_owner_closed` for a
release to an account that has been closed since, and `422 invalid_adjustment` for an
adjustment written by hand that debits suspense. A refused adjustment stays `pending`.

### `POST /v1/admin/adjustments/{adjustment_id}/reject`

Turns a pending adjustment down. Needs an `Idempotency-Key`. Any administrator can,
including the requester.

```http
POST /v1/admin/adjustments/01a116dd-06be-74b0-9674-636a4140d3c4/reject
Authorization: Bearer <access token>
Idempotency-Key: docs-9d6adb8f-fa0f-4856-b99e-9a4a66d98665
```

```http
HTTP 200

{
  "approved_by": null,
  "created_at": "2026-10-07T14:55:46.369649Z",
  "decided_at": "2026-10-07T14:55:46.383639Z",
  "deposit_id": null,
  "entry_id": null,
  "id": "01a116dd-06be-74b0-9674-636a4140d3c4",
  "kind": "manual",
  "legs": [
    {
      "account_id": "01a116dc-bbab-75cb-8215-7106971a3afe",
      "amount": "1.00",
      "asset": "USD",
      "direction": "debit"
    },
    {
      "account_id": "01a116dc-a1d4-70b0-aa4a-7485bae36ca6",
      "amount": "1.00",
      "asset": "USD",
      "direction": "credit"
    }
  ],
  "reason": "Goodwill credit, to be rejected in this example",
  "requested_by": "01a116dc-a2fc-75c4-a9c3-dcd296b6c35e",
  "status": "rejected",
  "user_id": null
}
```

### `GET /v1/admin/adjustments/{adjustment_id}`

```http
GET /v1/admin/adjustments/01a116dd-0636-70e5-be97-8b9691dfa284
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "approved_by": "01a116dc-a435-73f1-8312-2520f5c8a4d8",
  "created_at": "2026-10-07T14:55:46.236332Z",
  "decided_at": "2026-10-07T14:55:46.281520Z",
  "deposit_id": "01a116dd-057d-7468-9f15-19687e193b8f",
  "entry_id": "01a116dd-0663-703d-92d7-a5aa7dbbfb81",
  "id": "01a116dd-0636-70e5-be97-8b9691dfa284",
  "kind": "suspense_release",
  "legs": [
    {
      "account_id": "01a116dc-f7bb-7171-b26c-68371cba3213",
      "amount": "60.00",
      "asset": "USD",
      "direction": "debit"
    },
    {
      "account_id": "01a116dc-a1d4-70b0-aa4a-7485bae36ca6",
      "amount": "60.00",
      "asset": "USD",
      "direction": "credit"
    }
  ],
  "reason": "Sender confirmed the payee by phone",
  "requested_by": "01a116dc-a2fc-75c4-a9c3-dcd296b6c35e",
  "status": "approved",
  "user_id": "01a116dc-a1c5-7063-b579-8e23c5bf3d2e"
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
      "approved_by": "01a116dc-a435-73f1-8312-2520f5c8a4d8",
      "created_at": "2026-10-07T14:55:46.301941Z",
      "decided_at": "2026-10-07T14:55:46.328945Z",
      "deposit_id": "01a116dd-0596-7104-9866-cb0353e2282a",
      "entry_id": "01a116dd-0693-7157-89e1-9d3d0b286780",
      "id": "01a116dd-0678-750c-ab11-8af32da52a88",
      "kind": "suspense_return",
      "legs": [
        {
          "account_id": "01a116dc-f7bb-7171-b26c-68371cba3213",
          "amount": "15.00",
          "asset": "USD",
          "direction": "debit"
        },
        {
          "account_id": "01a116dc-bbab-75cb-8215-7106971a3afe",
          "amount": "15.00",
          "asset": "USD",
          "direction": "credit"
        }
      ],
      "reason": "Sender unknown; returned through the bank",
      "requested_by": "01a116dc-a2fc-75c4-a9c3-dcd296b6c35e",
      "status": "approved",
      "user_id": null
    }
  ],
  "next_cursor": "eyJrIjoiYWRqdXN0…"
}
```

### `GET /v1/admin/audit`

The audit log, newest first. Every filter is optional, and an event must match all that
are given.

| Query parameter | Matches |
|---|---|
| `actor` | The id of whoever acted: a user, an administrator or an agent by id, a provider or a job by name |
| `action` | Actions that begin with this: `user.` for everything done to a user's standing, `adjustment.approved` for one action. Lower-case letters, digits, `_` and `.` |
| `subject` | The id of what was acted on (`resource_id`), or of the user it was done for (`principal_id`) |
| `since` | Events from this moment on. A timestamp with its offset, such as `2026-10-07T00:00:00Z` |
| `until` | Events before this moment |
| `cursor`, `limit` | As for every list |

A cursor is a place in the whole log, so it can be sent back with other filters than it
was issued with. A filter that cannot be applied (a timestamp with no offset, an `action`
with other characters) is refused with `422 invalid_request`.

Reading the log is itself audited: one `audit.listed` event for each request, with the
filters and the number of events returned, and never one for each event. The event is
written after the page is read, so a request does not see itself.

What was done to users' standing, three at a time:

```http
GET /v1/admin/audit?action=user.&limit=3
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "items": [
    {
      "action": "user.closed",
      "actor_id": "01a116dc-a2fc-75c4-a9c3-dcd296b6c35e",
      "actor_type": "admin",
      "details": {
        "old_status": "active"
      },
      "id": "01a116dd-0c31-703c-9b7a-9bb7c6f6e644",
      "occurred_at": "2026-10-07T14:55:47.761424Z",
      "outcome": "success",
      "principal_id": "01a116dd-0b19-74cb-b642-6d6ea4eb4875",
      "request_id": "01a116dd-0c27-751c-a104-2a3f5c70e1cc",
      "resource_id": "01a116dd-0b19-74cb-b642-6d6ea4eb4875",
      "resource_type": "user"
    },
    {
      "action": "user.restriction_lifted",
      "actor_id": "01a116dc-a2fc-75c4-a9c3-dcd296b6c35e",
      "actor_type": "admin",
      "details": {
        "reason": "Investigation closed, nothing owed"
      },
      "id": "01a116dd-0c19-7526-b025-caa992b12e06",
      "occurred_at": "2026-10-07T14:55:47.737532Z",
      "outcome": "success",
      "principal_id": "01a116dc-a089-7087-b906-78afceb61920",
      "request_id": "01a116dd-0c13-7793-9eff-7797898e113b",
      "resource_id": "01a116dc-a089-7087-b906-78afceb61920",
      "resource_type": "user"
    },
    {
      "action": "user.restricted",
      "actor_id": "01a116dc-a2fc-75c4-a9c3-dcd296b6c35e",
      "actor_type": "admin",
      "details": {
        "reason": "Chargeback under investigation"
      },
      "id": "01a116dd-0c03-71b2-8ed4-7619028ce688",
      "occurred_at": "2026-10-07T14:55:47.715610Z",
      "outcome": "success",
      "principal_id": "01a116dc-a089-7087-b906-78afceb61920",
      "request_id": "01a116dd-0bf7-7094-a3b5-ef83f2cbb07e",
      "resource_id": "01a116dc-a089-7087-b906-78afceb61920",
      "resource_type": "user"
    }
  ],
  "next_cursor": "eyJrIjoiYXVkaXRf…"
}
```

Everything about one deposit, which arrived in suspense and was released:

```http
GET /v1/admin/audit?subject=01a116dd-057d-7468-9f15-19687e193b8f&since=2026-01-01T00%3A00%3A00Z
Authorization: Bearer <access token>
```

```http
HTTP 200

{
  "items": [
    {
      "action": "deposit.released",
      "actor_id": "01a116dc-a435-73f1-8312-2520f5c8a4d8",
      "actor_type": "admin",
      "details": {
        "amount": "6000",
        "asset": "USD",
        "entry_id": "01a116dd-0663-703d-92d7-a5aa7dbbfb81"
      },
      "id": "01a116dd-0667-726b-be28-027f8642b067",
      "occurred_at": "2026-10-07T14:55:46.279420Z",
      "outcome": "success",
      "principal_id": "01a116dc-a1c5-7063-b579-8e23c5bf3d2e",
      "request_id": "01a116dd-0653-7318-a214-7b98ec121c15",
      "resource_id": "01a116dd-057d-7468-9f15-19687e193b8f",
      "resource_type": "deposit"
    },
    {
      "action": "deposit.suspended",
      "actor_id": "simbank",
      "actor_type": "provider",
      "details": {
        "amount": "6000",
        "asset": "USD",
        "entry_id": "01a116dd-0584-73dc-8bc9-54edbefdfab9"
      },
      "id": "01a116dd-0588-7308-ba5e-7a238f191f40",
      "occurred_at": "2026-10-07T14:55:46.056139Z",
      "outcome": "success",
      "principal_id": null,
      "request_id": "01a116dd-055f-7685-829a-0eba4a1a7732",
      "resource_id": "01a116dd-057d-7468-9f15-19687e193b8f",
      "resource_type": "deposit"
    }
  ],
  "next_cursor": null
}
```

