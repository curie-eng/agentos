---
seam: Conversation history
kind: CLEAN
impls: 1 loader (StateApiTranscriptStore)
grade: not separately graded
epics:
  - "#20"
order: 16
---

# INTERFACE: Conversation history

> Part of the Curie swappable-seam catalog — see the [seam index](../../interfaces.md).

<!-- BEGIN GENERATED: header (curie dev docs-lint) -->
> **Kind:** CLEAN &nbsp;·&nbsp; **Implementations today:** 1 loader (StateApiTranscriptStore) &nbsp;·&nbsp; **Swap-readiness grade:** not separately graded
<!-- END GENERATED: header -->

**Kind legend:** CLEAN = a real `Protocol`/typed port class · SOFT = swap via env/URL/prefix/wire, no code interface · NONE = not built yet.

## The black line

The port is the `TranscriptStore` `Protocol` in
`runner/src/curie_runner/history.py` (issue #20, ADR-0029). Two methods:

```python
class TranscriptStore(Protocol):
    async def load(self) -> list[TurnRecord]: ...
    async def append(self, record: TurnRecord) -> None: ...
```

A `TurnRecord` is `user: str` plus `assistant: str` (and a `ts`) — one
conversation turn. `CURIE_HISTORY_REF` (a runner-local env, NOT a frozen ACI
`SessionConfig` field) is resolved to a concrete `TranscriptStore` at runner boot
by `resolve_history`. The state-API bearer is a runner-local knob
(`CURIE_HISTORY_TOKEN`), like `CURIE_MEMORY_TOKEN`.

This is the sibling seam of [Memory](../memory/INTERFACE.md): same store, same
boot-preamble delivery, a different scope. Memory is per-agent durable lessons;
history is *this thread's* conversation, so a restarted sandbox can rehydrate the
turns already spoken.

## Current contract

- **Resolution.** `resolve_history(history_ref, env)`: an absent ref →
  `NullTranscriptStore`; an `http(s)://` ref → `StateApiTranscriptStore`; any
  other scheme (an old SDK-resume id, `s3://` …) is reserved for a future loader
  and rejected loudly.
- **Load side.** `load()` returns prior turns oldest-first (empty when none). At
  boot the runner loads the transcript and composes it into the effective system
  prompt as a conversation preamble (after the memory preamble, before the
  bundle/env prompt) — this is how history is *delivered into the sandbox*,
  harness-agnostically (plain prompt text, not any one harness's resume API). A
  transient load failure degrades to "no history" and does not block boot.
- **Append side.** `append(record)` durably writes one turn. The runner appends
  `{user, assistant}` only for a successful terminal `final` with `DONE` status
  (`SessionRunner._record_turn`), best-effort — a store failure never fails a
  turn the user already received. Classified failures, budget/auth halts,
  awaiting-approval outcomes, and idle or otherwise incomplete outcomes are
  not recorded.

## Implementations today

One: **`StateApiTranscriptStore`**, backing the transcript as a per-thread
`transcript/<thread_key>` key over the durable KV/document store landed for
#23/#248 (`apps/api` `/agents/{agent_id}/state/{namespace}/{key}`, Postgres
JSONB). `load` GETs the key; `append` POSTs to the key's `/append` endpoint,
inheriting durability and the per-value/per-namespace size caps.
`NullTranscriptStore` is the no-ref sink. The worker (`binding.boot_env`)
delivers the ref as `http(s)://api/agents/<id>/state/transcript/<thread_key>`
(URL-encoded thread key) and forwards a scoped, agent-bound `state` token
(ADR-0033, #410) as the history token rather than the raw platform key. The ref
is **deterministic per (agent, thread)**, so a fresh, a restarted, and a resumed
sandbox all boot with the same ref and rehydrate identically — the
unplanned-restart case needs no special worker/kernel branch.

## Known leakage

- **Scoped history token (was: shared API key).** Same as memory: earlier the
  state API's one shared platform key was forwarded as `CURIE_HISTORY_TOKEN`,
  granting that key's scope. ADR-0033 (#410) replaced it with a scoped,
  agent-bound, HMAC-signed `state` token minted per turn, accepted only by the
  state router and bound to this agent's namespace, so the sandbox credential can
  no longer resolve approvals or reach another agent's state.
- **Unbounded append log under the state-store size caps.** A very long thread
  will eventually hit the per-namespace/per-value cap on the stored transcript.
  Delivery-side windowing now bounds the *rehydrated preamble* to a tail window
  (most-recent turns, byte-capped; `CURIE_HISTORY_MAX_TURNS` /
  `CURIE_HISTORY_MAX_BYTES`, overridable) -- but the stored transcript itself
  stays unwindowed, so the append log still grows unbounded until it hits the
  state-store cap. The replay is a prompt preamble, not a token-exact
  reconstruction, so it does not restore the model's original prompt cache
  (consistent with ADR-0003, which assumes cache-cold on a restart).
- **History lives OUTSIDE the sandbox** (ADR-0003) — the store is
  network-reachable and rehydratable, never pod-local state.

## Cross-links

- **Issue:** [#20](https://github.com/curie-eng/curie/issues/20) — transcript persistence across unplanned runner restarts
- **ADR(s):** [ADR-0029](../../adr/0029-conversation-history-port-and-first-loader.md) — the port + first loader; [ADR-0025](../../adr/0025-memory-port-and-first-loader.md) — the sibling memory port; [ADR-0003](../../adr/0003-stateless-first-rehydrate-on-resume.md) — stateless-first; rehydrate on resume; externalize session state
