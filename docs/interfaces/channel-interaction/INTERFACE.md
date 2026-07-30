---
seam: Channel interaction message
kind: CLEAN
impls: 2 renderers (Slack, terminal)
grade: not separately graded
epics:
  - "ADR-0020"
order: 5
---

# INTERFACE: Channel interaction

> Part of the Curie swappable-seam catalog — see the [seam index](../../interfaces.md).

<!-- BEGIN GENERATED: header (curie dev docs-lint) -->
> **Kind:** CLEAN &nbsp;·&nbsp; **Implementations today:** 2 renderers (Slack, terminal) &nbsp;·&nbsp; **Swap-readiness grade:** not separately graded
<!-- END GENERATED: header -->

**Kind legend:** CLEAN = a real `Protocol`/typed port class · SOFT = swap via env/URL/prefix/wire, no code interface · NONE = not built yet.

## The black line

Agents produce a semantic `OutboundMessage`; channel adapters render it. Slack
may use Block Kit and the terminal may use a numbered selector, but neither
widget appears in the contract. This is the interaction half of ADR-0020.

The source of truth is
`packages/channel-protocol/src/channel_protocol/models.py`; the committed JSON
Schema is `packages/channel-protocol/schema/channel-protocol.schema.json`.

## Message contract

Every message has `version: "1.0"` and mandatory `text`. The text must be a
complete usable reply after all optional fields are removed. Optional `status`,
`header`, `fields`, `links`, and `footer` enrich presentation.

An optional `interaction` is one semantic intent:

- `choice`: an id, optional prompt, one to ten `{label, value}` options, and
  `allow_free_text` (default `true`).
- `confirm`: an id, prompt, semantic confirm and cancel actions, and
  `allow_free_text` (default `false`).

Action `label` is display text. Action `value` is the exact inbound message sent
when selected. Values are conversation input, not trusted authorization tokens;
the server must still authorize side effects and approvals.

## ACI envelope

Until ACI gains a native semantic-message event, a runner carries the message
inside its final text as a complete fenced block:

````text
```curie-reply
{"version":"1.0","text":"Which view?","interaction":{"kind":"choice","id":"view","options":[{"label":"Open issues","value":"show open issues"}]}}
```
````

Adapters must hide incomplete or malformed envelopes and fall back to ordinary
text. The legacy unversioned `buttons: [[label, value]]` shape remains readable
during migration but is not valid v1 authoring.

## Adapter requirements

- Render `text` even when no optional capability is supported.
- Advertise capabilities; never infer them from the channel name.
- Render choices and confirmations using native affordances when
  `interactive-actions` is present and as numbered text otherwise.
- Preserve action values exactly when converting a selection into inbound text.
- Keep rendering and channel-native payloads inside the adapter.
- Treat links as navigation, not conversation responses.
- Never grant authority based only on an action id or value.

## TUI behavior

The Curie TUI advertises `interactive-actions`, `live-steering`, `streaming`,
and `threading`. It renders agent-authored actions first and appends `Type a
message...` as the final selector option when free text is allowed. Selecting
that option enters an explicit compose mode; it is a terminal affordance and is
never added to the agent-authored contract. The selector expands to keep every
contract option and the appended free-response option visible. The compose
field is hidden until free response is selected. The TUI sends the selected
action value or composed text as the next turn, replaces stale actions after
each reply, and never prints protocol fences or terminal status frames.

The conversation transcript remains scrollable while selecting, composing, and
waiting for a response. Scrolling suspends tail-follow until the user returns to
the latest output. Transcript navigation is adapter state and must not alter the
outbound message or its interaction intent.

Starter prompts are bundle metadata (`starterPrompts`), not response actions and
not hardcoded into the TUI. They disappear after the first turn unless the agent
returns a new interaction.

## Implementations today

Two renderers consume the same `OutboundMessage`, and neither leaks its widgets
back into the contract:

