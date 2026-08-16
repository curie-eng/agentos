---
seam: Channel / ingress (Slack)
kind: SOFT
impls: 1
grade: C
vision_row: Communication
epics:
  - "#7"
  - "#19"
  - "#27"
  - "#38"
order: 4
---
# INTERFACE: Channel / ingress (Slack)

> Part of the Curie swappable-seam catalog — see the [seam index](../../interfaces.md).
<!-- BEGIN GENERATED: header (curie dev docs-lint) -->
> **Kind:** SOFT &nbsp;·&nbsp; **Implementations today:** 1 &nbsp;·&nbsp; **Swap-readiness grade:** C
<!-- END GENERATED: header -->

**Kind legend:** CLEAN = a real `Protocol`/typed port class · SOFT = swap via env/URL/prefix/wire, no code interface · NONE = not built yet.

## The black line

The line that makes the communication channel swappable is the pair of contracts at
the two ends of the run: the ingress payload the dispatcher enqueues (`QueuedTurn`) and
the egress port the kernel writes replies through (`SlackSink`). Everything between them —
routing, concurrency, sandboxing — is opinionated core and channel-agnostic. Since #7 and
#19 the ingress payload and the per-turn reply routing are channel-neutral, so this is no
longer the least-clean seam by its wire contract; the remaining vendor shape is on the
egress semantics (edit-in-place streaming, plus posting and settling platform-owned cards)
and on the Slack-typed binding surface. One implementation
today; the port is the wire + Protocol contract, extracted further only when a second
channel demands it ("the second implementation teaches the interface").

## Current contract

A second channel must produce the ingress payload and satisfy the egress Protocol:

- **Ingress** — `QueuedTurn` (`packages/aci-protocol/src/aci_protocol/turn.py::QueuedTurn`),
  a Pydantic model in the frozen ACI package with channel-neutral fields: `event_id`
  (idempotency key), `conversation_id` (the conversation/thread key routing keeps one live
  session per), `author`, `text`, `received_at`, and `reply_handle` — a `ReplyHandle`
  (`packages/aci-protocol/src/aci_protocol/turn.py::ReplyHandle`) carrying `channel`,
  `placeholder` (the pre-posted reply the worker edits in place), and an optional per-turn
  `endpoint`. For the Slack adapter, `event_id` is the Slack event id, `conversation_id` is
  the thread ts, `author` is the Slack user id, and `reply_handle` carries the Slack channel
  plus the placeholder ts.
  The dispatcher serializes the turn to a single Stream field via `to_stream_fields`
  (`apps/dispatcher/src/curie_dispatcher/queue.py::to_stream_fields`), keyed by
  `STREAM_PAYLOAD_FIELD = "payload"`. That key is not the dispatcher's: it is frozen-package
  contract in
  `packages/aci-protocol/src/aci_protocol/service_config.py::STREAM_PAYLOAD_FIELD`, imported
  by every producer and consumer of the stream (the dispatcher, the API resume queue in
  `apps/api/src/curie_api/resumequeue.py`, and the Rust CLI through the generated constant in
  `cli/src/queue.rs`), so a second ingress adopts the package constant rather than copying
  the literal.
