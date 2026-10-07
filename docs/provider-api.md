# Provider API contract

Corridor talks to three kinds of provider: a **bank rail** (virtual accounts, payouts), a
**custodian** (deposit addresses, on-chain withdrawals) and an **FX rate source**. In this
repository all three are simulated by one small application, `corridor_sim`.

This document is the contract between the two sides. The simulator implements it; the
adapters in `corridor.providers` consume it. Neither imports the other, so this page is what
keeps them in step. A real provider would be integrated by writing a new adapter against the
same ports, not by changing this contract.

| Provider | Name in Corridor | Base path | Webhooks go to |
|---|---|---|---|
| Bank rail | `simbank` | `/bank/v1` | `POST /v1/webhooks/simbank` |
| Custodian | `simcustody` | `/custody/v1` | `POST /v1/webhooks/simcustody` |
| FX rates | `simfx` | `/fx/v1` | none |

## Conventions

- **Authentication.** `Authorization: Bearer <api key>`. A missing or wrong key is
  `401 unauthorized`.
- **Amounts.** Decimal strings in major units with an `asset` code, for example
  `{"asset": "USD", "amount": "12.50"}`. Never JSON numbers: a number where a string
  belongs is `422 invalid_amount`.
- **Assets.** `USD`, `MXN`, `BRL` (2 decimal places) and `USDC` (6).
- **Identifiers.** Opaque strings: a type prefix followed by 12 lower-case hex characters.
  The prefixes are `va_`, `ben_`, `po_`, `dep_`, `addr_`, `wd_` and `evt_`.
- **Time.** ISO-8601 in UTC with a `Z`, for example `2026-01-15T12:00:00Z`. A time that is
  not on a whole second carries six fractional digits.
- **Errors.** `{"error": {"code": "beneficiary_not_found", "message": "..."}}`. A `4xx` is a
  definite refusal: nothing happened. A `5xx`, a timeout or a dropped connection says nothing
  about whether the operation happened. A body that is not a JSON object, or that lacks a
  required field, is `422 invalid_request`.
- **Idempotency.** `POST /payouts` and `POST /withdrawals` require an `Idempotency-Key`
  header and answer `400 idempotency_key_required` without one; the other `POST`s accept
  one. Repeating a key with the same body returns the original resource with `200` (the
  first response was `201`). Repeating a key with a different body is
  `409 idempotency_conflict`. A key is bound when the operation takes effect, so a request
  whose response was lost has still bound its key, and a request that was refused has bound
  nothing.
- **References.** Corridor passes its own user id as `customer_reference` and its own
  withdrawal id as `reference` and as the idempotency key.

How Corridor's adapters read an answer:

| Answer | Corridor treats it as |
|---|---|
| `200` or `201` with a body that matches this contract | Success |
| `401` | A fault in Corridor's own configuration. Nothing happened, and no retry helps |
| Any other `4xx` with the error body above | A refusal. Nothing happened |
| `5xx`, a timeout, a broken connection, or a body that does not match this contract | Unknown. The call is repeated later with the same idempotency key |

## Bank rail: `/bank/v1`

Each asset moves on one rail, and each rail settles on its own schedule.

| Asset | Rail | Account identifier | Payout settles after | Provider fee |
|---|---|---|---|---|
| `USD` | `ach` | account number (4 to 17 digits) and 9-digit routing number | 30 seconds | `0.25` |
| `MXN` | `spei` | 18-digit CLABE | 5 seconds | `5.00` |
| `BRL` | `pix` | PIX key (1 to 77 characters, no whitespace) | 1 second | `0.10` |

The delays are simulator time and are configurable. The fee is charged to Corridor, in the
payout's asset, when a payout completes. Any other asset is `422 unsupported_asset`.

### `POST /virtual-accounts`

Returns the account a customer deposits into. There is one per customer and asset: asking
again returns the same account with `200`.

```json
{"customer_reference": "0199b7c2-6f0e-7b1a-9d53-2c1f4e8a7b10", "asset": "USD"}
```

`201`:

```json
{
  "id": "va_d1cb70757d9e",
  "customer_reference": "0199b7c2-6f0e-7b1a-9d53-2c1f4e8a7b10",
  "asset": "USD",
  "rail": "ach",
  "bank_name": "Sim Bank",
  "account_number": "900082184665",
  "routing_number": "021000021"
}
```

`routing_number` is present for `USD` only.

### `POST /beneficiaries`

Registers an external bank account to pay out to. The full account number goes in and never
comes back: the response carries a token and a masked value.

