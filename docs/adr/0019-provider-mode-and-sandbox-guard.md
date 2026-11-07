# 0019. A setting per provider chooses simulator or sandbox; CI stays on the simulator

**Status:** accepted

## Context

[0016](0016-simulators-as-separate-package.md) put the providers behind ports and wrote a
simulator against a contract that Corridor itself wrote. That proves Corridor against its own
reading of a provider. It cannot prove the reading: the adapters have never met a real
provider's response shapes, signatures or statements. A real provider's **sandbox** can, with
no real money involved.

The first target is the USD bank rail on Increase's sandbox. Increase documents two
environments: production requests go to `https://api.increase.com` and sandbox requests to
`https://sandbox.increase.com` ([API overview](https://increase.com/documentation/api/overview)).
The sandbox is described as the place to test with pretend money, and its keys do not work in
production ([sandbox](https://increase.com/documentation/sandbox),
[authentication for agents](https://increase.com/auth.md)).

Two things have to be settled before any adapter is written: how a process is told which
implementation to use, and how it is stopped from reaching the wrong host.

## Decision

- **One setting per provider.** `bank_rail_provider`, `custody_provider` and
  `fx_rates_provider` choose the implementation behind each port. Each defaults to `sim`.
  The bank rail also accepts `increase_sandbox`. Custody and rates accept only `sim`: no
  sandbox adapter exists for them, and a setting that accepted a value it could not honour
  would be a trap.
- **One place chooses.** `corridor.providers.factory` builds each port from those settings.
  The API and the worker call it and hold the result as a port. No module outside
  `corridor.providers` names an adapter, and a test and an `import-linter` contract fail if
  one does. The ports gain `aclose`, so a caller can release an adapter without knowing which
  it is.
- **The sandbox address is checked, exactly, twice.** With `increase_sandbox` selected, the
  settings refuse to load unless `increase_base_url` is exactly
  `https://sandbox.increase.com`, so the API, the worker and every command refuse to start.
  The factory checks it again where the adapter would be built, so that a settings object made
  without validation cannot point the adapter elsewhere. "Exactly" means a different case,
  port, path, trailing slash, scheme, user-info or sub-domain is refused.
- **The key's prefix is not read.** Increase's documentation does not say that a key's
  prefix marks its environment, so nothing here infers one from the other. The address is what
  Corridor controls, and it is what is checked. That a sandbox key cannot reach production is
  Increase's documented behaviour, a second line of defence and not the first.
- **Also refused:** `increase_sandbox` without `increase_api_key`, and `increase_sandbox` in a
  `production` environment.
- **Selecting a sandbox never falls back to the simulator.** Before the adapter existed, the
  factory raised `ProviderMisconfigured` for `increase_sandbox` rather than start a process
  that quietly had either no bank rail or the simulator. It now builds the Increase adapter
  ([0020](0020-increase-bank-rail-adapter.md)), and still never the simulator in its place.
- **CI stays on the simulator.** No job in `ci.yml` selects a sandbox, holds a provider key or
  calls a provider. The sandbox runs by hand, in its own workflow, in an environment that holds
  the key.

## What each one proves

| | The simulator | A sandbox |
|---|---|---|
| **Failure after the effect** (the provider acts, then the answer is lost) | Yes: `error_after_effect` and `timeout_after_effect` make the provider do the work and then fail to say so, on demand | No. A real provider cannot be told to do that |
| **Time** | Yes: a manual clock settles a payout, produces a block or makes a retry due at an instant a test picks, and no test sleeps | No. Time is the provider's |
| **Determinism** | Yes: the same seed and the same steps give the same identifiers and rates, so a failing run can be repeated | No. A run is not repeatable and its identifiers are the provider's |
| **Webhook misbehaviour** (duplicate, drop, reorder, hold) | Yes, on demand | Not on demand |
| **What the provider holds** (exactly one payout, from the provider's side) | Yes: the simulator keeps its own books and a test reads them | Only through the provider's own API |
| **Response shapes** | Only as Corridor wrote them | Yes: the provider's real ones |
| **Webhook signing** | Only Corridor's own scheme (`X-Signature`, HMAC-SHA256 over `t.body`) | The provider's scheme. Increase documents `webhook-id`, `webhook-timestamp` and `webhook-signature`, following the Standard Webhooks specification ([webhooks](https://increase.com/documentation/webhooks)). Whether the sandbox signs identically is for the sandbox run to show |
| **Statements** | Only as Corridor wrote them | The provider's real statement endpoint. Increase states that every API and dashboard feature is available in the sandbox ([sandbox](https://increase.com/documentation/sandbox)); what its entries look like is for the adapter to confirm |
| **External effects of its own** | Injected by a test | Increase's sandbox simulations mimic external events, such as incoming ACHs and returned ACHs, and compress events that take hours ([sandbox](https://increase.com/documentation/sandbox)) |
| **Webhook delivery to Corridor** | Yes: the simulator posts to a local address | Only to a public HTTPS address ([webhooks](https://increase.com/documentation/webhooks)). A CI runner has none |
| **Retries of webhooks** | Yes: the simulator retries as its contract says | Not in the sandbox: Increase states that failed webhooks are not retried there ([webhooks](https://increase.com/documentation/webhooks)) |

The two are not rivals. The simulator proves that Corridor's saga, locks and reconciliation
hold when a provider misbehaves in ways a sandbox will not misbehave on request. The sandbox
proves that the adapter's picture of one real provider is right. Neither proves what
production does under load, with real counterparties or with real money.

## Why CI stays on the simulator

- **Determinism.** A test that can fail because a third party was slow, was down or changed
  has no author to blame, and a suite that is sometimes red for no reason is ignored. The
  simulator's manual clock and seed make a run the same each time.
- **Failure injection.** The cases that cost money, a refusal after the effect and a webhook
  that never comes, cannot be produced in a sandbox on request. They are the reason the suite
  exists.
- **Secrets.** A job that calls a sandbox holds a key. The normal workflow runs on every pull
  request, and a pull request is code someone else may have written. A key belongs only to a
  workflow that nobody triggers without choosing to, in an environment that gates it.
- **Reach.** A runner has no public HTTPS address, so webhook delivery could not be exercised
  from CI in any case.
- **Cost of a false green.** A sandbox job in the normal workflow would be a job that passes
  on the provider's good day and means little.

## Consequences

- A process started with the defaults behaves exactly as before: every adapter is the
  simulator, and the suite does not change.
- Choosing a different bank rail is a change to the factory and to one adapter. No caller is
  touched.
- The key is read from `CORRIDOR_INCREASE_API_KEY`. It is a `SecretStr`, so it is not printed
  by the settings, by a validation error or by a log line.
- The guard compares one string. If Increase ever moves its sandbox, the constant is changed
  by a person who has read the new documentation.
- **A leak, found and fixed with this change.** The provider names `simbank` and
  `simcustody` were written into `corridor.payments.types`, the `webhooks.Provider`
  enumeration and the `provider` check constraint of the webhook table, and the payment
  handlers took the bank's name from a constant. A bank rail with another name could not
  receive a webhook or record a deposit. Names now follow the provider:
  - `corridor.providers.names` is the one place a name is spelled out, and says which kind
    of money each moves. A test scans `corridor` for a literal anywhere else.
  - The bank handlers take the provider the event came from. Deposits, instructions,
    withdrawals and ledger settlement accounts were already scoped by it, so a second rail
    has rows and accounts of its own, and one deposit id at two rails is two deposits.
  - A provider's event settles only a withdrawal that went out by that provider, and a
    process sends a withdrawal only through an adapter of the provider the row names. The
    second guard matters when the bank rail setting is changed while a withdrawal is in
    flight: the funds stay reserved until a process that has that provider picks it up.
  - Migration `0021` lets the webhook table hold `increase`; a test compares the table's
    constraint with the enumeration. Simulator and sandbox rows share one database, told
    apart by the name.
  - `increase` deliveries were named and routed but refused at verification until the
    signature scheme was implemented, so nothing of one was believed in the meantime.
    [0020](0020-increase-bank-rail-adapter.md) implements it and removes the refusal.
- The sandbox can prove the adapter's shapes. It cannot prove the error semantics under
  failure after the effect; the simulator remains the only place that is tested.