- **Egress** — the `SlackSink` Protocol (`apps/worker/src/curie_worker/slack_sink.py::SlackSink`)
  carries five methods, and a second channel implements all five:
  - `update` (`apps/worker/src/curie_worker/slack_sink.py::SlackSink.update`), the streamed
    reply, is `async def update(self, *, channel: str, ts: str, text: str, nav: NavPack | None
    = None, endpoint: str | None = None, best_effort_unreachable: bool = False) -> None`: an
    edit-in-place of the ingress placeholder, carrying the agent's hub-button pack (`nav`),
    the per-turn reply target (`endpoint`, #19), and the offline resume allowance
    (`best_effort_unreachable`, #708).
  - `post` (`apps/worker/src/curie_worker/slack_sink.py::SlackSink.post`) posts a NEW
    platform-owned message and returns its id, and `update_message`
    (`apps/worker/src/curie_worker/slack_sink.py::SlackSink.update_message`) edits an
    already-posted one into its settled form. Both take a channel-neutral `OutboundMessage`
    (`packages/channel-protocol/src/channel_protocol/models.py::OutboundMessage`, ADR-0020)
    that the adapter renders below the seam, so no channel-native markup crosses here;
    `update_message` also takes a `SettledCard`
    (`apps/worker/src/curie_worker/slack_sink.py::SettledCard`) carrying the outcome
    semantically (who asked, what was decided, by whom, with what note).
  - `set_status`/`clear_status`
    (`apps/worker/src/curie_worker/slack_sink.py::SlackSink.set_status`,
    `apps/worker/src/curie_worker/slack_sink.py::SlackSink.clear_status`) are the
    best-effort thread status caption, a no-op on a channel with no equivalent.

  Slack dialect and widget shape stay below the seam: `to_mrkdwn`
  (`apps/worker/src/curie_worker/mrkdwn.py::to_mrkdwn`) and the Block Kit rendering in
  `render` (`apps/worker/src/curie_worker/blocks.py::render`) and `approval_card`
  (`apps/worker/src/curie_worker/blocks.py::approval_card`).
- **Binding** — a channel resolves to a deployment by `agents.slack_channel`
  equality in `BindingResolver.resolve` (`apps/worker/src/curie_worker/binding.py::BindingResolver.resolve`).

## Implementations today

One: Slack. Ingress is `apps/dispatcher` (Bolt / Socket Mode); egress is
`AsyncSlackSink` (`apps/worker/src/curie_worker/slack_sink.py::AsyncSlackSink`) on the Slack Web API. The swap proof that the
protocol (not just the service) is the seam: the Rust CLI mints the exact
`QueuedTurn` wire payload with the same channel-neutral fields
(`cli/src/queue.rs`) and drives the whole deployed system with zero Slack contact
via `curie local message` / `cluster message` (`cli/src/chat.rs`, `cli/src/message.rs`).

## Known leakage

Two ends were cleaned and one Slack surface is newly documented.

- **Fixed (#7).** The ingress field names were Slack's (`slack_event_id`, `thread_ts`,
  `placeholder_ts`); the payload was promoted into `packages/aci-protocol` as `QueuedTurn`
  with channel-neutral names.
- **Fixed (#19).** The reply base URL was worker-global; per-turn reply routing now rides
  `ReplyHandle.endpoint`, so a real Slack workspace and a no-Slack CLI stub can coexist on
  one deployment. `WorkerConfig.slack_api_base_url` (`apps/worker/src/curie_worker/config.py::WorkerConfig`)
  is now only the default when a turn sets no `endpoint`, fed to `AsyncSlackSink`
  (`apps/worker/src/curie_worker/slack_sink.py::AsyncSlackSink.__init__`).
- **Still leaks — egress semantics.** The streamed reply is edit-a-placeholder: `update`
  runs on Slack's `chat.update` against the message the ingress already posted
  (`apps/worker/src/curie_worker/slack_sink.py::AsyncSlackSink.update`), so a channel with no
  in-place edit must emulate it. That is no longer the whole egress model, though: the sink
  also really posts, via `chat.postMessage` in
  `apps/worker/src/curie_worker/slack_sink.py::AsyncSlackSink.post`, for platform-owned
  messages such as the approval card, and settles that card in place afterwards in
  `apps/worker/src/curie_worker/slack_sink.py::AsyncSlackSink.update_message` (expired in
  #419, resolved in #1084). A second channel therefore has to support three shapes, not one:
  repeatedly editing one streamed message, posting an interactive card and returning its id,
  and editing that card into a settled, non-interactive form.
- **Still leaks — the Slack-typed binding surface, undocumented until now.** The agents table
  carries a `slack_channel` column (`apps/api/src/curie_api/models.py::Agent`), and agent
  create/update validate it as a Slack channel id via `_validate_slack_channel_id`
  (`apps/api/src/curie_api/schemas.py::_validate_slack_channel_id`) wired onto
  `apps/api/src/curie_api/schemas.py::AgentCreate` and
  `apps/api/src/curie_api/schemas.py::AgentUpdate`. This is the largest remaining Slack
  surface and appears in no other seam doc: the binding key and its validators are
  Slack-shaped in the control plane, not just at the channel edges. The restraint is
  deliberate: no multi-channel adapter framework is built (#27) — the channel-neutral
  binding rename comes with the second real channel.

## Cross-links

- **Epic(s):** #7 — promote the queue payload into `packages/aci-protocol` with
  channel-neutral field names (landed)
- **Epic(s):** #19 — per-turn reply routing (landed)
- **Epic(s):** #27 — deliberately defers a pluggable multi-channel framework
- **Epic(s):** #38 — channel-seam hardening / follow-up
- **Vision doc:** [architecture-vision.md](../../architecture-vision.md) — Job 6 (Communication channel), grade C
- **ADR(s):** none directly on this seam
- **Interaction contract:** [Channel interaction](../channel-interaction/INTERFACE.md)
  defines the semantic reply before this Slack adapter renders it.
