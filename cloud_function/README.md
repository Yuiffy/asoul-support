# Huawei FunctionGraph adapter — queue-v4

An asoul-support based Python cloud function with a full medal-room queue.

## Deployment (two-hour schedule)

Set Timer cron `0 0 */2 * * ?` (every two hours), timeout **6900 seconds**,
128MB memory, maximum instances1, request concurrency1, and no reserved instances.
Default and maximum run budget **6600 seconds (110 minutes)**; the handler also
honors the actual cloud timeout. Keep only one active Timer and stop the old
half-hour/four-hour triggers, including the previous `bili-fan-paizi-1` timer.
`runs/queue-v4/<slot>.json` reservations fence every two-hour window an invocation
could occupy until its cloud timeout, before any account task starts. Manual
starts near a boundary can therefore suppress the next scheduled execution. An
uncertain reservation fails closed. Maximum instances1 and request concurrency1
must also be verified in FunctionGraph; health reports the requirement, not the
actual cloud setting. Reservation records are never deleted to force a retry.

For the current primary-only rollout, set `ACTIVE_ACCOUNT_UIDS=14279` and
`SUI_ONLY=false`: 带鱼 processes the full configured medal list; 鹿饼 remains excluded.
Verify health has only UID14279 and `other_medals:true`. Every active account uses
Sui first even when resuming a daily queue. Sui gets its available interaction
rounds first. Remaining rooms are classified with a per-run batch GET (up to50
streamer UIDs per request), so live rooms get their available like rounds before
offline rooms get danmaku. Replay/unknown rooms are last. Ordering snapshots never
authorize an action: likes, watch and especially offline-only danmaku still check
current room state. Completed rooms are absent from the batch query. Then a single
watch worker runs alongside the remaining room scan
and interaction sweeps, sharing the same account-wide request/pacing gate. Watch
sessions run one at a time, with Sui's session queued first. Only the main queue
thread appends daily checkpoints; watch results merge without decreasing already
confirmed interaction progress. Queued sessions skip login after risk or deadline.
This keeps a long Sui watch from blocking all other room interactions. Offline-only
danmaku still applies to every room. Offline tasks stay pending. Overall timeout,
midnight or risk stops work; completing a scan does not imply daily caps are met.

Pacing follows the inspected BLTH MedalModule/likeTask implementation for likes:
a full API-specified round (normally30 clicks) is sent in **one** request, with
**15–20seconds between like requests**, shared across all rooms for the account.
The old10-click requests separated by3seconds are removed. Queries retain a slower
**1.5–2second** shared gap than BLTH's300–800ms query delays. Danmaku retain a slower
**30–40second** gap than BLTH's6–8seconds, and are sent only with an explicit offline
status. Heartbeats retain the Bilibili server's interval. Cooldown waits release
the account network lock. WBI signatures and danmaku timestamps are generated
after all pacing waits, not before. These intervals are not an official guarantee
against platform risk checks. No deliberate live risk-reproduction POSTs are used
as deployment validation.

`-352`/`-412` stops the account for this execution; `-101` likewise stops an expired
login. `10030` disables the failing endpoint for the execution and reports its
endpoint, code and sanitized message. No challenge bypass or automatic POST retries
are used. Existing paid-gift limits and daily-cache policy remain in force. This
change does not add a persistent cross-run risk pause.

## Daily remaining queue (compatible queue-v3 journals)

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
corrupt journal is never treated as an empty roster. OBS returns403 for nonexistent
objects when ListBucket is absent. On403, the reader atomically creates only a
missing journal using a position-zero initialization marker, then requires a
successful GET. Existing journals cannot be overwritten; a persistent403 still
stops the queue. No ListBucket privilege is needed. A worker
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
| `ACTIVE_ACCOUNT_UIDS` | Optional comma-separated UID allowlist. Excluded accounts make no login, task, or progress-state requests. Leave empty to use all configured accounts. Unknown UIDs fail closed. |
| `SUI_ONLY` | `true` forces every active account to Sui's room only, overriding `other_medals` without editing encrypted account credentials. Uses a separate daily queue policy; existing gift reservations remain valid. Default `false`. |
| `ENABLE_PAID_GIFT` | `true` to allow the primary account's daily gift; default off |
| `PAID_ACCOUNT_UID` | Must match the primary account's UID, in addition to its `allow_paid: true` |
| `OBS_BUCKET` | Private standard OBS bucket in cn-south-1 for execution reservations |
| `WECOM_WEBHOOK_URL` | Optional WeCom group robot webhook; store as an AES encrypted variable |
| `ENABLE_COOKIE_REFRESH` | `true` enables daily refresh checks and encrypted credential persistence; default off |
| `CREDENTIAL_ENCRYPTION_KEY` | Fernet key; generate with `Fernet.generate_key()`, save in an encrypted cloud variable, never Git |

