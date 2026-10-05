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

- **Authentication.** `Authorization: Bearer <api key>`. A missing or wrong key is `401`.
- **Amounts.** Decimal strings in major units with an `asset` code, for example
  `{"asset": "USD", "amount": "12.50"}`. Never JSON numbers.
- **Assets.** `USD`, `MXN`, `BRL` (2 decimal places) and `USDC` (6).
- **Identifiers.** Opaque strings with a type prefix: `va_`, `ben_`, `po_`, `dep_`, `addr_`,
  `wd_`, `evt_`.
- **Time.** ISO-8601 in UTC, for example `2026-01-15T12:00:00Z`.
- **Errors.** `{"error": {"code": "beneficiary_not_found", "message": "..."}}`. A `4xx` is a
  definite refusal: nothing happened. A `5xx`, a timeout or a dropped connection says nothing
  about whether the operation happened.
- **Idempotency.** `POST /payouts` and `POST /withdrawals` require an `Idempotency-Key`
  header; the other `POST`s accept one. Repeating a key with the same body returns the
  original resource with `200` (the first response was `201`). Repeating a key with a
  different body is `409 idempotency_conflict`.
- **References.** Corridor passes its own user id as `customer_reference` and its own
  withdrawal id as `reference` and as the idempotency key.

## Bank rail: `/bank/v1`

Each asset moves on one rail, and each rail settles on its own schedule.

| Asset | Rail | Account identifier | Payout settles after | Provider fee |
|---|---|---|---|---|
| `USD` | `ach` | account number and routing number | 30 seconds | `0.25` |
| `MXN` | `spei` | 18-digit CLABE | 5 seconds | `5.00` |
| `BRL` | `pix` | PIX key | 1 second | `0.10` |

The delays are simulator time and are configurable. The fee is charged to Corridor, in the
payout's asset, when a payout completes.

### `POST /virtual-accounts`

Returns the account a customer deposits into. There is one per customer and asset: asking
again returns the same account with `200`.

```json
{"customer_reference": "0199b7c2-6f0e-7b1a-9d53-2c1f4e8a7b10", "asset": "USD"}
```

`201`:

```json
{
  "id": "va_8f3k2m9q",
  "customer_reference": "0199b7c2-6f0e-7b1a-9d53-2c1f4e8a7b10",
  "asset": "USD",
  "rail": "ach",
  "bank_name": "Sim Bank",
  "account_number": "900012345678",
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
{"id": "ben_4t7w1x", "asset": "USD", "rail": "ach", "holder_name": "Maria Silva", "account_mask": "••••6789"}
```

Refusals: `422 invalid_account` (wrong shape for the rail), `422 unsupported_asset`.

### `POST /payouts`

Requires `Idempotency-Key`.

```json
{"beneficiary_id": "ben_4t7w1x", "asset": "USD", "amount": "100.00", "reference": "0199b7c3-1a2b-7c3d-8e4f-5a6b7c8d9e0f"}
```

`201`:

