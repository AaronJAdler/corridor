# 0020. The Increase sandbox behind the bank rail port, read conservatively

**Status:** accepted

## Context

[0019](0019-provider-mode-and-sandbox-guard.md) made room for a real provider's sandbox behind
the bank rail port and guarded the address. This is the adapter: USD over ACH on Increase's
sandbox, in `corridor.providers.increase`.

Three things about Increase do not fit the provider contract Corridor wrote for its
simulator, and decide most of what follows:

- **Its webhooks carry no data.** An Increase webhook is an Event: an id, a category, and the
  type and id of the object it is about. To learn what happened, the receiver reads the
  object ([webhooks](https://increase.com/documentation/webhooks)).
- **Its errors are RFC 9457 documents** with a machine-readable `type`, not the contract's
  `{"error": {"code", "message"}}` ([API overview](https://increase.com/documentation/api/overview)).
- **It signs by the Standard Webhooks scheme**, with three headers, not the contract's
  `X-Signature` ([webhooks](https://increase.com/documentation/webhooks)).

### Where the facts come from

Every fact about Increase below is from Increase's public documentation, and each is cited
where it is used. Two sources:

- **The documentation pages** at `increase.com/documentation`, cited by page.
- **Increase's published OpenAPI document**, which the API overview links
  ([openapi.json](https://increase.com/openapi.json)). The reference pages render their enum
  lists in the browser, so the values of `status`, `category` and the error `type`s were read
  from Increase's official Python SDK, which is generated from that document and which the
  overview also links ([github.com/increase](https://github.com/increase)): the files named
  below, at [`Increase/increase-python@726ea73`](https://github.com/Increase/increase-python/tree/726ea733539b3bc8d553c5ad228d76d149c64a04)
  (2026-11-04). Nothing was taken from the SDK's code but the values it declares.

Nothing was learned by calling Increase: the workspace this was written in cannot reach it,
and the key is held only in a GitHub Actions environment and on the owner's machine. Where
the documentation is silent, the adapter takes the conservative reading and the ADR says
which reading it took, so that the manual sandbox run can confirm or correct it.

**The key's prefix is not read.** No page says a key's prefix marks its environment; 0019's
rule stands. The address is what is checked, now in three places (settings, factory, and the
adapter's constructor), and the sandbox key not working in production is Increase's own
second line of defence ([sandbox](https://increase.com/documentation/sandbox)).

## Decision

### The port, mapped onto Increase

All calls are `Authorization: Bearer <key>` to `https://sandbox.increase.com`, JSON in and out,
with `Idempotency-Key` on every `POST` ([API overview](https://increase.com/documentation/api/overview)).
Increase guarantees at most one object per key, answers a repeat with the same arguments with
the original object, and answers a repeat with other arguments `409`
`idempotency_key_already_used_error`; keys are ASCII and at most 200 characters
([idempotency keys](https://increase.com/documentation/idempotency-keys)).

| Port method | Increase | Notes |
|---|---|---|
| `create_virtual_account` | `POST /account_numbers` with `account_id`, `name` (Corridor's user id) and `inbound_ach_debits: "blocked"` ([account numbers](https://increase.com/documentation/api/account-numbers)) | One account number per user, under the key `instr:<user>:USD`, so a retry returns the same one. Debits are blocked: a deposit account is for money in. Increase's `inbound_ach_debits` setting is `allowed` by default (same page), so Corridor asks for `blocked`. The response must echo the account, the name, `status: active` and `inbound_ach_debits: blocked`. |
| `create_beneficiary` | `POST /external_accounts` with `account_number` (1–17), `routing_number` (9), `description` (the holder's name), `funding: checking` ([external accounts](https://increase.com/documentation/api/external-accounts)) | The lengths are checked before sending, and a wrong shape is `invalid_account` without a call. Increase returns the full account number; the adapter masks it at once, as the simulator does. `invalid_parameters_error` on this call is read as `invalid_account`: the numbers and the name are all the request carries of the user's. |
| `create_payout` | `POST /ach_transfers` with `account_id`, `amount` in cents, `external_account_id`, `statement_descriptor` and `require_approval: false` ([ACH transfers](https://increase.com/documentation/api/ach-transfers)) | Amounts are integers in USD cents, positive for a credit (same page). **The reference is the idempotency key**: an ACH transfer keeps its `idempotency_key` and has no other field for Corridor's reference, so the adapter refuses a call whose two differ. The saga already passes the withdrawal id as both. |
| `get_payout` | `GET /ach_transfers/{id}` | |
| `find_payouts` | `GET /ach_transfers?account_id=&idempotency_key=` | Every list endpoint filters by key; a used key lists exactly one object, an unused one none ([idempotency keys](https://increase.com/documentation/idempotency-keys)). More than a page is not believed. |
| `list_transactions` | `GET /transactions?account_id=&created_at.on_or_after=&created_at.before=`, every page by `cursor`, and `GET /accounts/{id}/balance?at_time=` for the closing balance ([transactions](https://increase.com/documentation/api/transactions), [accounts](https://increase.com/documentation/api/accounts), pagination in the [API overview](https://increase.com/documentation/api/overview)) | `current_balance` is "the sum of all posted Transactions" (`types/balance_lookup.py`). Each line of an ACH payout is joined to its transfer, read once, to learn its key. Other currencies: ACH moves USD only (an ACH transfer's `currency` is always `USD`, [ACH transfers](https://increase.com/documentation/api/ach-transfers)), so the adapter answers an empty statement and a zero balance for MXN and BRL without asking. |

Corridor's keys that do not fit 200 characters (a client's key with a prefix can be up to 296)
are sent as `sha256:<hex>` of the key: the same key gives the same digest, so a retry still
finds the first attempt's object. A withdrawal id is 36 characters and is never digested.

#### An ACH transfer's status, as the port's three

From `types/ach_transfer.py` (`status`), each with Increase's description:

| Increase `status` | Payout status | Why |
|---|---|---|
| `pending_approval`, `pending_transfer_session_confirmation`, `pending_submission`, `pending_reviewing`, `requires_attention` | `pending` | Not yet sent to the Federal Reserve |
| `submitted`, `settlement` null | `pending` | Sent, not settled |
| `submitted`, `settlement` set | `completed` | "When the transfer settles, the status remains `submitted` and the `settlement` sub-object is populated"; `settlement.settled_at` is when the funds settled at the destination bank |
| `canceled`, `rejected` | `failed`, reason the status | Never left |
| `returned` | `failed`, reason `return.return_reason_code` | Returned by the receiving bank |
| anything else | unknown outcome | A status this version does not know is not read as any of these |

Increase charges no fee on the transfer object, so the payout's `fee` is zero.

#### Statement lines

From `types/transaction.py` (`source.category` and its sub-objects):

| Transaction `source.category` | Statement `type` | `id` | `related_id` | `reference` |
|---|---|---|---|---|
| `inbound_ach_transfer` | `deposit` | the inbound transfer's id (`transfer_id`) | the account number (`route_id`) | none |
| `inbound_ach_transfer_return_intention` | `deposit_return` | `<inbound id>:return` | the inbound transfer's id | none |
| `ach_transfer_intention` | `payout` | the ACH transfer's id | its external account | its idempotency key |
| `ach_transfer_return`, `ach_transfer_rejection` | `payout_return` | `<transfer id>:<category>` | the ACH transfer's id | none |
| any other | the category itself | the transaction's id | `route_id` | none |

Both a return and a rejection "offset the ACH Transfer Intention" (`types/transaction.py`). An
intention whose offset is in the same statement is left out with it: on Corridor's books that
money never left. A return of a transfer from before the window stays, as `payout_return`.
Reconciliation does not read `payout_return` or the other categories, so each shows only in the
balance comparison, which is where a person should see it. Lines of zero are left out. A line
of another account or currency, or outside the window, makes the statement unknown.

### Every answer is success, refusal or unknown

The shared client reads status codes as before: `2xx` with a believable body is success,
`401` is a fault of configuration, `5xx`, a timeout, a broken connection or an unbelievable
body is unknown. What changed is that each adapter reads its provider's `4xx` bodies; the
simulator's reading is unchanged. Increase's error types are from `_exceptions.py`:

| Increase `type` | Status | Corridor reads it as |
|---|---|---|
| `invalid_parameters_error`, `malformed_request_error` | 400 | Refusal (`invalid_parameters`, `malformed_request`) |
| `object_not_found_error` | 404 | Refusal (`object_not_found`). Reconciliation reads a 404 for a recorded payout as "gone", as with the simulator |
| `invalid_operation_error` | 409 | Refusal (`invalid_operation`). The overview's example is an insufficient balance |
| `idempotency_key_already_used_error` | 409 | Refusal with the code `idempotency_conflict`, which the saga already reads as unknown, never as a refusal of the payout |
| `invalid_api_key_error` | 401 | Configuration |
| `insufficient_permissions_error`, `private_feature_error`, `environment_mismatch_error` | 403 | Configuration |
| `api_method_not_found_error` | 404 | Configuration: Corridor called a path that does not exist |
| `rate_limited_error` | 429 | **Unknown.** Nothing happened, but it is not a refusal of the operation, and a refusal is what releases funds |
| `internal_server_error`, any other type | | Unknown |
| a `4xx` whose body is not Increase's document, or whose `status` is not the response's | | Unknown: something in between answered |

Only the title is kept as a refusal's message; `detail` can repeat what was sent. The
withdrawal saga, the ledger, idempotency and the lock order are unchanged: the saga already
asks the provider what it holds before it releases funds on a refusal
([0011](0011-refusal-check-before-release.md)), and `find_payouts` by key is that question.

### Webhooks

**Verification.** Increase follows Standard Webhooks: `webhook-id` (the Event's id),
`webhook-timestamp` (Unix seconds) and `webhook-signature`, a space-separated list of `v1,`
followed by the base64 HMAC-SHA256 of `<webhook-id>.<webhook-timestamp>.<raw body>`, one
signature per active secret during a rotation, each compared in constant time; the guide's
example tolerance is 300 seconds ([webhooks](https://increase.com/documentation/webhooks)).
`corridor.webhooks.standard` implements exactly that, against the raw body, with its own
secrets (`increase_webhook_secrets`), every signature tried against every secret without
stopping early, and the timestamp judged only after a match. Exactly one of each header is
accepted. A signature of an unknown version is passed over, as the scheme asks; a malformed
`v1` refuses the delivery.

**The key is the shared secret's bytes.** The Event Subscription's `shared_secret` is "the key
that will be used to sign webhooks" ([event subscriptions](https://increase.com/documentation/api/event-subscriptions)),
and the guide says the signing secret is the HMAC key. Neither says how a string becomes key
bytes. Increase's SDK passes the secret's UTF-8 bytes to the Standard Webhooks library
(`resources/events.py`, `unwrap`), and so does Corridor. The webhook run of 2026-11-08
confirmed it: ten deliveries verified, none refused.

**A provider's scheme only.** The simulators' deliveries are judged by `X-Signature` alone and
Increase's by the three headers alone, so a delivery signed one way never verifies on the
other's path. `validate_secrets` now refuses any secret shared between any two of the three
lists.

**The event's id is the signed id.** An Increase body's `id` must equal `webhook-id`, which is
under the signature, or the delivery is malformed. Events are stored and deduplicated by that id
(the guide says to use `webhook-id` as an idempotency key, and that duplicates happen). The
stored payload is the Event as sent, which names no person.

**Routing.** Events are stored under their `category`. The worker routes
`ach_transfer.created`, `ach_transfer.updated`, `inbound_ach_transfer.created` and
`inbound_ach_transfer.updated` (from `types/event.py`; the inbound page names its two as that object's
webhook categories) to one handler, which asks the bank rail to resolve the event and hands each
result to the existing bank functions with `provider="increase"`. Any other category is stored
and ignored. The port gains `BankEventSource.resolve_event`, which only an adapter whose events
need reading implements; `SimBank` does not, and its routes are unchanged.

**Any order.** Resolving reads the object as it is now, so an old event read late finds what is
true now, and a repeated event finds the same thing. The bank functions were already idempotent
and order-proof. The guide states no ordering guarantee, so none is assumed.

| Object, as read | Contract event handed on |
|---|---|
| Inbound ACH transfer, `direction: credit`, `status: accepted` | `deposit.received`: `deposit_id` = its id, `virtual_account_id` = `account_number_id`, `amount`, `sender_name` = `originator_company_name`, `reference` = `originator_company_entry_description` ([inbound ACH transfers](https://increase.com/documentation/api/inbound-ach-transfers); status values from `types/inbound_ach_transfer.py`) |
| Inbound ACH transfer, `status: returned` | `deposit.returned` with `transfer_return.reason`. A return the worker sees before the receipt is recorded as returned and credits nothing, as with the simulator |
| Inbound ACH transfer, `pending` or `declined` | nothing: no money moved |
| Inbound ACH transfer, `direction: debit` | nothing, and an error is logged. The numbers block debits; one taken anyway is money gone that reconciliation shows in the balance |
| ACH transfer, settled | `payout.completed` with `settled_at`, `fee` zero |
| ACH transfer, `canceled`, `rejected` or `returned` | `payout.failed` with the reason above |
| ACH transfer, pending | nothing |
| ACH transfer with no key, or a debit | nothing: not one Corridor sent |
| An object of another account, another id, or another type than its category | unknown: the event is retried |

**Fail closed, and never lost.** An Increase event at a worker whose bank rail is not Increase's
adapter raises `ProviderMisconfigured`: the event stays unprocessed and the outbox retries it, as
a withdrawal waits for a process with its provider. It is never marked ignored. The refusal of
every `increase` delivery from 0019 is removed.

### Events can be listed instead of received

A run in GitHub Actions has no address Increase could deliver to. Events are kept and listable
for 30 days, at `GET /events`, filtered by `category.in` and `created_at`, ordered by
`order_by`, paged by `cursor` ([events](https://increase.com/documentation/api/events); the
query encoding, dots for nesting and commas for lists, from `_client.py`). When the bank rail
is a `BankEventSource`, the worker runs `bank.poll_events` every ten seconds: it lists the four
routed categories, oldest first, from a day back on start and then from five minutes before the
newest event it has seen, and stores each through `webhooks.record`, under the same provider and
event id a webhook would use. A listed event and a delivered one are the same row, processed
once, so both may run at once. A listing is a call made with the API key, so it needs no
signature; what an event says is checked against its object either way. At most ten pages are
read per listing; the rest are read next time.

### The account is found from the key

Increase's account-number and ACH-transfer endpoints take an `account_id`, and a key can reach
more than one account. When `CORRIDOR_INCREASE_ACCOUNT_ID` is unset, the adapter lists
`GET /accounts?status.in=open` on first use and uses the account if there is exactly one open
USD account ([accounts](https://increase.com/documentation/api/accounts); `status` and
`currency` from `types/account.py`). None, several, or more than a page is a fault of
configuration that names the setting, and nothing is sent until it is set. A lookup that cannot
be answered is unknown, and nothing is sent either.

### Reconciliation asks each withdrawal's own rail

A run looked up every bank withdrawal in flight at whatever bank rail the process had. With a
second rail, a simulator payout would be looked up at Increase, which would answer `404`, and be
reported as an unknown payout. A run now asks only the provider a withdrawal went out by.

### Settings

`CORRIDOR_INCREASE_ACCOUNT_ID` (the account money moves from and numbers open on; optional, a
plain identifier) and `CORRIDOR_INCREASE_WEBHOOK_SECRETS` (a JSON list, 32+
characters each; Increase allows a `shared_secret` of 1–100). Neither is a key, and neither is
read in CI.

## Consequences

- With the defaults nothing changes: every adapter is the simulator and CI calls no provider.
- The adapter is tested against hand-written documents of the shapes cited above, including
  every status, every error type, and each refusal of a response that cannot be believed. Each
  guard has a test that fails when the guard is removed. What the sandbox really answers is for
  the manual run (stage 3) to show.
- **A return after settlement is not undone.** ACH returns "usually occur within the first two
  business days ... but can occur much later" (`types/transaction.py`). A payout is `completed`
  when it settles; a later return arrives as `payout.failed` for a completed withdrawal, which
  the saga refuses to release and logs as `withdrawal.failed_after_settlement` for a person.
  Holding payouts open until the return window has passed is a change to the saga, and is not
  made here.
- **A payout's line may be on the statement before Corridor settles it.** The documentation
  does not say when the intention is booked relative to `settlement.settled_at`, and Corridor
  settles on the latter. Reconciliation already allows for a payout paid and not yet settled.
- **The sender of an inbound ACH is its originator.** `originator_company_name` is who
  originated the entry, which for a consumer push may be a bank or a payroll company, not the
  person. Screening reads what it is given.
- **Events cost a read each.** One `GET` per event, made by the worker with no transaction open.
- **The sandbox run.** `scripts/sandbox_e2e.py`, run by hand from the `Sandbox end to end`
  workflow, which alone holds the key, in the `sandbox` environment: a deposit by a simulated
  inbound ACH, a withdrawal settled by `POST /simulations/ach_transfers/{id}/settle`, one
  returned by `.../return`, then the ledger verifier and reconciliation
  ([sandbox](https://increase.com/documentation/sandbox); paths from
  `resources/simulations/`). It prints what the sandbox answered for each reading below.
- **Confirmed by the sandbox run of 2026-11-07:** a simulated inbound ACH credit arrives
  `accepted`; a settled transfer keeps the status `submitted` with `settlement` filled in, as
  read above; a returned transfer reads `returned`. A new sandbox has no account, so the
  sandbox run opens one when the key reaches none. The deposit, the settled and the returned
  withdrawal, the ledger verifier and reconciliation all passed.
- **The webhook run.** With the workflow's `webhooks` input, the API is reached through a
  Cloudflare quick tunnel and the run registers an Event Subscription
  (`POST /event_subscriptions` with `url`, `shared_secret` and `selected_event_categories`,
  deleted at the end with `status: deleted`; [event subscriptions](https://increase.com/documentation/api/event-subscriptions)).
  Listing is turned off (`CORRIDOR_INCREASE_EVENT_POLLING=false`), so the scenario finishes
  only if every event arrives by webhook and verifies.
- **Confirmed by the webhook run of 2026-11-08:** the shared secret's UTF-8 bytes are the
  HMAC key. With listing off, all ten events of the scenario arrived by webhook and verified,
  none was refused, and the scenario passed.
- **Not yet confirmed by the sandbox:** whether a settled transfer's `settlement` is filled in
  without the settle simulation, which in production is the Federal Reserve's doing.
