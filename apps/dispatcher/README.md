# apps/dispatcher

The Slack dispatcher: Slack Bolt for Python in Socket Mode.
On an `app_mention` (channel) or direct `message` (DM) for the bot it acks the
Socket Mode envelope fast, posts an in-thread placeholder reply, and enqueues a
normalized job onto a Valkey Stream keyed by the Slack event id (idempotent).
All of this runs under reconnect supervision with graceful shutdown.

It does exactly that and no more: routing, the finish-race, steer/interrupt, and
run orchestration are the worker's job, not the dispatcher's.

## The queue seam (what the worker consumes)

The dispatcher `XADD`s onto a Valkey Stream (`CURIE_STREAM`, default
`curie:runs`). Each entry carries one field, `payload`, holding the JSON of a
`QueuedTurn` (from `aci_protocol`). Its fields are channel-neutral; the
parenthetical is what the Slack adapter maps onto each one:

| field | meaning |
|---|---|
| `event_id` | idempotency key for the delivery (the Slack event id) |
| `conversation_id` | canonical thread/conversation key (the thread ts) |
| `author` | who authored the message (the Slack user id) |
| `text` | message text |
| `reply_handle` | where the reply is delivered: a `ReplyHandle` of `channel`, `placeholder` (ts of the already-posted placeholder the worker edits in place), and an optional per-turn `endpoint` |
| `received_at` | ISO-8601 UTC timestamp the adapter received it |

The worker reconstructs it with `from_stream_fields(fields)`, a module-level
helper in `curie_dispatcher.queue`. The model lives in the frozen `aci_protocol`
package (promoted out of the dispatcher in issue #7) so the producer and the
Rust/TS consumers share one schema-gated contract instead of a hand-mirrored copy;
the dispatcher's queue module owns only the Stream transport of it. The
single-`payload`-field encoding keeps the seam explicit and lets fields be added
without reshaping the Stream schema.

## Dedupe (idempotency)

Idempotency key = Slack event id (detailed-architecture 2b rule 5). A retried
Slack delivery must not enqueue twice. Before posting or enqueuing, the dispatcher
claims the event with a Valkey `SET <dedupe_prefix><event_id> 1 NX EX <ttl>`; the
first delivery wins and proceeds, a retry finds the key set and is dropped (still
acked, never re-posted, never re-enqueued). Chosen over stream-side dedupe because
it is O(1), TTL-bounded (no unbounded dedupe set to prune), and needs no Stream
scan. Order is claim -> post placeholder -> `XADD`, so a duplicate never produces
a second placeholder.

## Reconnect supervision and shutdown

`dispatcher.supervisor.Supervisor` drives a transport-agnostic `Connection`
(anything that blocks in `run` until the link drops and unblocks on `close`). The
builtin Slack client self-heals transient websocket drops; the supervisor is the
outer net for failures it cannot recover (the connection factory raising on
connect, an unrecoverable exit) and the owner of graceful shutdown. On a drop it
sleeps for an exponential, capped backoff (`BackoffPolicy`) and reconnects with a
fresh connection; `request_stop` (wired to SIGINT/SIGTERM) closes the current
connection and exits the loop without reconnecting. The Socket Mode adapter
(`app.SocketModeConnection`) is the thin production `Connection`.

## Config surface (env vars)

Read from the environment by `DispatcherConfig()` (a `pydantic_settings.BaseSettings`).

| env var | default | meaning |
|---|---|---|
| `SLACK_APP_TOKEN` | "" | app-level token (`xapp-...`), Socket Mode |
| `SLACK_BOT_TOKEN` | "" | bot token (`xoxb-...`), Web API |
| `SLACK_SIGNING_SECRET` | "" | optional; unused in Socket Mode, kept for Bolt App construction |
| `VALKEY_HOST` | `localhost` | Valkey host (in-cluster: `valkey`) |
| `VALKEY_PORT` | `6379` | Valkey port (compose maps it to `26379` on the host) |
| `VALKEY_PASSWORD` | "" | Valkey password (compose dev: `valkeypass`) |
| `VALKEY_DB` | `0` | Valkey db index |
| `CURIE_STREAM` | `curie:runs` | Stream the jobs land on |
| `CURIE_DEDUPE_PREFIX` | `curie:dedupe:` | dedupe key prefix |
| `CURIE_DEDUPE_TTL_SECONDS` | `3600` | dedupe guard TTL |
| `CURIE_PLACEHOLDER_TEXT` | `On it. Working on your request.` | placeholder reply text |
| `CURIE_BACKOFF_INITIAL_SECONDS` | `1.0` | first reconnect backoff |
| `CURIE_BACKOFF_MAX_SECONDS` | `30.0` | backoff cap |
| `CURIE_BACKOFF_MULTIPLIER` | `2.0` | backoff growth factor |
| `CURIE_API_URL` | `http://localhost:8000` | platform API used to resolve approval clicks (compose: `http://curie-api:8000`). `CURIE_API_BASE_URL` is a deprecated alias. |
| `CURIE_API_KEY` | `curie-dev-key` | shared API key sent as `X-API-Key` on the resolve call |
| `CURIE_API_PREFLIGHT_TIMEOUT_SECONDS` | `30.0` | deadline for the boot gate below; must be positive |

### Boot gate on the platform API

Before any Slack wiring, `main()` polls `GET {CURIE_API_URL}/health` until
it answers 200 or `CURIE_API_PREFLIGHT_TIMEOUT_SECONDS` elapses, reusing the
`CURIE_BACKOFF_*` tunables for the poll interval. On success it logs the
resolved URL once at INFO. On the deadline it logs an error naming that URL and
the time actually spent, and
exits non-zero, so a misconfigured base URL is dead on arrival instead of
dead-ending every Slack Approve click much later (the previous behavior was a
single warning at click time). It retries rather than probing once so a
slow-starting API does not fail a healthy stack; in Kubernetes the restart
backoff is the outer retry loop and `CrashLoopBackOff` is the operator signal.

The gate runs once at boot only. It is not a liveness monitor: an API restart
later does not kill the dispatcher (the heartbeat probes own liveness, and the
resolve call degrades per-call on its own). There is no off switch: the gate is
the point, so a non-positive timeout is rejected as a config error at boot.

**Known limit: the gate proves reachability, not credentials.** `/health` is
unauthenticated, so a wrong `CURIE_API_KEY` still passes the gate and fails at
click time. This check catches the base-URL class of misconfiguration only.

The two Slack tokens are the only secrets. When a workspace exists they come from
the app's install (App-Level Token with `connections:write` for `SLACK_APP_TOKEN`;
Bot User OAuth Token for `SLACK_BOT_TOKEN`), delivered as env vars (a K8s Secret
in the chart). Nothing else is secret.

