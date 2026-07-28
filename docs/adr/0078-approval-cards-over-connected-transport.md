# 78. Route message-driven approval cards through the connected Slack transport

Date: 2026-07-23

Status: Accepted

**Mechanism superseded by [ADR-0082](0082-connected-transport-approval-cards-via-a-real-placeholder.md)**
(back-link added under [ADR-0045](0045-the-status-line-is-the-mutable-part-of-an-immutable-adr.md)):
the empty-`reply_handle.placeholder` sentinel this ADR's Decision specifies is
not implementable. 0082 carries this ADR's intent forward unchanged and states
the mechanism that replaces it, with the reasoning there.

## Context

ADR-0063 fixed the zero-Slack case of a `message`-driven approval: `curie
local/cluster message` keeps its in-process `SlackStub` alive across the
approval-pending window and delivers the resumed reply on the same placeholder
(#766/#772). That ADR's Decision was explicit that the stub is the reply
surface **"not (yet) the connected Slack transport"**, and its "Alternatives
considered #1" named routing through the connected transport as the eventual
durable, clickable-card surface — **"not rejected, only deferred" to Follow-up
A**. #770 is that follow-up.

The deferral existed because the connected-transport path is a coordinated
change across a sacred boundary, not a passenger on the offline fix:

- **The requesting-channel card rides the trigger's transport.** In
  `apps/worker/src/curie_worker/kernel.py` the `in_requesting_channel` branch
  posts the card over `qevent.reply_handle.endpoint` (the stub) when the card
  channel equals the requesting channel. This is a deliberate, test-pinned
  design from #451 (`apps/worker/tests/kernel/test_approval_lifecycle.py`,
  `test_pause_emits_a_confirm_intent_for_the_approval_card` and
  `test_routed_approval_cards_go_to_the_bound_channel`).
- **A late resumed reply cannot `chat.update` the stub's placeholder ts** in
  real Slack: that synthetic timestamp never existed there, so the resumed
  reply must post top-level instead of editing the placeholder.
- **`cluster message`'s `--force-wire` guard inverts.** Today it refuses to
  wire the deployed worker to the local stub when a `<release>-dispatcher`
  exists (a live workspace), to avoid hijacking replies. Under this decision a
  connected dispatcher is the *intended* transport, so the CLI must instead
  skip minting the stub and let the card and resumed reply ride that transport.

## Decision

**When `message` runs against a connected transport (a running dispatcher),
route the approval card and the resumed reply over the worker's default Slack
transport instead of the throwaway stub.**

- `local/cluster message` detects a connected dispatcher and **skips minting
  the stub endpoint**, leaving `reply_handle.endpoint` unset so the worker uses
  its default transport (the same way a real workspace's endpoint-less turns
  already do).
- The kernel's `in_requesting_channel` card path is reworked so a
  requesting-channel card with no per-turn endpoint posts over the default
  transport (the sacred-kernel change).
- The resumed reply **posts top-level** rather than editing the (real-Slack-
  nonexistent) placeholder ts.
- `cluster message`'s `--force-wire` guard is reworked: a connected dispatcher
  is the transport, not a hijack to refuse.
- The zero-Slack path (ADR-0063) is unchanged: with no connected transport, the
  stub is still minted and kept alive exactly as today.

This **partially supersedes ADR-0063** — specifically its deferral of the
connected-transport surface ("not (yet) the connected Slack transport" /
Follow-up A). ADR-0063's offline keep-alive decision and its Postgres-durable
`Approval` record (ADR-0010) stand unchanged; approval semantics are untouched
(resolution stays an authorized `--resolve` or a real card click).

## Consequences

- **This is a sacred-kernel change** (`kernel.py`) that overturns a pinned
  design, so it requires single-owner authorship + human adversarial review and
  updates to `test_approval_lifecycle.py`'s pinned assertions in the same
  reviewed change.
- **It must be validated with real-Slack E2E** (a connected workspace): a card
  that renders and a click that resolves, plus the top-level resumed reply.
  Unit tests alone cannot prove the transport routing.
- **Phasing (per the plan on #770).** This ADR lands first, on its own. The
  code change is delivered as a single coordinated PR — the kernel card-routing,
  the CLI no-stub detection, and the `--force-wire` rework together — because
  ADR-0063 established they are one change: the CLI no-stub path half-breaks the
  connected case without the kernel routing it over the default transport. That
  PR carries the sacred-kernel review and the real-Slack E2E.

## Alternatives considered

1. **Keep it deferred (status quo).** Rejected: #770 is assigned and the
   connected-transport card is the durable, clickable surface operators expect
   when a workspace is connected; the offline stub is a fallback, not the goal.
2. **A standalone persistent reply server instead of the connected transport.**
   Already rejected in ADR-0063 (its Alternative 2): more infrastructure for a
   surface the connected workspace already provides.
3. **Ship the CLI no-stub path ahead of the kernel change (a true "non-kernel
   first" PR).** Rejected: without the kernel routing an endpoint-less
   requesting-channel card over the default transport, skipping the stub drops
   the card in the connected case. The two are coupled, so they ship together.