Secondary accounts **cannot** send paid gifts even if their `allow_paid` is accidentally
enabled. No other streamer can receive paid gifts. Missing/expired cookies or a UID
mismatch stop the affected account. Renew a cookie when the Bilibili login expires.

## Automatic Cookie refresh (queue-v5-refresh)

Configure each selected account with a matching `cookie` and `refresh_token`
(browser localStorage `ac_time_value`), then set `ENABLE_COOKIE_REFRESH=true` and
an encrypted `CREDENTIAL_ENCRYPTION_KEY`. The token and cookie must belong to the
same login session, not merely the same UID. A valid Cookie alone cannot create a
refresh token. QR login saves both in its ignored local credential file.

The first unfinished queue run each Beijing day verifies login, queries Bilibili's
refresh recommendation and rotates only when requested. A fully completed daily
queue still makes no Bilibili request. Subsequent runs use the saved Cookie and
skip repeat refresh checks that day; normal login verification still applies.
The refreshed credential is passed to both the queue and the watch worker.
Expired login, CAPTCHA/risk responses and missing tokens are reported rather than
solved or repeatedly retried. Refresh does not guarantee resolution of `-352`.

Credentials are authenticated-encrypted with Fernet (AES-128-CBC/HMAC-SHA256 from
`cryptography`) in `runs/daily/credentials/<uid>/<seed-hash>.jsonl`. This uses the
existing OBS GetObject/PutObject prefix grants, with no List/Delete or permanent
cloud keys. Encryption keys stay separate in encrypted FunctionGraph variables.
Only ciphertext is written to OBS; Cookie/token values never enter logs, returned
results, notifications or deployment ZIPs. The append byte position fences stale
writers. The journal's 2MB limit fails closed; it is not discarded or reset.
Keep maximum instances and request concurrency at1 for maintenance too.

A durable refresh-started record is saved before rotating. New Cookie, new token
and old token are encrypted and saved before confirming the old session's expiry.
The new Cookie's UID/login is verified before confirmation. Failed confirmation
resumes from the saved new credential without another refresh POST. Uncertain
rotation or persistence stops that account; it never blindly retries rotation.
If confirmation was accepted but its response was lost, a repeated confirmation
may be rejected; reimport a fresh matching Cookie/token pair in that case.
Replacing the configured seed pair creates a new journal namespace. Keep the key
stable; changing it cannot decrypt existing state. Export recovery credentials only
through private authorized channels; this adapter has no credential-export mode.

`{"mode":"credential_status"}` is read-only: it loads the newest encrypted
credential, checks login and refresh recommendation, and returns only status and
Cookie/token presence flags. It never initializes OBS objects. A missing-object
403 without ListBucket is reported as unknown, not silently treated as empty.
`{"mode":"credential_maintain"}` explicitly checks/refreshes selected credentials
and verifies encrypted persistence without intimacy tasks, gifts, notifications or
run-slot reservations. Run it only after verifying the `queue-v5-refresh` health
marker; this mode changes credential state and can expire the old cloud session.
It never changes the account scope or clears today's progress/gift reservations.

Install the pinned Linux x86_64 Python3.12 wheels from `requirements.txt` into an
ignored local directory before building, then pass `build.py --dependencies <dir>`.
The ZIP allowlists the three pinned packages and retains their licenses. Linux
runtime must support glibc2.28 or newer for the cryptography wheel; a cold-start
maintenance test is required before enabling scheduled refresh in production.

## Deployment

1. Create a Python3.10/3.12 event function in cn-south-1; entry `index.handler`.
2. Use `cloud_function/build.py` to package all adapter modules at ZIP root.
   Include the pinned Linux dependency directory when enabling Cookie refresh.