```json
{
  "customer_reference": "0199b7c2-6f0e-7b1a-9d53-2c1f4e8a7b10",
  "asset": "USD",
  "holder_name": "Maria Silva",
  "account_number": "000123456789",
  "routing_number": "021000021"
}
```

`201`:

```json
{"id": "ben_4f1e55f6c8a9", "asset": "USD", "rail": "ach", "holder_name": "Maria Silva", "account_mask": "••••6789"}
```

The mask is four bullets and the last four characters of the identifier, or fewer for an
identifier shorter than eight characters, so that never more than half of it is shown.

Refusals: `422 invalid_account` (wrong shape for the rail), `422 unsupported_asset`.

### `POST /payouts`

Requires `Idempotency-Key`.

```json
{"beneficiary_id": "ben_4f1e55f6c8a9", "asset": "USD", "amount": "100.00", "reference": "0199b7c3-1a2b-7c3d-8e4f-5a6b7c8d9e0f"}
```

`201`:

```json
{
  "id": "po_9ecb87d927e1",
  "status": "pending",
  "beneficiary_id": "ben_4f1e55f6c8a9",
  "asset": "USD",
  "amount": "100.00",
  "fee": "0.25",
  "reference": "0199b7c3-1a2b-7c3d-8e4f-5a6b7c8d9e0f",
  "created_at": "2026-01-15T12:00:00Z",
  "settled_at": null,
  "failure_reason": null
}
```

Refusals: `404 beneficiary_not_found`, `422 asset_mismatch` (the beneficiary is in another
asset), `422 invalid_amount`.

A payout is `pending` until the rail's delay has passed, then `completed`. A payout to a
beneficiary whose account number ends in `0000` becomes `failed` at that moment instead, with
`failure_reason` `account_closed`. The provider does not refuse a payout for lack of funds:
Corridor's balance with it may go negative, like an overdraft.

### `GET /payouts/{id}` and `GET /payouts?reference=`

The payout as above; the list form returns `{"payouts": [...]}`, which is empty when no
payout carries the reference. `GET /payouts/{id}` for an unknown id is `404 payout_not_found`.
Corridor polls these when a webhook is overdue, and before it releases the funds of a
payout request that was refused.

### `GET /transactions?asset=&from=&to=`

The provider's statement for one asset over a half-open window `[from, to)`: every settled
movement and the balance at the end of the window. `from` and `to` are times with a UTC
offset, and `from` must not be later than `to`.

```json
{
  "asset": "USD",
  "from": "2026-01-15T00:00:00Z",
  "to": "2026-01-16T00:00:00Z",
  "transactions": [
    {"id": "dep_a7c3669559b4", "type": "deposit", "direction": "credit", "asset": "USD", "amount": "250.00",
     "reference": "INV-2041", "related_id": "va_d1cb70757d9e", "occurred_at": "2026-01-15T09:30:00Z"},
    {"id": "po_9ecb87d927e1", "type": "payout", "direction": "debit", "asset": "USD", "amount": "100.00",
     "reference": "0199b7c3-1a2b-7c3d-8e4f-5a6b7c8d9e0f", "related_id": "ben_4f1e55f6c8a9", "occurred_at": "2026-01-15T12:00:30Z"},
    {"id": "po_9ecb87d927e1:fee", "type": "payout_fee", "direction": "debit", "asset": "USD", "amount": "0.25",
     "reference": "0199b7c3-1a2b-7c3d-8e4f-5a6b7c8d9e0f", "related_id": "po_9ecb87d927e1", "occurred_at": "2026-01-15T12:00:30Z"}
  ],
  "closing_balance": "149.75"
}
```

| Type | Direction | `id` | `related_id` |
|---|---|---|---|
| `deposit` | credit | the deposit's id | the virtual account it arrived at |
| `deposit_return` | debit | the deposit's id followed by `:return` | the deposit's id |
| `payout` | debit | the payout's id | the beneficiary |
| `payout_fee` | debit | the payout's id followed by `:fee` | the payout's id |

A pending or failed payout is not a transaction. `closing_balance` may be negative. The
statement does not name the sender of a deposit.

### Bank webhooks

| Type | `data` |
|---|---|
| `deposit.received` | `deposit_id`, `virtual_account_id`, `customer_reference`, `asset`, `amount`, `sender_name`, `reference` |
| `deposit.returned` | `deposit_id`, `asset`, `amount`, `reason` |
| `payout.completed` | `payout_id`, `reference`, `asset`, `amount`, `fee`, `settled_at` |
| `payout.failed` | `payout_id`, `reference`, `asset`, `amount`, `failure_reason` |

