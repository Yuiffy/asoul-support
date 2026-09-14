# Huawei FunctionGraph adapter

An asoul-support based Python 3.9+ cloud function for current Bilibili fans-club tasks.
It reads daily task progress, completes remaining likes/danmaku and uses X25Kn E/X
heartbeats while the streamer is live. Sui is the primary target. Other existing
medals are optional, rotated five at a time, with one free interaction round and
danmaku only while offline. Optional rooms are watched only when the account's
Sui watch task still needs time and Sui is live, keeping them from extending the
function into an all-day background worker. Optional WeCom notifications use your
own encrypted webhook configuration.

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
3. Use 128 MB memory, 1750 seconds timeout, one concurrent instance and one
   concurrent request per instance. Keep reserved instances at zero.
4. Use a private standard OBS bucket with versioning off. Grant the function
   execution agency only `obs:object:PutObject` for this bucket's `runs/*` and
   `gifts/*` prefixes. AppendObject at `position=0` is the atomic reservation.
   No Get/List/Delete permissions or permanent cloud access keys are needed.
5. Create a Timer trigger every 30 minutes. Start with actions disabled, test
   `{"mode":"health"}`, then `{"mode":"inspect"}` with account credentials.
6. Enable free actions after inspection. Enable the paid switch only after
   confirming the account, gift and daily spending limit.

The function ends before the next half-hour boundary. The server stores task
progress across invocations. Watch time cannot be earned while the streamer is
offline. If they stream too briefly, or Bilibili rejects cloud requests, daily
completion is not guaranteed. The function never bypasses risk-control challenges.

OBS stores only account/room identifiers, a day or slot, and reservation metadata,
not cookies. Records are immutable: a failed or ambiguous paid request is not retried
that day. A retention rule of 30 days is sufficient; do not delete today's records.
Each enabled room creates at most 48 small run records per day, plus one gift record.
With two accounts and five optional rooms each, this is at most 17,856 tiny run
records in a 31-day month, approximately 0.018 CNY in standard OBS PUT requests
at Guangzhou's published 0.01 CNY per 10,000 requests, plus negligible storage.

FunctionGraph's shared monthly free tier is 1,000,000 calls and 400,000 GB-seconds.
At 128 MB, 150 minutes per day for 31 days is 34,875 GB-seconds. Two accounts
run concurrently in one instance rather than doubling the memory allocation.
The timer makes 1,488 calls in 31 days. Other functions share this allowance;
actual billing depends on total account usage. OBS and paid Bilibili gifts are
separate from FunctionGraph's free tier. No reserved instances or LTS are needed.
Sources: [free tier](https://support.huaweicloud.com/price-functiongraph/functiongraph_00_0012.html),
[pricing](https://www.huaweicloud.com/pricing/calculator.html?tab=detail#/obs).

## WeCom notifications

Each real execution sends one final aggregate report across all processed rooms,
separated by account: covered rooms, total medal-room count when available,
verified daily free-task completion, confirmed progress, storage-full skips,
duplicate skips, errors, and accepted watch-heartbeat seconds. Sui's exact task
progress and lamp result follow the totals. Inspection-only calls send no summary.
A durable OBS reservation permits only one report attempt per half-hour slot and
robot destination; uncertain HTTP outcomes are not blindly retried.
Only `qyapi.weixin.qq.com/cgi-bin/webhook/send` HTTPS URLs are accepted. Webhook keys
and cookies are never included in messages or logs. OBS downtime can also prevent
notification deduplication, so inspect the cloud execution record if no alert arrives.

Test events: `{"mode":"ledger_check"}` verifies a first append succeeds and a
duplicate is rejected; `{"mode":"notify_test"}` sends one setup test per day.
`{"max_seconds":150}` performs a bounded real run after action switches are enabled.
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
