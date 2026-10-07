# Corridor runbook

What to do when something needs a person. Each section says what the signal means, how to
find out what is going on, and what to do. The design behind each mechanism is in the
[architecture](architecture.md); the endpoints are in the [API guide](api.md).

Three rules apply to every procedure here:

1. **Never edit money by hand.** Do not `UPDATE` a balance, a deposit, a withdrawal or a
   journal entry. A correction is a new journal entry, made through an adjustment that a
   second administrator approves.
2. **Never release funds for a payout the provider may have made.** Ask the provider first.
3. **Write down what you did.** The admin API audits itself. Anything done with SQL is not
   audited, so record it where your team keeps such records.

## Contents

- [Tools](#tools)
- [Signals](#signals)
- [Ledger verifier finding](#ledger-verifier-finding)
- [Outbox backlog](#outbox-backlog)
- [Dead letters](#dead-letters)
- [Reconciliation breaks](#reconciliation-breaks)
- [Redis is unavailable](#redis-is-unavailable)
- [Provider errors](#provider-errors)
- [A withdrawal that does not finish](#a-withdrawal-that-does-not-finish)
- [The review queue](#the-review-queue)
- [Funds in suspense](#funds-in-suspense)
- [A restricted user](#a-restricted-user)
- [Rotating secrets and keys](#rotating-secrets-and-keys)
- [Restoring from a backup](#restoring-from-a-backup)

## Tools

**The admin API.** Every admin endpoint needs the access token of a user whose role is
`admin`. In PowerShell:

```powershell
$api = "http://127.0.0.1:8000"
$body = @{ email = "operator@example.com"; password = "<password>" } | ConvertTo-Json
$login = Invoke-RestMethod -Method Post -Uri "$api/v1/auth/login" -ContentType "application/json" -Body $body
$admin = @{ Authorization = "Bearer $($login.access_token)" }

Invoke-RestMethod -Headers $admin -Uri "$api/v1/admin/outbox/dead"
```

The token lives 15 minutes. Log in again when a call answers `401`.

**The first administrator.** The role endpoint needs an administrator, so the first one is
made from a shell, by someone who holds the owner connection
(`CORRIDOR_DATABASE_OWNER_URL`, the one migrations use). The user registers through the
API first. Then:

```powershell
uv run corridor users make-admin --email operator@example.com --yes
```

Without `--yes` the command says what it would do, changes nothing and exits with code 1.
It finds the account by its email address only, refuses a closed one, and does nothing to
a user who is an administrator already. The change is written to the audit log as
`user.role_changed`, with the actor `system` / `cli.users.make_admin`. The user must log
in again afterwards: a token that carries the old role is refused. After that, use
`POST /v1/admin/users/{user_id}/role`. In the compose stack, where only the migration
service is given the owner connection:

```powershell
docker compose run --rm migrate corridor users make-admin --email operator@example.com --yes
```

**The audit log.** `GET /v1/admin/audit` reads it, newest first, filtered by `actor`,
`action` (what the action begins with), `subject` (the id of what was acted on, or of the
user it was done for), `since` and `until`. Each read is itself one audit event.

```powershell
(Invoke-RestMethod -Headers $admin -Uri "$api/v1/admin/audit?subject=<id>").items | Format-List occurred_at, actor_type, actor_id, action, outcome, request_id, details
```

**SQL.** Read-only queries in this document can be run as any role that can read the
tables. In the local compose stack:

```powershell
docker compose exec postgres psql --username postgres --dbname corridor
```

**The ledger verifier.** `corridor verify-ledger` recomputes every ledger invariant from
the postings, and that every held and suspense balance is accounted for by a withdrawal
or a deposit, and prints each finding. It only reads. It needs the same database and Redis
settings as the API (`CORRIDOR_DATABASE_URL`, `CORRIDOR_REDIS_URL`). It exits with code 1
if it finds anything.

```powershell
uv run corridor verify-ledger
docker compose run --rm api corridor verify-ledger
```

**Logs.** Both processes write one JSON object per line. Every line of a request carries
its `request_id`, which is also in the `X-Request-ID` response header and in error bodies.
Outbox events carry the request id of the request that enqueued them. Useful event names
are given in each section.

**Metrics.** The worker serves its metrics on `CORRIDOR_WORKER_METRICS_PORT` when that is
set. The API serves its own at `/metrics` only when `CORRIDOR_METRICS_PUBLIC` is `true`.

**The simulator, in a development stack.** `GET /_control/bank/payouts`,
`/_control/custody/withdrawals`, `/_control/webhooks/events` and the balances endpoints show
what the simulated providers hold. See [provider-api.md](provider-api.md#simulator-control-_control).

## Signals

| Signal | Condition | Section |
|---|---|---|
| `corridor_ledger_verifier_findings` | Above 0 | [Ledger verifier finding](#ledger-verifier-finding) |
| `corridor_ledger_verifier_last_run_timestamp_seconds` | More than two hours old | The worker is not running its jobs. See [Outbox backlog](#outbox-backlog) |
| `corridor_outbox_oldest_pending_seconds` | Above a threshold such as 60 | [Outbox backlog](#outbox-backlog) |
| `corridor_outbox_dead` | Above 0 | [Dead letters](#dead-letters) |
| `corridor_recon_open_breaks` | Above 0 | [Reconciliation breaks](#reconciliation-breaks) |
| `corridor_recon_break_changes_total` | Increasing | An open break whose difference keeps changing. Same section |
| `corridor_redis_unavailable_total` | Increasing | [Redis is unavailable](#redis-is-unavailable) |
| `corridor_provider_calls_total` | A rising share with an outcome other than `ok` | [Provider errors](#provider-errors) |
| `corridor_scheduled_job_runs_total{outcome="error"}` | Increasing | Read `job_runs.last_error` for the job (below) |
| `corridor_webhook_deliveries_total{outcome="bad_signature"}` | Increasing | A provider is signing with a secret Corridor does not have, or someone is probing. See [Webhook secrets](#webhook-secrets) |
| `corridor_db_transaction_retries_total` | Above 0 | A deadlock was retried. Not urgent. It means two code paths take locks in different orders, which is a defect to report |
| `corridor_rate_limit_rejections_total` | A sudden rise in one group | A client is retrying too hard, or an attack. The access log shows which routes |

No alert rules are shipped. The thresholds are starting points.

What each scheduled job last did:

```sql
SELECT name, last_started_at, last_finished_at, last_error FROM job_runs ORDER BY name;
```

`last_finished_at` is null while a run is in progress, and stays null if the worker died
during the run. The job is due again one interval after `last_started_at`.

## Ledger verifier finding

**What it means.** The ledger broke one of its own rules, or holds money for withdrawals or
in suspense that no withdrawal or deposit accounts for. The application refuses to write
a violation and the database refuses to store one, so a finding means both failed, or that
someone changed data outside the application. Treat it as serious until explained.

Two verifiers run together, in `corridor verify-ledger` and in the worker's hourly
`ledger.verify` job, and report into the same gauge. The ledger's checks what the ledger
alone can know. The one in payments (`held_mismatch`, `suspense_mismatch`) compares the
ledger with the withdrawals and the deposits.

Neither can see a whole balanced entry that was deleted together with its postings and
with the balances rewritten to match: nothing is left to compare. Only a superuser with
the triggers switched off can do that, and reconciliation against the providers is what
would notice the money.

**Diagnose.**

1. Run `corridor verify-ledger`. Each line is `check: subject: detail`. The worker's log
   line `ledger.verify_failed` names only the checks.

| Check | Subject | Meaning |
|---|---|---|
| `unbalanced_entry` | entry id | Debits and credits differ in an asset |
| `too_few_postings` | entry id | An entry with fewer than two postings |
| `negative_balance` | account id | A cached balance below zero |
| `balance_mismatch` | account id | A cached balance that is not the sum of the account's postings |
| `broken_balance_chain` | account id | A posting whose `balance_after` is not what the postings before it give |
| `stale_balance_pointer` | account id | A balance row that does not point at the account's latest posting |
| `missing_balance_row`, `unexpected_balance_row`, `stray_balance_after` | account id | A cached balance where there should be none, or none where there should be one |
| `negative_suspense` | account id | More was taken out of a suspense account than was put in |
| `account_off_chart` | account id | An account whose category, normal side, owner, provider or constraint is not what the chart of accounts gives its kind. Every balance on it may be read with the wrong sign |
| `reversal_mismatch` | entry id (the reversal) | A reversal whose postings are not the mirror image of the entry it says it reverses |
| `held_mismatch` | `user id:asset` | A user's held balance is not the amount plus fee of their withdrawals that are `held`, `submitting` or `submitted` |
| `suspense_mismatch` | asset | What suspense holds in an asset is not the sum of the deposits in `suspense` in that asset |

2. Find what the subject is.

```sql
-- an entry and its postings
SELECT e.id, e.kind, e.source_type, e.source_id, e.posted_at, e.metadata
  FROM journal_entries e WHERE e.id = '<entry id>';
SELECT seq, account_id, asset_code, direction, amount, balance_after
  FROM postings WHERE entry_id = '<entry id>' ORDER BY seq;

-- an account, its cached balance, and its latest postings
SELECT * FROM ledger_accounts WHERE id = '<account id>';
SELECT * FROM account_balances WHERE account_id = '<account id>';
SELECT p.seq, p.entry_id, e.kind, p.direction, p.amount, p.balance_after, e.posted_at
  FROM postings p JOIN journal_entries e ON e.id = p.entry_id
 WHERE p.account_id = '<account id>' ORDER BY p.seq DESC LIMIT 20;
```

3. `source_type` and `source_id` on the entry name the business object (a transfer, a
   withdrawal, a deposit by provider and provider id, a conversion, an adjustment). Read
   its audit trail, with the id of the transfer, withdrawal, deposit, conversion or
   adjustment as the subject:

```powershell
(Invoke-RestMethod -Headers $admin -Uri "$api/v1/admin/audit?subject=<id>").items | Format-List occurred_at, actor_type, actor_id, action, outcome, request_id, details
```

**What to do.**

- If the finding involves a user's balance, consider stopping that user from moving money
  while you investigate: `POST /v1/admin/users/{user_id}/restrict` with a reason. See
  [A restricted user](#a-restricted-user).
- A wrong amount in the books is corrected with an adjustment
  (`POST /v1/admin/adjustments`), requested by one administrator and approved by another.
- A cached balance that disagrees with correct postings cannot be corrected with an
  adjustment, because an adjustment adds postings. There is no tool that rebuilds a cached
  balance. It needs a change to `account_balances` made as the owner role, with a second
  person watching, after the cause is understood. Report the cause as a defect.
- `negative_suspense` means suspense money was paid out twice. Every way out of suspense
  checks the deposit's status under a lock and an adjustment written by hand cannot debit
  suspense, so this should not happen through the application. Find the entries that
  debited suspense and compare them with the deposits:

```sql
SELECT e.id, e.kind, e.source_type, e.source_id, e.posted_at, p.direction, p.amount
  FROM postings p
  JOIN journal_entries e ON e.id = p.entry_id
  JOIN ledger_accounts a ON a.id = p.account_id
 WHERE a.kind = 'suspense' AND a.asset_code = '<asset>'
 ORDER BY p.seq DESC LIMIT 50;
```

- `account_off_chart` cannot be produced through the application or by the owner role: a
  trigger refuses any change to `ledger_accounts` and a constraint holds each account to
  the chart. It means a superuser changed the table with both out of the way. Compare the
  account with the chart in section 4.2 of the architecture; putting it right is a change
  made by a superuser, with a second person watching, and a defect report.
- `reversal_mismatch`: read both entries with the queries above. `reverses_entry_id` on
  the reversal names the original. The one that was changed after it was posted is the
  one whose postings no longer match its business object.
- `held_mismatch`: list the user's withdrawals of that asset and the postings on their
  held account, and find the one without the other.

```sql
SELECT id, status, amount, fee, hold_entry_id, final_entry_id, updated_at
  FROM withdrawals WHERE user_id = '<user id>' AND asset_code = '<asset>' ORDER BY id DESC;
SELECT p.seq, e.kind, e.source_type, e.source_id, p.direction, p.amount, p.balance_after
  FROM postings p
  JOIN journal_entries e ON e.id = p.entry_id
  JOIN ledger_accounts a ON a.id = p.account_id
 WHERE a.kind = 'user_held' AND a.owner_id = '<user id>' AND a.asset_code = '<asset>'
 ORDER BY p.seq DESC LIMIT 50;
```

  The application moves a held balance only together with its withdrawal, and an
  adjustment written by hand may not debit one, so there is no operator action that
  corrects this. Restrict the user, so that nothing else moves while it is looked at, and
  report it as a defect with both listings. If an adjustment credited the held account by
  hand, that adjustment is the cause: its entry id is on the posting.
- `suspense_mismatch`: compare the deposits in suspense
  (`GET /v1/admin/deposits/suspense`) with the suspense postings from the query above. An
  adjustment written by hand that credited suspense is the likely cause: it put money
  there that no deposit names, and nothing can release or return money without a deposit.
  It is a defect to report; do not try to debit suspense by hand, which is refused.
- Run `corridor verify-ledger` again afterwards. The gauge returns to 0 at the next hourly
  run.

## Outbox backlog

**What it means.** `corridor_outbox_oldest_pending_seconds` is how long the oldest event
that is due has waited. Normally it is under a second. A growing value means events are
not being processed: withdrawals are not being sent and webhooks are not being applied.
Money is safe while this lasts. Funds stay held and events stay stored.

**Diagnose.**

```sql
SELECT status, count(*), min(available_at) AS oldest_due FROM outbox_events GROUP BY status;

SELECT id, topic, status, attempts, available_at, locked_until, last_error
  FROM outbox_events
 WHERE status IN ('pending', 'processing') ORDER BY id LIMIT 50;
```

| What you see | Likely cause |
|---|---|
| The gauge is stale and no `worker.started` in the log | No worker is running |
| Log line `worker.drain_failed` | The worker cannot reach PostgreSQL |
| Many `pending` with rising `attempts` and the same `last_error`; log lines `outbox.event_retry` | A handler keeps failing. Often a provider is down: see [Provider errors](#provider-errors) |
| Rows stuck in `processing` with `locked_until` in the past | A worker died mid-event. They are claimed again once another worker runs |
| Many `pending` with `attempts = 0` | The worker is slower than the arrival rate. Raise `CORRIDOR_OUTBOX_CONCURRENCY` or run another worker |

**What to do.** Start or restart the worker. Several workers can run at once. Do not
delete or edit outbox rows: an event that fails eight times becomes a dead letter and is
handled as one. To check that a worker is alive without waiting for real work, insert a
`worker.ping` event and watch it become `done`:

```sql
INSERT INTO outbox_events (id, topic, payload, status, attempts, available_at, context, created_at)
VALUES (gen_random_uuid(), 'worker.ping', '{}', 'pending', 0, now(), '{}', now())
RETURNING id;

SELECT status, attempts, finished_at FROM outbox_events WHERE id = '<that id>';
```

## Dead letters

**What it means.** An outbox event failed 8 times, or its topic has no handler, or its
last claim expired while a worker was handling it. It will not be tried again until an
administrator requeues it. `corridor_outbox_dead` counts them and the log line is
`outbox.event_dead`.

**Diagnose.** List them. `last_error` is the last failure with secrets removed.

```powershell
(Invoke-RestMethod -Headers $admin -Uri "$api/v1/admin/outbox/dead").items | Format-List id, topic, attempts, last_error, payload
```

| Topic | What is waiting | Before you requeue |
|---|---|---|
| `withdrawal.submit` | A withdrawal that was not sent, or whose sending was not recorded. Its funds are still held | Fix the cause in `last_error` (provider down, provider not configured). The payout sweeper also asks again for a withdrawal left in `submitting`, so this may already have resolved itself: read the withdrawal first |
| `webhook.received` | A stored provider event that was not applied | Read the event: `SELECT provider, event_id, type, received_at, processed_at FROM webhook_events WHERE id = '<payload.webhook_event_id>'`. `ProviderEventMismatch` in `last_error` means the event names an asset, an amount or a payout that differs from what Corridor recorded. Do not requeue that until someone has compared both sides |
| `transfer.completed`, `deposit.completed`, `fx.converted`, `worker.ping` | Nothing. These handlers do nothing | A dead one means the handler is missing from the deployed worker |
| Any topic with `no handler for topic` | The worker that ran it is older or newer than the code that enqueued it | Deploy matching versions first |
| `claim expired after the last attempt` | The handler hung or killed its worker each time | Find out why before giving it eight more tries |

**What to do.** When the cause is fixed, requeue. The event gets a full set of attempts and
the worker picks it up within about 5 seconds.

```powershell
Invoke-RestMethod -Method Post -Headers $admin -Uri "$api/v1/admin/outbox/dead/<event id>/requeue"
```

Requeueing is safe to do more than once in the sense that handlers are idempotent, and the
endpoint refuses an event that is no longer dead. Listing and requeueing are audited. Dead
events are never deleted automatically.

## Reconciliation breaks

**What it means.** A reconciliation run found that Corridor and a provider disagree about
something and could not repair it by itself. `corridor_recon_open_breaks` is the number of
open breaks.

**Diagnose.**

```powershell
(Invoke-RestMethod -Headers $admin -Uri "$api/v1/admin/recon/breaks?status=open").items
(Invoke-RestMethod -Headers $admin -Uri "$api/v1/admin/recon/runs?limit=5").items
```

`expected` is what Corridor recorded, `actual` is what the provider reports, and
`provider_ref` is the provider's id for the deposit or payout (or the asset code for a
balance). A run with status `incomplete` could not read a provider for everything it
asked; the log line `recon.provider_failed` says which. An open break is refreshed by
every later run that still sees it, so `expected` and `actual` are current.
`corridor_recon_break_changes_total` counts, by kind, each time a run found the difference
of an open break changed: a break that keeps moving is one whose cause is still at work.

**Each kind.**

| Kind | What happened | What to check | What to do |
|---|---|---|---|
| `missing_deposit` | The provider's statement has a deposit Corridor has not credited. Normally the run repairs this itself and closes the break with `resolved_by: system` | An open one means the repair was refused: log line `recon.repair_refused`. Usually the statement line names an asset or amount that differs from a deposit Corridor already recorded | Compare `SELECT * FROM deposits WHERE provider = '<provider>' AND provider_ref = '<ref>'` with the provider's record. If the provider is right, credit the difference with an adjustment. Then resolve the break |
| `missing_return` | The bank's statement shows a deposit as recalled, and it is still credited here (or still in suspense). Normally the run repairs this itself, exactly as the bank's own `deposit.returned` would have: the money is taken back from the user, a shortfall is booked as owed and the user restricted, and the break is closed with `resolved_by: system` | An open one means the repair was refused (`recon.repair_refused`): the recall's asset or amount differs from the deposit Corridor recorded | Compare the deposit (`SELECT * FROM deposits WHERE provider = 'simbank' AND provider_ref = '<ref>'`) with the bank's record. If the bank is right about a different amount, that is an `amount_mismatch` to settle with an adjustment; then resolve the break. After a repair, see [A restricted user](#a-restricted-user) if the user was left owing |
| `missing_payout_result` | A withdrawal is in flight here and the provider says it finished. Normally repaired by the run | An open one means the repair was refused, or the run was `incomplete` | Read the withdrawal (`SELECT * FROM withdrawals WHERE provider_ref = '<ref>'`). If it has finished since, the next complete run closes the break. If not, see [A withdrawal that does not finish](#a-withdrawal-that-does-not-finish) |
| `unknown_deposit` | Corridor credited a deposit that is not on the provider's statement | Whether the provider really has no record. A statement window that ends too early also causes this | If the provider confirms it never received the money, the credit must be reversed with an adjustment, and the user may need restricting. This needs a decision by a person |
| `unknown_payout` | The provider paid out something Corridor cannot match, or Corridor shows a payout the provider does not have | The log lines `withdrawal.paid_out_after_release` and `payout_sweep.mismatch`. Find the withdrawal by the payout's reference, which is the withdrawal id | If the provider paid a withdrawal whose funds went back to the user, the user has the money twice: recover it with an adjustment, or record the loss. If Corridor settled something the provider never paid, the user is owed a payout |
| `amount_mismatch` | Both sides have the deposit or payout, with different amounts | Both records | Decide which is right. Correct Corridor's side with an adjustment for the difference |
| `settlement_balance` | The settlement account's balance differs from the provider's closing balance after allowing for items in transit | Every other open break for that provider and asset first: one unmatched item explains a balance difference of the same size. Then entries posted by adjustments | Often this closes itself once the item that caused it is settled, for example a suspense return that the bank has not sent back yet. If nothing explains it, compare the provider's statement line by line with the settlement account's postings |

The settlement account's postings, for the last comparison:

```sql
SELECT p.seq, e.kind, e.source_id, p.direction, p.amount, e.posted_at
  FROM postings p
  JOIN journal_entries e ON e.id = p.entry_id
  JOIN ledger_accounts a ON a.id = p.account_id
 WHERE a.kind IN ('bank_settlement', 'custody_omnibus')
   AND a.provider = '<provider>' AND a.asset_code = '<asset>'
 ORDER BY p.seq DESC LIMIT 100;
```

**Resolve.** When the disagreement is settled or explained, close the break with a note
that says why. Resolving changes no money.

```powershell
$note = @{ note = "Bank confirmed the return on its next statement." } | ConvertTo-Json
Invoke-RestMethod -Method Post -Headers $admin -Uri "$api/v1/admin/recon/breaks/<break id>/resolve" -ContentType "application/json" -Body $note
```

A break you resolve while the disagreement still exists is opened again by the next run.

**After an outage.** A run is made every `CORRIDOR_RECONCILIATION_INTERVAL_SECONDS` (5
minutes) over the last `CORRIDOR_RECONCILIATION_WINDOW_SECONDS` (an hour). If the last
completed run ended longer ago than that, the next run starts where that run ended, up to 7
days back, so a worker that was down for a few hours still reconciles the gap. A gap
longer than 7 days needs the statements compared by hand.

**A deposit that is not repaired at once.** A deposit on a statement that the provider
received less than `CORRIDOR_RECONCILIATION_GRACE_SECONDS` ago (2 minutes) is left for its
webhook, and is repaired by a later run if the webhook never comes. A deposit that the
statement shows as received and then returned, and that Corridor never booked, is not
credited: it is recorded as `returned` with nothing posted, and its break is closed.

## Redis is unavailable

**What it means.** `corridor_redis_unavailable_total` is rising, `/readyz` answers
`"status": "degraded"`, and requests that move money are answered
`503 rate_limiter_unavailable`. That last part is by design: a money-moving request that
cannot be counted against its rate limit is refused.

| Still works | Does not work |
|---|---|
| Logins, registration, reading wallets, deposits, transfers and withdrawals. Canceling a withdrawal that is still `held` | `POST` on transfers, withdrawals, beneficiaries, FX quotes and conversions, and approving an agent's request |
| Webhook intake and everything the worker does: payouts in flight still settle, deposits are still credited | Immediate effect of a logout. A logged-out session's access token works until it expires, at most 15 minutes |
| Deposit instructions | The shared FX rate cache. Each quote would ask the rate source |

No money is at risk and nothing needs repair afterwards. Clients can retry refused
requests with the same idempotency key.

**What to do.** Restore Redis. Nothing has to be reloaded into it: buckets start full, the
rate cache refills, and revocation marks for sessions ended during the outage are
absent (their tokens expire within 15 minutes). If Redis will be down for long and
refusing money movement is worse than not rate-limiting it, there is no switch that makes
money writes fail open. `CORRIDOR_RATE_LIMIT_ENABLED=false` turns every rate limit off, is
refused in a production configuration, and should not be used as a workaround.

## Provider errors

**What it means.** `corridor_provider_calls_total` counts calls by outcome.

| Outcome | Meaning | Effect |
|---|---|---|
| `rejected` | The provider refused with a `4xx`. Nothing happened there | Normal in small numbers (an invalid account, for example) |
| `unknown` | A timeout, a broken connection, a `5xx`, or an answer that does not match the contract | The operation is retried with the same idempotency key. Withdrawals stay `submitting` with funds held. Users get `503 provider_unavailable` or `503 rate_unavailable` on deposit instructions, beneficiaries and quotes |
| `misconfigured` | No address, no key, or the provider answered `401` | No retry helps. Fix `CORRIDOR_*_URL` and `CORRIDOR_*_API_KEY` and restart |

**Diagnose.** The log lines carry the provider and the operation: `fx.rate_fetch_failed`,
`fx.rate_stale`, `payout_sweep.provider_failed`, `recon.provider_failed`,
`outbox.event_retry`. `fx.rate_stale` means the rate source answers with a rate older than
15 seconds: its feed has stopped, and quotes are refused until it resumes.

**What to do.** Nothing to the data. When the provider is back, retries, the payout
sweeper and reconciliation bring everything up to date. If the outage outlasted eight
attempts, some `withdrawal.submit` events are dead; the sweeper asks again for those
withdrawals by itself, and the dead events can then be left or requeued.

## A withdrawal that does not finish

Find it and its history:

```sql
SELECT id, user_id, asset_code, amount, fee, kind, status, provider, provider_ref,
       failure_reason, created_at, updated_at, submitted_at
  FROM withdrawals WHERE id = '<withdrawal id>';

-- every withdrawal in flight, oldest first
SELECT id, status, provider, provider_ref, updated_at
  FROM withdrawals WHERE status IN ('held', 'submitting', 'submitted') ORDER BY id;
```

```powershell
(Invoke-RestMethod -Headers $admin -Uri "$api/v1/admin/audit?subject=<withdrawal id>").items | Format-List occurred_at, actor_type, actor_id, action, details
```

**`held` for more than a few seconds.**

- It has an open review: `SELECT * FROM risk_reviews WHERE subject_type = 'withdrawal' AND
  subject_id = '<id>'`. See [The review queue](#the-review-queue).
- Its `withdrawal.submit` event has not run: see [Outbox backlog](#outbox-backlog).
- Its event is dead with "no bank rail is configured" or "no custodian is configured": the
  worker has no provider settings. Fix them and restart the worker. The dead letter can
  be requeued, and need not be: see the next point.
- It has no event left at all, or only a dead one. After 2 minutes
  (`CORRIDOR_PAYOUT_SWEEP_AFTER_SECONDS`) the payout sweeper writes a new
  `withdrawal.submit` event for a held withdrawal whose user is active, that has no open
  or rejected review, and that has no such event pending or being processed. It does so
  at most once every 2 minutes for one withdrawal, and logs `payout_sweep.resent_held`.
  A withdrawal that keeps being asked for and stays `held` has an event that keeps
  failing: read the dead letters.

A `held` withdrawal has not been sent. Its user can cancel it, also while Redis is down.
An agent can cancel only a withdrawal it asked for itself (`403 withdrawal_not_agents`
otherwise).

**`submitting`.** The worker marked it and has not recorded the provider's answer. The
provider may or may not have the payout.

- The outbox retries the submission with the same idempotency key, so the provider makes
  one payout however often it is asked.
- After 2 minutes (`CORRIDOR_PAYOUT_SWEEP_AFTER_SECONDS`) the payout sweeper asks the
  provider what it holds under the withdrawal's id. If the provider has a payout, the
  sweeper records it. If it has none, the sweeper asks for the withdrawal to be sent
  again, at most once every 2 minutes. Log lines: `payout_sweep.resubmitted`,
  `payout_sweep.provider_failed`.
- It stays `submitting` only while the provider cannot be reached, or when the provider
  holds more than one payout under the reference (`payout_sweep.several_payouts`). The
  second case needs a person at the provider.

**`submitted`.** The provider accepted the payout and has not said how it ended. The
sweeper reads the payout every 30 seconds once it is 2 minutes old and settles or fails the
withdrawal when the provider reports an end. If the provider still says `pending`, the
withdrawal is waiting on the provider and nothing is wrong on Corridor's side.
`payout_sweep.mismatch` means the payout recorded on the withdrawal is not the one the
provider has under that id; nothing is moved, and a person has to compare both.

**What not to do.** Do not change `status` with SQL. Do not return the funds with an
adjustment while the provider might hold a payout: ask the provider for payouts under the
withdrawal's id first (`GET /payouts?reference=<withdrawal id>` on the bank rail,
`GET /withdrawals?reference=` on the custodian). If the provider confirms it holds
nothing and will never pay, the supported way to give the funds back is to let the
submission run: a refusal from the provider releases them.

**Three log lines that always need a person.**

| Log line | Meaning |
|---|---|
| `withdrawal.paid_out_after_release` | The provider reports a payout for a withdrawal whose funds went back to the user. The user has the money twice |
| `withdrawal.failed_after_settlement` | The provider reports a failure for a withdrawal Corridor already settled |
| `withdrawal.sent_without_reservation` | The provider has a payout for a withdrawal that was never marked as sent |

## The review queue

**What it means.** Screening found a party on the deny list, or a deposit arrived for an
account that has been closed. A withdrawal whose destination is listed as `review` is held
with its funds reserved. A deposit whose sender is listed (as `review` or `deny`) is in
suspense, and so is a deposit for a closed account (its review has the outcome `review`,
and its `deposit.suspended` audit event says `reason: owner_closed`). Each has one open
review.

**Diagnose.**

```powershell
(Invoke-RestMethod -Headers $admin -Uri "$api/v1/admin/reviews").items
```

`subject_type` and `subject_id` name the withdrawal or the deposit. `user_id` is whose
movement it is. Read the subject with SQL (`withdrawals` or `deposits` by id) and decide
according to your compliance procedure, which is outside this system.

**Decide.** A review is decided once; a second decision gets `409 review_already_resolved`.

| Decision | Withdrawal | Deposit |
|---|---|---|
| `POST /v1/admin/reviews/{id}/clear` | Sent to the provider | Credited to the user on the review. Refused if the review has no user (`409 review_has_no_user`) or the user's account is closed (`409 deposit_owner_closed`) |
| `POST /v1/admin/reviews/{id}/reject` | Funds returned to the user; the withdrawal ends as `failed` with `review_rejected` | Stays in suspense. Send it back as described under [Funds in suspense](#funds-in-suspense) |

A deposit for a closed account cannot be cleared: a closed account receives nothing. Reject
its review and send the deposit back as described under
[Funds in suspense](#funds-in-suspense).

A review can outlive its subject. If the user cancels a held withdrawal before the review
is decided, the review stays open, and clearing or rejecting it changes no money. If a
deposit left suspense some other way (the bank took it back, or an adjustment released or
returned it), clearing its review is refused with `409 deposit_not_in_suspense`. Reject
the review to take it off the queue; rejecting a deposit's review moves nothing.

## Funds in suspense

**What it means.** The suspense account of an asset holds deposits that could not be
credited: a deposit to an account or address Corridor did not issue, or a deposit held for
review. No user sees them.

**Diagnose.** List them, newest first. Each has its id, provider, asset, amount, when it
was received and, if screening opened a review on it, the review's id.

```powershell
(Invoke-RestMethod -Headers $admin -Uri "$api/v1/admin/deposits/suspense").items
```

A deposit with a `review_id` is, or was, in [the review queue](#the-review-queue): the
open reviews there say which user it arrived for. One without arrived at an account or
address Corridor never issued. What has happened to a deposit so far is in the audit log
under its id (`GET /v1/admin/audit?subject=<deposit id>`).

The list does not show the provider's id for the deposit or who sent it. Those are read
with SQL; the sender's name and reference are in the stored webhook for 30 days:

```sql
SELECT provider, provider_ref FROM deposits WHERE id = '<deposit id>';

SELECT received_at, payload -> 'data' AS data
  FROM webhook_events
 WHERE type IN ('deposit.received', 'deposit.confirmed')
   AND payload -> 'data' ->> 'deposit_id' = '<provider_ref>';
```

**What to do.** There are three ways out of suspense, and each deposit can take only one.

| Situation | Action |
|---|---|
| It has an open review with a user, and it should be credited | Clear the review |
| It belongs to a user (no review, or a review with no user) | `POST /v1/admin/adjustments/suspense-release` with a reason, the deposit's id (`deposit_id`) and the user (`user_id`). The asset and the amount are the deposit's own and are not sent. A second administrator approves it. Reject the deposit's open review afterwards, if it has one |
| It should go back to the sender | `POST /v1/admin/adjustments/suspense-return` with a reason and the deposit's id. A second administrator approves it. Then have the provider send the money back |

```powershell
$key = @{ "Idempotency-Key" = [guid]::NewGuid().ToString() }
$body = @{ reason = "Sender confirmed the payee"; deposit_id = "<deposit id>"; user_id = "<user id>" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Headers ($admin + $key) -Uri "$api/v1/admin/adjustments/suspense-release" -ContentType "application/json" -Body $body
```

An adjustment written by hand (`POST /v1/admin/adjustments`) cannot be used for this: one
with a leg that debits a suspense account is refused with `422 invalid_adjustment`. So is
one with a leg that debits a user's held account: what is on hold leaves as its
withdrawal is settled, canceled or released.

A suspense adjustment is refused when it is asked for if the deposit is not in suspense
(`409 deposit_not_in_suspense`) or, for a release, if the user's account is closed
(`409 deposit_owner_closed`). When it is approved, Corridor locks the deposit and checks
both again. If the deposit was released, returned or taken back by the bank in the
meantime, the approval is refused with the same codes, nothing is posted and the
adjustment stays `pending`. Reject the adjustment and look at the deposit again.

After a suspense return is approved, reconciliation reports a `settlement_balance` break
for that provider and asset until the provider's statement shows the money leaving. That
is expected. Resolve the break once it does.

If the bank takes back a deposit that is in suspense, Corridor books the return against
suspense itself and nothing needs doing.

## A restricted user

**What it means.** The user's status is `restricted`: they can log in, read and receive
money, and cannot transfer, convert or withdraw. There are two ways a user gets there.
Corridor restricts a user itself when a bank takes back a deposit after the user had spent
some of it; the shortfall is then recorded in the user's `user_receivable` account. And an
administrator restricts a user on purpose, with a reason.

**Diagnose.** Who restricted the user, when and why is in the audit log: `user.restricted`
for an administrator's restriction, `deposit.returned` with a `shortfall` for Corridor's
own.

```powershell
(Invoke-RestMethod -Headers $admin -Uri "$api/v1/admin/audit?subject=<user id>&action=user.").items
(Invoke-RestMethod -Headers $admin -Uri "$api/v1/admin/audit?subject=<user id>&action=deposit.returned").items
```

The reason on the account, and what is owed:

```sql
SELECT id, email, status, restricted_reason FROM users WHERE id = '<user id>';

SELECT a.asset_code, b.balance AS owed
  FROM ledger_accounts a JOIN account_balances b ON b.account_id = a.id
 WHERE a.kind = 'user_receivable' AND a.owner_id = '<user id>';
```

**What to do.** Deposits still reach a restricted user's available balance. When the user
has enough to cover what is owed, settle it with an adjustment that debits the user's
`user_available` account and credits their `user_receivable` account for the amount owed.
Find the two account ids in `ledger_accounts` by `owner_id`, `asset_code` and `kind`.

**Restrict and lift.** Both need a reason of 1 to 500 characters, and both are audited
with it. Restricting waits for a movement of the user's that is under way and stops every
one after it; restricting a user who is restricted already replaces the reason.

```powershell
$body = @{ reason = "Chargeback under investigation" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Headers $admin -Uri "$api/v1/admin/users/<user id>/restrict" -ContentType "application/json" -Body $body

$body = @{ reason = "Shortfall settled by adjustment <adjustment id>" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Headers $admin -Uri "$api/v1/admin/users/<user id>/lift-restriction" -ContentType "application/json" -Body $body
```

Lifting does not look at what the user owes: settle a shortfall first. An administrator
cannot restrict their own account or lift their own restriction (`409 own_account`), and a
closed account is refused (`409 conflict`). Neither endpoint ends the user's sessions.

## Rotating secrets and keys

Settings are read when a process starts. Every rotation below ends with restarting the API
and the worker with the new values.

### Webhook secrets

**Supported: rotation with no dropped deliveries.** `CORRIDOR_BANK_RAIL_WEBHOOK_SECRETS` and
`CORRIDOR_CUSTODY_WEBHOOK_SECRETS` are lists, and a delivery is accepted if any secret in
the list verifies it.

1. Add the new secret to the list, keeping the old one. Restart the API.
2. Have the provider start signing with the new secret.
3. Remove the old secret. Restart the API.

Each secret is at least 32 characters. The two providers must never share a secret: the
API refuses to start if a secret is in both lists.

### The access-token signing key

**Supported: rotation with no forced logouts.** The key id is derived from the key, so
there is nothing to keep in step.

1. Generate a key pair. The command prints the two settings that use it.

   ```powershell
   uv run corridor keys generate --out .local/keys
   ```

   The private key is written readable by its owner alone (mode 600). `--mode 644` writes
   one that other users of the machine can read, which is what the local compose stack
   needs on a Linux host, where the API container runs as user id 10001 and reads the key
   through a bind mount. Do not use it for a key that protects anything.

2. With more than one API instance, add the **new** public key to
   `CORRIDOR_JWT_ADDITIONAL_PUBLIC_KEYS` on every instance and restart them, before any
   instance signs with it. No instance then meets a token it cannot verify. With one
   instance, skip this step.
3. Set `CORRIDOR_JWT_SIGNING_KEY_FILE` (or `CORRIDOR_JWT_SIGNING_KEY`) to the new private
   key, and put the **old** key's public PEM in `CORRIDOR_JWT_ADDITIONAL_PUBLIC_KEYS`.
   Restart the API. New tokens are signed with the new key; tokens signed with the old key
   still verify.
4. After 15 minutes (the access-token lifetime) remove the old public key.

Refresh tokens are not signed and are not affected. To end every token of one user at
once, close the account or change its role; there is no endpoint that ends all tokens of
all users. If the old private key was stolen, skip keeping its public key: every access
token then fails and clients refresh.

### The API-key hash key

**Not supported: rotation.** `CORRIDOR_API_KEY_HASH_KEY` is a single key. An agent key is
stored only as its HMAC under that key, so changing it makes every stored hash
unverifiable: **every agent key stops working at once**, and there is no way to convert
them. If the key must change:

1. Tell agent owners that keys will have to be issued again.
2. Change the setting and restart the API.
3. Owners issue new keys (`POST /v1/agents/{agent_id}/keys`) and revoke the old ones, which
   no longer work anyway.

Agents, policies and approval requests are not affected.

### The FX cache key

**Supported, trivially.** `CORRIDOR_FX_CACHE_MAC_KEY` authenticates rates cached in Redis
for 5 seconds. Change it and restart the API. Entries written under the old key fail
verification and are fetched again. With several instances, they disagree for the length
of the rolling restart, which costs extra calls to the rate source and nothing else.

### Provider API keys

`CORRIDOR_BANK_RAIL_API_KEY`, `CORRIDOR_CUSTODY_API_KEY` and `CORRIDOR_FX_RATES_API_KEY` are
single values. Have the provider accept both keys for the changeover if it can; otherwise
calls between the provider's switch and the restart are refused with `401`, are counted as
`misconfigured`, and are retried once the new key is in place.

### Database passwords and the Redis token

Change the role's password in PostgreSQL, update the connection URL, restart. The owner
URL is used only by `corridor db migrate`. For the deployed stack, the
[infrastructure README](../infra/README.md#rotating-a-secret) has the steps for Secrets
Manager and for the cache's token.

## Restoring from a backup

**Prefer a full restore.** A physical restore (an RDS snapshot or point-in-time restore)
or a full logical restore into a new, empty database brings back the schema, the triggers,
the grants and the data together, and needs no special handling.

```powershell
pg_dump --format=custom --file corridor.dump "<connection URL of a superuser or the owner>"

psql "<admin connection URL>" -c "CREATE DATABASE corridor_restored OWNER corridor_owner"
pg_restore --exit-on-error --dbname "<connection URL to corridor_restored>" corridor.dump
```

The roles `corridor_owner` and `corridor_app` must exist on the target server before the
restore, because the dump refers to them.

**One step after any restore into a new database.** The right to create temporary tables is
a privilege on the database itself, not on anything in it, so a dump does not carry its
removal. Take it away again:

```sql
REVOKE TEMPORARY ON DATABASE corridor_restored FROM PUBLIC;
REVOKE TEMPORARY ON DATABASE corridor_restored FROM corridor_app;
```

**Then verify.** Point `CORRIDOR_DATABASE_URL` at the restored database and run
`corridor verify-ledger`. It must report no findings before the application is started
against it.

**After a restore to an earlier point in time**, the providers know about movements the
database has forgotten. Start the worker and let reconciliation run. It credits deposits
that are on the provider's statement and missing from the books, and applies payout
results for withdrawals that are in flight again. Anything older than its window (up to 7
days from the last completed run) has to be compared by hand. Idempotency keys issued
after the restore point are gone, so a client retry of a request from that period is a new
request.

### The caveat for data-only restores

A data-only restore (`pg_restore --data-only`) into a database that was created by
`corridor db migrate` **fails by default**, for three reasons:

1. **The sealed-postings trigger.** `postings_entry_unsealed` accepts a posting only from
   the transaction that wrote its journal entry. A restore loads `journal_entries` and
   `postings` as separate statements, usually in separate transactions, so every posting is
   refused with `journal entry … was not written by this transaction`. The balance trigger
   likewise refuses the entries, which arrive before their postings.
2. **Foreign keys.** The tables are loaded in an order that does not respect them.
3. **Seeded rows.** The migrations insert rows into `assets`, `risk_limits` and
   `risk_reference_rates`. The dump contains the same rows, and they collide.

The procedure that works needs a superuser, because it switches triggers off for the
length of the restore:

```powershell
# 1. In the migrated target, as the owner, remove the rows the migrations seeded.
psql "<owner connection URL>" -c "DELETE FROM risk_limits; DELETE FROM risk_reference_rates; DELETE FROM assets;"

# 2. List the dump's contents, and keep the data and sequence lines except alembic_version.
pg_restore --list corridor.dump | Select-String "TABLE DATA|SEQUENCE SET" | Where-Object { $_ -notmatch "alembic_version" } | ForEach-Object { $_.Line } | Set-Content -Encoding ascii restore.list

# 3. Restore the data with triggers disabled, as a superuser.
pg_restore --data-only --disable-triggers --use-list restore.list --dbname "<superuser connection URL>" corridor.dump
```

Then run `corridor verify-ledger`. With the triggers off, nothing checked the data as it
was loaded; the verifier is the check. The dump must come from the same migration revision
as the target.

This procedure was tried on PostgreSQL 16.15 with a small ledger: the restore succeeded,
the verifier reported nothing, and the `postings.seq` sequence continued after the highest
restored value. It was run with the Unix forms of the commands, not in PowerShell. It
cannot be used on a managed service that gives no superuser, such as RDS. Use a snapshot or
point-in-time restore there.