`customer_reference` is `null` for a deposit to a virtual account the bank did not issue.
Corridor does not read `customer_reference` at all: it attributes a deposit only by the
virtual account, looked up among the accounts Corridor itself obtained.

## Custodian: `/custody/v1`

One network, `simchain`, carrying `USDC`. A block is produced every 2 seconds of simulator
time. A deposit is final after 3 confirmations. Both numbers are configurable.

**Addresses** are 44 characters: the prefix `sim1`, a 32-character body in lower-case base32
(`a`–`z`, `2`–`7`), then 8 lower-case hex characters that are the first four bytes of the
SHA-256 of the body's ASCII bytes. Either side can check an address without asking the
other. The address in the example below is valid.

### `POST /addresses`

```json
{"customer_reference": "0199b7c2-6f0e-7b1a-9d53-2c1f4e8a7b10", "asset": "USDC"}
```

`201` (or `200` with the same address if one exists for this customer and asset):

```json
{"id": "addr_e26c387c0895", "customer_reference": "0199b7c2-6f0e-7b1a-9d53-2c1f4e8a7b10", "asset": "USDC",
 "network": "simchain", "address": "sim1corridorexampledepositaddress234f24b41da"}
```

Refusal: `422 unsupported_asset`.

### `POST /withdrawals`

Requires `Idempotency-Key`.

```json
{"asset": "USDC", "amount": "25.000000", "to_address": "sim1...", "reference": "0199b7c3-1a2b-7c3d-8e4f-5a6b7c8d9e0f"}
```

`201`:

```json
{
  "id": "wd_a87d49f5d20f",
  "status": "pending",
  "asset": "USDC",
  "amount": "25.000000",
  "network_fee": "0.150000",
  "to_address": "sim1...",
  "reference": "0199b7c3-1a2b-7c3d-8e4f-5a6b7c8d9e0f",
  "tx_hash": null,
  "confirmations": 0,
  "created_at": "2026-01-15T12:00:00Z",
  "completed_at": null,
  "failure_reason": null
}
```

Refusals: `422 invalid_address`, `422 invalid_amount`, `422 unsupported_asset`.

A withdrawal is `pending` until the next block, when it is `broadcast` and gets a `tx_hash`.
After 3 confirmations it is `completed`. A withdrawal to an address whose body begins with
`dead` becomes `failed` at broadcast, with `failure_reason` `rejected_by_network`. The network
fee is fixed, is paid from Corridor's balance at the custodian, and is charged only when a
withdrawal completes.

### `GET /withdrawals/{id}` and `GET /withdrawals?reference=`

The withdrawal as above; the list form returns `{"withdrawals": [...]}`, which is empty when
no withdrawal carries the reference. `GET /withdrawals/{id}` for an unknown id is
`404 withdrawal_not_found`.

### `GET /transactions?asset=&from=&to=`

Same shape as the bank statement, and each line also carries `tx_hash`.

| Type | Direction | `id` | `related_id` |
|---|---|---|---|
| `deposit` (once confirmed) | credit | the deposit's id | the id of the address it arrived at |
| `withdrawal` (once completed) | debit | the withdrawal's id | the address it was sent to |
| `network_fee` | debit | the withdrawal's id followed by `:fee` | the withdrawal's id |

`reference` is `null` on a deposit line. The statement does not name the address a deposit
came from.

### Custody webhooks

| Type | `data` |
|---|---|
| `deposit.detected` | `deposit_id`, `address_id`, `address`, `customer_reference`, `asset`, `amount`, `tx_hash`, `from_address`, `confirmations` |
| `deposit.confirmed` | the same fields, with `confirmations` at 3 or more |
| `deposit.failed` | `deposit_id`, `asset`, `amount`, `tx_hash`, `reason` |
| `withdrawal.completed` | `withdrawal_id`, `reference`, `asset`, `amount`, `network_fee`, `tx_hash` |
| `withdrawal.failed` | `withdrawal_id`, `reference`, `asset`, `amount`, `failure_reason` |

`deposit.detected` is sent when a transaction is first seen, with no confirmations. It is
information only: the funds are not final. A deposit can be dropped before it is final, in
which case `deposit.failed` follows instead of `deposit.confirmed`, with the reason
`dropped`.

## FX rates: `/fx/v1`

### `GET /rates/{base}/{quote}`

The mid-market rate: how many units of `quote` one unit of `base` buys.

