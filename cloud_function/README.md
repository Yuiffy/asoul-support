# Huawei FunctionGraph adapter — queue-v3

An asoul-support based Python cloud function with a full medal-room queue.

## Deployment (four-hour schedule retained)

Set Timer cron `0 0 */4 * * ?` (every four hours), timeout **10900 seconds**,
128MB memory, maximum instances1, request concurrency1, and no reserved instances.
Default run budget10800seconds; the handler also honors the actual cloud timeout.
Four-hour run reservations suppress duplicate invocations. The old half-hour
trigger must be replaced if upgrading from the original five-room version.

Two accounts run independently. Each enumerates its entire medal list, Sui first.
Interaction work is serial within an account: one round for each room before up
to nine further passes over remaining tasks. Sui watching runs alongside this
sweep. Other live rooms then queue for up to16minutes watching each. At most two
watch sessions per account run concurrently. Overall timeout, midnight or account
risk control stops work, and uninspected/incomplete room counts appear in the
final report. Completing a scan does not mean every room can reach daily caps.
Subsequent executions read Bilibili's persisted progress and skip completed tasks.
No local filesystem cursor is relied on.

All account workers share a serialized request gate (minimum1.5seconds between
requests, minimum30seconds between danmaku). Other rooms receive danmaku only
while offline. `-352`/`-412` stops the account for this execution; `-101` likewise
stops an expired login. `10030` has no confirmed general meaning in the available
evidence: disable its failing endpoint for the execution and report its endpoint,
code and sanitized server message. Never bypass challenges or automatically retry
POSTs. Gift daily restrictions remain unchanged.

Cost:186scheduled calls per31day month. At128MB, even10800seconds on every call
uses251100GB-seconds, within the400000GB-second free tier **before other functions**.
Actual work can exit sooner; other account workloads and OBS are billed separately.
The final WeCom summary is deduplicated per four-hour run and per robot.

## Daily remaining queue (queue-v3)

At the first execution of each Beijing calendar day, each account snapshots its
eligible medal-room roster. Every verified room completion is immediately appended
to an OBS journal under `runs/daily/<account>/<date>-<policy-hash>.jsonl`. Later
executions read that journal once, skip the completed rooms without any Bilibili
requests, and resume the remaining queue. Sui stays first; previously unvisited
rooms precede rooms already inspected but still incomplete. If the entire account
queue is complete, no Bilibili login or API request is made for that account.

Only all three free tasks at their verified daily caps count as complete. The paid
primary Sui target additionally requires `feedLight` at1/1. Offline, storage-full,
unknown-progress, rejected and failed rooms remain pending. Existing gift reservations
are separate and are never cleared by the daily queue. Next day starts a fresh roster.
New medals acquired mid-day are discovered the next day. Changes to account scope,
blacklist or paid-gift policy start a new matching roster; changing only the cookie
keeps today's progress. No cookies/webhook keys are stored in the journal.

Journal appends use the current byte position. A stale writer or uncertain append
stops the queue; a fresh invocation reloads and reconciles persisted records. A
corrupt journal or403 permission error is not treated as an empty roster. A worker
that exits unexpectedly retains all previous successful checkpoints. The first
queue-v3 run cannot reconstruct old queue-v2 completion records, so it performs
one full scan to establish today's state.

**Additional required permission before uploading queue-v3:** allow
`obs:object:GetObject` on **only** the dedicated bucket's `runs/daily/*` prefix.
Existing PutObject permissions for `runs/*` and `gifts/*` remain as-is; no ListBucket
or DeleteObject is required. See `iam-policy.example.json`. Keep code/public config
separate from real credentials.

Test `{"mode":"progress_check"}` to append and read back a synthetic progress record
without touching Bilibili, then `{"mode":"progress_state"}` to inspect actual cached
counts without making Bilibili requests. The latter returns uninitialized until
an actual queue-v3 run creates today's roster.

## Configuration and secrets

Source and deployment ZIP contain **no account credentials**. Put the following in
FunctionGraph environment variables. Use encrypted variable storage when available.
Do not put real cookies into the repository or test events.

| Variable | Meaning |
| --- | --- |
| `BILIBILI_ACCOUNTS_JSON` | JSON array matching `accounts.example.json`; exactly one primary and optionally a secondary account |
| `ENABLE_ACTIONS` | `true` to execute, otherwise read-only |
| `ENABLE_PAID_GIFT` | `true` to allow the primary account's daily gift; default off |
| `PAID_ACCOUNT_UID` | Must match the primary account's UID, in addition to its `allow_paid: true` |
| `OBS_BUCKET` | Private standard OBS bucket in cn-south-1 for execution reservations |
| `WECOM_WEBHOOK_URL` | Optional WeCom group robot webhook; store as an AES encrypted variable |