3. Use 128 MB memory, 6900 seconds timeout, one concurrent instance and one
   concurrent request per instance. Keep reserved instances at zero.
4. Use a private standard OBS bucket with versioning off. Grant the function
   execution agency `obs:object:PutObject` for this bucket's `runs/*` and
   `gifts/*` prefixes. AppendObject at `position=0` is the atomic reservation.
   Add GetObject only for `runs/daily/*`; no List/Delete permissions or permanent cloud access keys are needed.
5. Create a Timer trigger every two hours. Start with actions disabled, test
   `{"mode":"health"}`, then `{"mode":"inspect"}` with account credentials.
6. Enable free actions after inspection. Enable the paid switch only after
   confirming the account, gift and daily spending limit.

The function ends before the next two-hour scheduled run. The server stores task
progress across invocations. Watch time cannot be earned while the streamer is
offline. If they stream too briefly, or Bilibili rejects cloud requests, daily
completion is not guaranteed. The function never bypasses risk-control challenges.

OBS task journals store account/room identifiers, a day or slot, and reservation metadata;
credential journals store only encrypted credential records,
not cookies. Records are immutable: a failed or ambiguous paid request is not retried
that day. A retention rule of 30 days is sufficient; do not delete today's records.
Each enabled room creates at most 48 small run records per day, plus one gift record. Daily journals add a small append per changed checkpoint and one GET per account/run.
## WeCom notifications

Each real execution sends one final aggregate report across all processed rooms,
separated by account: covered rooms, total medal-room count when available,
verified daily free-task completion, confirmed progress, storage-full skips,
duplicate skips, errors, and accepted watch-heartbeat seconds. Sui's exact task
progress and lamp result follow the totals, with cumulative daily completions, cache skips and remaining-queue counts. Inspection-only calls send no summary.
A durable OBS reservation permits only one report attempt per two-hour slot and
robot destination; uncertain HTTP outcomes are not blindly retried.
Only `qyapi.weixin.qq.com/cgi-bin/webhook/send` HTTPS URLs are accepted. Webhook keys
and cookies are never included in messages or logs. OBS downtime can also prevent
notification deduplication, so inspect the cloud execution record if no alert arrives.

Test events: `{"mode":"ledger_check"}` verifies a first append succeeds and a
duplicate is rejected; `{"mode":"notify_test"}` sends one setup test per day.
`{"max_seconds":150}` performs a bounded real run after action switches are enabled.
It consumes the same occupied-window reservations as a scheduled run; use health
for a smoke check without accessing Bilibili. A short manual run near a boundary
can suppress the following scheduled run until its reserved slot ends.
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

`progress_diagnose` compares existing and missing-object reads using synthetic
records only. Reads of missing progress may create a small init marker; no real
account tasks or gift reservations are changed.

## Do not disturb live streams

Automatic danmaku requires live_status=0 for **every** room and account, including
Sui. Live, replay and unknown status skip danmaku and keep the task pending. The
status is checked again after the account cooldown before sending. Status lookup
failures never send. There is no live-room exception or option to bypass this rule.
Likes, watch heartbeats and the explicitly allowed daily lamp retain their rules.
A streamer can technically go live between the final status check and server
acceptance; Bilibili provides no atomic offline-only send operation.

## Unlit medals and completion reporting

An unlit medal can return only two tasks: `发弹幕10次` and `点赞30次`,
both with `sub_title=仅点亮` and boolean `is_done`. These are relighting
tasks, not daily `x/10` counters. Only this explicit schema with
`is_lighted=false` is supported; unfamiliar responses never authorize actions.
A live room receives one30-click relight request; an unconfirmed relight like
is not blindly repeated. Offline relighting can send up to10 messages, checking
room status before every send and re-reading task state after each one.
Relighting is allowed with full free-intimacy storage, but normal intimacy
actions still stop when storage is full. When confirmed lighting exposes the
normal counters, live likes and watch tasks resume under the existing pacing.

Notifications list confirmed relights, rooms whose API initially offered only
relighting, each individual daily task's completion, and the snapshot live-room
count. All-three-task completion remains a strict verified measure; it is not
the number of rooms scanned, lit, or temporarily ineligible while offline.
The health build marker is `queue-v4-credentials`; OBS admission and gift namespaces
remain unchanged, preserving existing reservations.