```json
{"base": "USD", "quote": "MXN", "mid": "17.253400", "as_of": "2026-01-15T12:00:00Z"}
```

`mid` has six decimal places. Every ordered pair of two different assets is served, and the
rates are consistent: `rate(A, B) × rate(B, A)` is 1 to within rounding. `404 unknown_pair`
otherwise. `as_of` is when the rate was last updated; a consumer decides how old is too old.
Corridor refuses to quote from a rate older than 15 seconds.

The simulator moves each rate by a small step once per second of simulator time. The walk is
driven by a seed, so the same seed and the same number of steps give the same rates.

## Webhook delivery

Both providers deliver webhooks the same way.

```json
{
  "id": "evt_dd29d671dd1e",
  "type": "payout.completed",
  "created_at": "2026-01-15T12:00:30Z",
  "data": {"payout_id": "po_9ecb87d927e1", "reference": "0199b7c3-...", "asset": "USD", "amount": "100.00", "fee": "0.25",
           "settled_at": "2026-01-15T12:00:30Z"}
}
```

- **Signature.** The header `X-Signature: t=<unix seconds>,v1=<hex>` where `v1` is
  HMAC-SHA256, keyed with the shared secret, over the bytes `<t>.<raw request body>`. The
  receiver must verify against the body exactly as received, compare in constant time, and
  refuse a timestamp more than five minutes from its own clock.
- **One secret per provider.** The provider's name is not among the signed bytes. The two
  providers therefore need different secrets, or an event signed by one would verify on the
  other's path. Corridor refuses to start if they share one.
- **At least once.** A delivery that does not get a `2xx` within the timeout (5 seconds by
  default) is retried with the same event `id`: after 1, 2, 4, 8 and 16 seconds, six
  attempts in all. A receiver must treat a repeated `id` as already handled.
- **No ordering.** Events can arrive in any order, and `payout.completed` can arrive before
  the response to the `POST /payouts` that created the payout.
- **Not guaranteed.** After the last attempt the event is abandoned. The polling endpoints
  and the statement exist so that a receiver does not depend on webhooks alone.

## Running the simulator

`python -m corridor_sim` serves all three providers and the control endpoints on one port.
It is configured by environment variables prefixed `CORRIDOR_SIM_`.

| Variable | Default | Meaning |
|---|---|---|
| `CORRIDOR_SIM_ENVIRONMENT` | `development` | `development` or `test`. Any other value stops the simulator from starting |
| `CORRIDOR_SIM_API_KEY` | required | The key Corridor's adapters present. At least 32 characters |
| `CORRIDOR_SIM_BANK_WEBHOOK_URL`, `CORRIDOR_SIM_CUSTODY_WEBHOOK_URL` | not set | Where each provider's webhooks go. A provider with no URL records its events and delivers none |
| `CORRIDOR_SIM_BANK_WEBHOOK_SECRET`, `CORRIDOR_SIM_CUSTODY_WEBHOOK_SECRET` | required | The secret that signs each provider's webhooks. At least 32 characters |
| `CORRIDOR_SIM_WEBHOOK_TIMEOUT_SECONDS` | `5` | How long one delivery may take |
| `CORRIDOR_SIM_CLOCK_MODE` | `realtime` | `realtime` or `manual` |
| `CORRIDOR_SIM_START_TIME` | `2026-01-15T12:00:00Z` | Where a manual clock starts |
| `CORRIDOR_SIM_SEED` | `20260115` | Seeds the identifiers and the rate walk |
| `CORRIDOR_SIM_ACH_SETTLE_SECONDS`, `CORRIDOR_SIM_SPEI_SETTLE_SECONDS`, `CORRIDOR_SIM_PIX_SETTLE_SECONDS` | `30`, `5`, `1` | Payout delay per rail |
| `CORRIDOR_SIM_BLOCK_SECONDS`, `CORRIDOR_SIM_CONFIRMATIONS` | `2`, `3` | The chain |
| `CORRIDOR_SIM_HOST`, `CORRIDOR_SIM_PORT` | `127.0.0.1`, `8100` | Where it listens |
| `CORRIDOR_SIM_CONTROL_TOKEN` | not set | The token `/_control` asks for. Required when the host is not a loopback address |

The simulator keeps everything in memory. Restarting it forgets every account, payout and
event.

## Simulator control: `/_control`

Not part of any provider's API. These endpoints exist so that tests and the demo can make
the outside world do something: receive a deposit, lose a webhook, time out.