```json
{
  "id": "po_2h5j8n",
  "status": "pending",
  "beneficiary_id": "ben_4t7w1x",
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

The payout as above; the list form returns `{"payouts": [...]}`. `404 payout_not_found`.
Corridor polls these when a webhook is overdue.

### `GET /transactions?asset=&from=&to=`

The provider's statement for one asset over a half-open window `[from, to)`: every settled
movement and the balance at the end of the window.

```json
{
  "asset": "USD",
  "from": "2026-01-15T00:00:00Z",
  "to": "2026-01-16T00:00:00Z",
  "transactions": [
    {"id": "dep_7c1d", "type": "deposit", "direction": "credit", "asset": "USD", "amount": "250.00",
     "reference": "INV-2041", "related_id": "va_8f3k2m9q", "occurred_at": "2026-01-15T09:30:00Z"},
    {"id": "po_2h5j8n", "type": "payout", "direction": "debit", "asset": "USD", "amount": "100.00",
     "reference": "0199b7c3-1a2b-7c3d-8e4f-5a6b7c8d9e0f", "related_id": "ben_4t7w1x", "occurred_at": "2026-01-15T12:00:30Z"},
    {"id": "po_2h5j8n:fee", "type": "payout_fee", "direction": "debit", "asset": "USD", "amount": "0.25",
     "reference": "0199b7c3-1a2b-7c3d-8e4f-5a6b7c8d9e0f", "related_id": "po_2h5j8n", "occurred_at": "2026-01-15T12:00:30Z"}
  ],
  "closing_balance": "149.75"
}
```

Types: `deposit` (credit), `deposit_return` (debit), `payout` (debit), `payout_fee` (debit).
A pending or failed payout is not a transaction. `closing_balance` may be negative.

### Bank webhooks

| Type | `data` |
|---|---|
| `deposit.received` | `deposit_id`, `virtual_account_id`, `customer_reference`, `asset`, `amount`, `sender_name`, `reference` |
| `deposit.returned` | `deposit_id`, `asset`, `amount`, `reason` |
| `payout.completed` | `payout_id`, `reference`, `asset`, `amount`, `fee`, `settled_at` |
| `payout.failed` | `payout_id`, `reference`, `asset`, `amount`, `failure_reason` |

## Custodian: `/custody/v1`

One network, `simchain`, carrying `USDC`. A block is produced every 2 seconds of simulator
time. A deposit is final after 3 confirmations.

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
{"id": "addr_5m2p9r", "customer_reference": "0199b7c2-6f0e-7b1a-9d53-2c1f4e8a7b10", "asset": "USDC",
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
  "id": "wd_9q4s6v",
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

The withdrawal as above; the list form returns `{"withdrawals": [...]}`.
`404 withdrawal_not_found`.

### `GET /transactions?asset=&from=&to=`

Same shape as the bank statement. Types: `deposit` (credit, once confirmed), `withdrawal`
(debit, once completed), `network_fee` (debit). Each carries `tx_hash`.

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
which case `deposit.failed` follows instead of `deposit.confirmed`.

## FX rates: `/fx/v1`

### `GET /rates/{base}/{quote}`

The mid-market rate: how many units of `quote` one unit of `base` buys.

```json
{"base": "USD", "quote": "MXN", "mid": "17.253400", "as_of": "2026-01-15T12:00:00Z"}
```

`mid` has six decimal places. Every ordered pair of two different assets is served, and the
rates are consistent: `rate(A, B) × rate(B, A)` is 1 to within rounding. `404 unknown_pair`
otherwise. `as_of` is when the rate was last updated; a consumer decides how old is too old.

The simulator moves each rate by a small step once per second of simulator time. The walk is
driven by a seed, so the same seed and the same number of steps give the same rates.

## Webhook delivery

Both providers deliver webhooks the same way.

```json
{
  "id": "evt_3n8b1c",
  "type": "payout.completed",
  "created_at": "2026-01-15T12:00:30Z",
  "data": {"payout_id": "po_2h5j8n", "reference": "0199b7c3-...", "asset": "USD", "amount": "100.00", "fee": "0.25",
           "settled_at": "2026-01-15T12:00:30Z"}
}
```

- **Signature.** The header `X-Signature: t=<unix seconds>,v1=<hex>` where `v1` is
  HMAC-SHA256, keyed with the shared secret, over the bytes `<t>.<raw request body>`. The
  receiver must verify against the body exactly as received, compare in constant time, and
  refuse a timestamp more than five minutes from its own clock.
- **At least once.** A delivery that does not get a `2xx` within the timeout is retried with
  the same event `id`: after 1, 2, 4, 8 and 16 seconds, six attempts in all. A receiver must
  treat a repeated `id` as already handled.
- **No ordering.** Events can arrive in any order, and `payout.completed` can arrive before
  the response to the `POST /payouts` that created the payout.
- **Not guaranteed.** After the last attempt the event is abandoned. The polling endpoints
  and the statement exist so that a receiver does not depend on webhooks alone.

## Simulator control: `/_control`

Not part of any provider's API. These endpoints exist so that tests and the demo can make
the outside world do something: receive a deposit, lose a webhook, time out. They are
unauthenticated and must never be reachable from outside a development or test environment.

| Endpoint | Effect |
|---|---|
| `POST /_control/reset` | Forget everything |
| `GET /_control/clock` | The simulator's time and mode |
| `POST /_control/clock/advance` `{"seconds": 30}` | Move time forward: settles payouts, produces blocks, moves rates, makes retries due |
| `POST /_control/bank/deposits` `{"virtual_account_id", "amount", "sender_name", "reference"}` | A customer's bank deposit arrives. An unknown `virtual_account_id` is accepted, to exercise unattributable deposits |
| `POST /_control/bank/deposits/{id}/return` `{"reason"}` | The sending bank recalls a deposit |
| `POST /_control/custody/deposits` `{"address", "amount", "from_address"}` | A transaction to a deposit address appears, unconfirmed |
| `POST /_control/custody/deposits/{id}/drop` | The transaction is dropped before it is final |
| `POST /_control/chain/mine` `{"blocks": 3}` | Produce blocks now |
| `POST /_control/fx/rates` `{"base", "quote", "mid"}` | Pin a rate |
| `POST /_control/fx/freeze` `{"frozen": true}` | Stop updating `as_of`, so rates go stale |
| `POST /_control/faults` `{"operation", "mode", "times", "status"}` | Make the next `times` calls to an operation fail |
| `DELETE /_control/faults` | Clear injected faults |
| `POST /_control/webhooks/behaviour` `{"duplicates", "drop_types", "hold", "reverse"}` | Change how webhooks are delivered |
| `POST /_control/webhooks/deliver` | Attempt every delivery that is due, now, and report the outcomes |
| `GET /_control/webhooks/events` | Every event and its delivery attempts |
| `GET /_control/bank/payouts`, `/_control/bank/deposits`, `/_control/bank/balances` | Inspect the bank's books |
| `GET /_control/custody/withdrawals`, `/_control/custody/deposits`, `/_control/custody/balances` | Inspect the custodian's books |

**Fault operations:** `bank.create_virtual_account`, `bank.create_beneficiary`,
`bank.create_payout`, `bank.get_payout`, `bank.list_transactions`, `custody.create_address`,
`custody.create_withdrawal`, `custody.get_withdrawal`, `custody.list_transactions`,
`fx.get_rate`.

**Fault modes:**

| Mode | What the caller sees | Did the operation happen? |
|---|---|---|
| `error` | The given `status` (default `503`) | No |
| `error_after_effect` | The given `status` | **Yes** |
| `timeout` | No response within the caller's deadline | No |
| `timeout_after_effect` | No response within the caller's deadline | **Yes** |

The two `after_effect` modes are the important ones. They reproduce a provider that accepted
a payout and then failed to say so, which is the case idempotency keys exist for.

**Webhook behaviour:** `duplicates` sends each event that many extra times; `drop_types`
lists event types that are never delivered; `hold` queues events without delivering them
until it is switched off; `reverse` delivers the queued events newest first.

**Clock.** In `manual` mode, time moves only through `/_control/clock/advance`, which makes a
test deterministic. In `realtime` mode the simulator follows the wall clock, which is what the
local stack and the demo use.