1. **Slack (Block Kit)** — `apps/worker/src/curie_worker/blocks.py`. The worker
   parses the `curie-reply` envelope out of the runner's final text
   (`apps/worker/src/curie_worker/blocks.py::parse_reply`), maps it onto the
   internal `apps/worker/src/curie_worker/blocks.py::Reply` shape
   (`apps/worker/src/curie_worker/blocks.py::_reply_from_message`), and renders
   Block Kit sections/buttons via
   `apps/worker/src/curie_worker/blocks.py::to_blocks`. `apps/worker/src/curie_worker/blocks.py::render`
   is the fallback boundary: anything that is not a complete, valid envelope
   degrades to plain text, so a half-streamed block never shows raw JSON. Slack's
   3000-char section cap is absorbed here by
   `apps/worker/src/curie_worker/blocks.py::chunk`, not pushed onto the agent.
   The approval card (ADR-0010) travels the same seam: the kernel emits a
   `confirm` intent and the adapter renders it below the line via
   `apps/worker/src/curie_worker/blocks.py::approval_card` inside
   `apps/worker/src/curie_worker/slack_sink.py::AsyncSlackSink.post` (its
   settled/expired form via
   `apps/worker/src/curie_worker/blocks.py::expired_approval_card`), so no Block
   Kit is built above the seam. `allow_free_text` on that intent is rendered here
   too: Slack expresses "this decision may carry free text" as a dialog opened by
   the click, so the card carries the note-collecting action ids and the
   dispatcher's `apps/dispatcher/src/curie_dispatcher/approval_actions.py::open_note_dialog`
   opens the view. Stamping a settled card re-reads the original through
   `conversations.replies` rather than `conversations.history`, because the
   default card is a thread reply and the channel timeline does not carry one
   (#1073); a card that cannot be read is left unstamped rather than rebuilt
   from nothing.
2. **Terminal (TUI selector)** — `cli/src/channel.rs`. It parses the same fence
   (`REPLY_FENCE`) into a `TerminalMessage` of plain lines plus actions, which the
   TUI renders as a numbered selector per the TUI behavior above. It expresses
   `allow_free_text` as a typed reply alongside the numbered actions.

Both renderers now honor `allow_free_text`; until #1053 the terminal one did and
the Slack one silently dropped it, which is the sibling-path drift this catalog
exists to make visible.

The split is the point: Block Kit lives only in `blocks.py`, the numbered selector
only in `channel.rs`, and the agent authors neither.

## Known leakage

- **The terminal renderer is a hand-written mirror, not generated.** The source of
  truth is the Pydantic model in
  `packages/channel-protocol/src/channel_protocol/models.py` with a committed JSON
  Schema (`packages/channel-protocol/schema/channel-protocol.schema.json`), but
  `channel-protocol` ships **no Rust binding**. `cli/src/channel.rs` re-declares the
  wire shape by hand (`#[serde(deny_unknown_fields)]` on each struct). Nothing gates
  that mirror against the schema the way ADR-0017 gates the ACI's tri-language
  contract, so a field added in Python is not mechanically caught here — and
  `deny_unknown_fields` means the mirror *rejects* the new field rather than
  ignoring it. This is the seam's real drift risk, and it is why "2 renderers" does
  not imply "2 generated adapters".
- **The envelope is a text-channel workaround.** ACI has no native
  semantic-message event, so the message rides inside the runner's final text as a
  fenced block. Every adapter therefore carries fence-parsing and partial-envelope
  suppression that a native event would delete. Named in "ACI envelope" above as
  explicitly interim.
- **Not separately graded** — this seam is the interaction half of ADR-0020 and is
  not one of the six swap-readiness Jobs. The channel *ingress/egress* swap story is
  graded, and graded `C`, on the [channel-ingress](../channel-ingress/INTERFACE.md)
  seam. Read that grade as the honest one for "can we add a second channel"; this
  file only covers the rendering-free message contract.

## Cross-links

- **ADR(s):** [ADR-0020](../../adr/0020-message-port-rendering-free-channel-interface.md) — the message port: a rendering-free channel interface with capability negotiation (this file is its interaction half); [ADR-0017](../../adr/0017-tri-language-contract-codegen.md) — the tri-language codegen pattern this seam's Rust mirror does **not** yet follow
- **Sibling seams:** [channel-ingress](../channel-ingress/INTERFACE.md) — the graded (`C`) ingress/egress swap story; [aci-producer](../aci-producer/INTERFACE.md) — the frozen ACI the envelope currently tunnels through
- **Vision doc:** [architecture-vision.md](../../architecture-vision.md) — the interaction contract is not one of the six swap-readiness Jobs; not separately graded