**Access.** While the simulator listens only on a loopback address and has no control
token, these endpoints are open to whoever can reach them. When
`CORRIDOR_SIM_CONTROL_TOKEN` is set, every one of them needs
`Authorization: Bearer <control token>` and answers `401 unauthorized` without it. The
simulator refuses to start on a non-loopback address without a token, and refuses to start
at all outside a development or test environment. They must never be reachable from outside
such an environment.

| Endpoint | Effect |
|---|---|
| `POST /_control/reset` | Forget everything |
| `GET /_control/clock` | The simulator's time and mode |
| `POST /_control/clock/advance` `{"seconds": 30}` | Move a manual clock forward: settles payouts, produces blocks, moves rates, makes retries due. At most 31 days per call. `409 clock_is_realtime` in realtime mode |
| `GET /_control/bank/virtual-accounts?customer_reference=` | The virtual accounts the bank issued for a customer, so that a deposit can be made to one without the provider API key |
| `POST /_control/bank/deposits` `{"virtual_account_id", "amount", "sender_name", "reference"}` | A customer's bank deposit arrives. An unknown `virtual_account_id` is accepted, to exercise unattributable deposits; such a deposit is in `USD` unless the body also names an `asset` |
| `POST /_control/bank/deposits/{id}/return` `{"reason"}` | The sending bank recalls a deposit. `409 deposit_already_returned` the second time |
| `POST /_control/custody/deposits` `{"address", "amount", "from_address"}` | A transaction to a deposit address appears, unconfirmed. `404 address_not_found` for an address the custodian did not issue |
| `POST /_control/custody/deposits/{id}/drop` | The transaction is dropped before it is final. `409 deposit_already_final` once it is confirmed or failed |
| `POST /_control/chain/mine` `{"blocks": 3}` | Produce blocks now |
| `POST /_control/fx/rates` `{"base", "quote", "mid"}` | Pin a rate, and its reverse at the reciprocal. `as_of` keeps moving, so a pinned rate stays fresh |
| `POST /_control/fx/freeze` `{"frozen": true}` | Stop updating `as_of`, so rates go stale |
| `POST /_control/faults` `{"operation", "mode", "times", "status", "hang_seconds"}` | Make the next `times` calls to an operation fail |
| `DELETE /_control/faults` | Clear injected faults |
| `POST /_control/webhooks/behaviour` `{"duplicates", "drop_types", "hold", "reverse"}` | Change how webhooks are delivered. A field that is left out keeps its value |
| `POST /_control/webhooks/deliver` | Attempt every delivery that is due, now, and report the outcomes |
| `GET /_control/webhooks/events` | Every event, its status and its delivery attempts |
| `GET /_control/bank/payouts`, `/_control/bank/deposits`, `/_control/bank/balances` | Inspect the bank's books |
| `GET /_control/custody/withdrawals`, `/_control/custody/deposits`, `/_control/custody/balances` | Inspect the custodian's books |

**Fault operations:** `bank.create_virtual_account`, `bank.create_beneficiary`,
`bank.create_payout`, `bank.get_payout`, `bank.list_transactions`, `custody.create_address`,
`custody.create_withdrawal`, `custody.get_withdrawal`, `custody.list_transactions`,
`fx.get_rate`. The list forms (`GET /payouts?reference=`, `GET /withdrawals?reference=`)
share the fault of the read by id. Any other name is `422 unknown_operation`.

**Fault modes:**

| Mode | What the caller sees | Did the operation happen? |
|---|---|---|
| `error` | The given `status` (default `503`), with the code `injected_fault` | No |
| `error_after_effect` | The given `status` | **Yes** |
| `timeout` | No response for `hang_seconds` of real time (default 30), then `504 injected_timeout`. A caller with a shorter deadline sees a timeout | No |
| `timeout_after_effect` | The same | **Yes** |

The two `after_effect` modes are the important ones. They reproduce a provider that accepted
a payout and then failed to say so, which is the case idempotency keys exist for.

**Webhook behaviour:** `duplicates` sends each event that many extra times; `drop_types`
lists event types that are never delivered; `hold` queues events without delivering them
until it is switched off; `reverse` delivers the queued events newest first. An event's
status is `pending`, `delivered`, `abandoned` (out of attempts), `dropped` (its type was in
`drop_types`) or `undeliverable` (its provider has no webhook URL).

**Clock.** In `manual` mode, time moves only through `/_control/clock/advance`, which makes a
test deterministic. In `realtime` mode the simulator follows the wall clock and looks at it
four times a second, which is what the local stack and the demo use.