## Run it

```bash
python -m curie_dispatcher
```

## Runbook: point it at a real Slack workspace (once one exists)

1. Create a Slack app. Fastest path: at <https://api.slack.com/apps> choose
   "From a manifest" and paste [`slack-app-manifest.yaml`](slack-app-manifest.yaml),
   which already sets Socket Mode on, the bot scopes (`app_mentions:read`,
   `chat:write`, `im:history`, `im:read`), and the `app_mention` + `message.im`
   event subscriptions. (To do it by hand, configure exactly those.)
2. Generate an App-Level Token with `connections:write` -> `SLACK_APP_TOKEN`
   (`xapp-...`); copy the Bot User OAuth Token -> `SLACK_BOT_TOKEN` (`xoxb-...`).
3. Set both env vars (plus `VALKEY_*` for the target Valkey) and run
   `python -m curie_dispatcher`. @-mention the bot in a channel it is in, or DM
   it: you should see the placeholder reply appear and one entry land on the
   Stream (`XLEN curie:runs`). The worker consumes from there.

## Verification (Slack-free)

All tests run without a Slack workspace: the Slack Web API client and socket
transport are the only things faked; Stream and dedupe assertions run against the
real Valkey from `compose.dev.yaml` (host port `26379`). From the repo root:

```bash
docker compose -f compose.dev.yaml up -d valkey
uv run pytest apps/dispatcher/tests -q
```

`tests/test_dispatch.py` drives Bolt's real `SocketModeHandler.handle` end to end
(envelope -> ack -> placeholder -> `XADD`), including the duplicate-delivery and
event-filtering cases; `tests/test_queue.py` covers the seam and dedupe against
real Valkey; `tests/test_supervisor.py` covers backoff, reconnect, and graceful
shutdown with a fake connection.