Secondary accounts **cannot** send paid gifts even if their `allow_paid` is accidentally
enabled. No other streamer can receive paid gifts. Missing/expired cookies or a UID
mismatch stop the affected account. Renew a cookie when the Bilibili login expires.

## Deployment

1. Create a Python3.10/3.12 event function in cn-south-1; entry `index.handler`.
2. ZIP `index.py`, `asoul_x25kn.py`, `THIRD_PARTY.md` at ZIP root; no dependencies.
3. Use 128 MB memory, 10900 seconds timeout, one concurrent instance and one
   concurrent request per instance. Keep reserved instances at zero.
4. Use a private standard OBS bucket with versioning off. Grant the function
   execution agency `obs:object:PutObject` for this bucket's `runs/*` and
   `gifts/*` prefixes. AppendObject at `position=0` is the atomic reservation.
   Add GetObject only for `runs/daily/*`; no List/Delete permissions or permanent cloud access keys are needed.
5. Create a Timer trigger every four hours. Start with actions disabled, test
   `{"mode":"health"}`, then `{"mode":"inspect"}` with account credentials.
6. Enable free actions after inspection. Enable the paid switch only after
   confirming the account, gift and daily spending limit.

The function ends before the next four-hour run budget. The server stores task
progress across invocations. Watch time cannot be earned while the streamer is
offline. If they stream too briefly, or Bilibili rejects cloud requests, daily
completion is not guaranteed. The function never bypasses risk-control challenges.

OBS stores only account/room identifiers, a day or slot, and reservation metadata,
not cookies. Records are immutable: a failed or ambiguous paid request is not retried
that day. A retention rule of 30 days is sufficient; do not delete today's records.
Each enabled room creates at most 48 small run records per day, plus one gift record. Daily journals add a small append per changed checkpoint and one GET per account/run.
## WeCom notifications

Each real execution sends one final aggregate report across all processed rooms,
separated by account: covered rooms, total medal-room count when available,
verified daily free-task completion, confirmed progress, storage-full skips,
duplicate skips, errors, and accepted watch-heartbeat seconds. Sui's exact task
progress and lamp result follow the totals, with cumulative daily completions, cache skips and remaining-queue counts. Inspection-only calls send no summary.
A durable OBS reservation permits only one report attempt per four-hour slot and
robot destination; uncertain HTTP outcomes are not blindly retried.
Only `qyapi.weixin.qq.com/cgi-bin/webhook/send` HTTPS URLs are accepted. Webhook keys
and cookies are never included in messages or logs. OBS downtime can also prevent
notification deduplication, so inspect the cloud execution record if no alert arrives.

Test events: `{"mode":"ledger_check"}` verifies a first append succeeds and a
duplicate is rejected; `{"mode":"notify_test"}` sends one setup test per day.
`{"max_seconds":150}` performs a bounded real run after action switches are enabled. It consumes the same four-hour run reservation; use inspection for a smoke check.
These events contain no credentials and cannot change account or spending limits.

## Paid-gift boundaries

The primary account can send **one** gift, only to room `25788785`, owner `1954091502`,
gift ID `31164`, with verified unit price at most 100 gold seeds (= 1 battery).
The current `feedLight` task must explicitly be `0/1` and incomplete. The function
reserves the date durably before sending, then rechecks the Bilibili task. The
reservation prevents duplicate payments on retries/cold starts/concurrent calls.
No automatic recharge, other gifts, video coins or subscriptions are implemented.

Bilibili does not expose an atomic "send only if no manual gift exists" operation.
A manual gift in the tiny interval after the last check can still race the bot.
Avoid sending manually at the scheduled execution boundary. A payment whose
response is lost is logged as uncertain, and the reservation blocks another attempt.

Free-task progress is checked after each round. If progress fails to increase,
that action stops for this invocation. Full free-intimacy storage is reported and
free tasks are skipped; a free-only secondary account never buys a gift to clear it.

## Verification

Run `python -m unittest discover -s cloud_function -p 'test_*.py'` from the fork.
Tests include the upstream HMAC vector, already-gifted days, unexpected prices,
concurrent runs, uncertain requests, day rollover, and absent account credentials.
