# 122. A snapshot is sealed, not scrubbed

Date: 2026-08-25

Status: Draft

Answers the exposure question [ADR-0117](0117-a-tool-that-changes-the-world-reports-what-it-changed.md)
left to the connector author and
[ADR-0121](0121-a-restore-is-the-connectors-own-verb-run-under-the-same-pinned-connector.md)
named as its own prerequisite. Changes no isolation boundary:
[ADR-0006](0006-security-rails-as-chart-defaults.md)'s rails and
[ADR-0008](0008-multi-tenancy.md)'s tenant compute stand as they are.

## Context

ADR-0117 states, in its consequences, that a snapshot's secrets "pass through the
existing redaction path". **That is not true today, in either direction.**

[`packages/telemetry/src/curie_telemetry/redact.py`](../../packages/telemetry/src/curie_telemetry/redact.py)
declares its own scope in one line: `REDACTION_BOUNDARIES = ("stdout",
"gen_ai_span")`. A snapshot travels on neither. It rides an ACI frame, and
[`runner/src/curie_runner/redact.py`](../../runner/src/curie_runner/redact.py)
re-exports that same policy without widening it. The runner's server "streams
verbatim model output as NDJSON ACI frames, which is a larger surface left
deliberately untouched here: the frames are the product contract, and scrubbing
them is a separate decision."

This is that decision.

### What changed to make it urgent

While the frame carried a tool name and a constant string, the gap was
theoretical. It stopped being theoretical when the frame began carrying
`arguments` and `result`, and the ledger began storing `prior_state`
([`apps/api/src/curie_api/models.py::AgentAction`](../../apps/api/src/curie_api/models.py)).

A Kubernetes Deployment's pod template carries environment variables and secret
references. So a snapshot of one is, routinely, a credential-bearing document
that the platform now stores indefinitely -- ADR-0117 decided a record does not
expire -- and that ADR-0121 proposes to send back out to a sandbox on undo.

### The obvious answer is wrong, and provably

"Run the existing rules over the snapshot before storing it." The rules work:

```
before  {"env": [{"name": "API_TOKEN", "value": "Bearer sk-live-abcdef123456"}]}
after   {"env": [{"name": "API_TOKEN", "value": "[REDACTED:bearer_token]"}]}
```

And that is the problem. A snapshot is not a log line. It is **the input to a
write**: ADR-0121 replays it through the connector's own restore verb. Scrubbing
it means an undo sets the resource's token to the literal string
`[REDACTED:bearer_token]`.

So redaction, applied here, converts a stored secret into a **worse** outcome
than the exposure it prevents: an undo that reports success and quietly breaks
the workload it was restoring. The receipt would say the world was put back.

That rules out scrubbing wherever the value must remain replayable. It does not
rule out scrubbing where the value is only ever read by a human.

## Decision

**A snapshot is sealed at rest and never scrubbed. What a human reads is
scrubbed and never replayed. The two are different artifacts and the ledger holds
both.**

1. **`prior_state` is stored sealed**, through the credential sealing that
   already exists for connector secrets rather than a second mechanism. It is
   written by the platform, read only by the restore path, and never rendered to
   a receipt, an API list response, or a log line.

2. **The receipt renders a scrubbed summary, never the snapshot.** The human-
   facing line is built from `summary`, `target` and the tool name. A receipt has
   never needed the snapshot's contents to say "scaled 3 to 10, undo", and the
   moment it does it becomes a disclosure surface with a click on it.

3. **The ACI frames stay verbatim.** Scrubbing the product contract in flight
   would corrupt exactly the field a restore replays, for the reason above, and
   would make the wire's meaning depend on a rule table that can change under a
   consumer. The frames' exposure is bounded by where they already run: the
   sandbox that produced the value and the worker that records it.

4. **A connector that cannot produce a replayable snapshot reports itself
   irreversible.** ADR-0117 already says this, and it stays the escape hatch --
   but it stops being the only answer, which is what made it insufficient. It
   asked every author to reason correctly about a control plane they cannot see.

5. **The exposure that remains is named, not implied.** A sealed snapshot is
   readable by whatever holds the sealing key, which is the platform. That is a
   real, bounded statement an operator can evaluate, and it replaces the current
   position, which is an ADR sentence that says a redaction path covers this when
   no such path exists.

## Consequences

- **The ledger becomes a credential-bearing store, explicitly.** It already was;
  this says so and secures it accordingly. Anything that dumps `agent_actions`
  -- a support bundle, a backup, a debug endpoint -- now has a rule to obey
  rather than an assumption to violate.

- **Undo keeps working on resources that carry secrets.** Under scrubbing it
  would not, and would not say so.

- **A second read path is now the thing to review hardest.** `prior_state` has
  exactly one legitimate reader. A future feature that renders it -- a diff view,
  an "what changed" expansion on the receipt -- is the regression this decision
  exists to make visible, and it should be hard to add by accident.

- **Sealing is not encryption at rest for the whole table.** Columns beside it
  (`arguments`, `result`) can also carry secrets, and this decision does not
  cover them; they are read by humans on the receipt path, so they take the
  scrubbing half.

## Alternatives considered

- **Scrub the snapshot before storing.** Rejected above: it corrupts the input to
  a write, and the failure is silent and worse than the leak.

- **Scrub the ACI frames.** Rejected per decision 3. It is the same corruption
  one hop earlier, plus it makes the product contract's content depend on a
  mutable rule table.

- **Do not store snapshots for resources that can carry secrets.** This is
  ADR-0117's current answer and it is not sufficient on its own -- it asks each
  connector author to classify a resource kind correctly, forever, and the
  penalty for guessing wrong is a credential in the control plane. Kept as the
  escape hatch, demoted from being the whole policy.

- **Encrypt the whole `agent_actions` table at rest.** Heavier, and it answers a
  different question: it protects the disk, not the read paths. The leak this
  decision is about is a snapshot rendered onto a Slack card.

## Out of scope

- **Key management and rotation for the sealing path.** Whatever the sealed-
  credential seam decides applies here; this ADR adopts it rather than extending
  it.
- **Retention and pruning of the ledger**, still open from ADR-0117.
- **Whether `arguments` and `result` need their own treatment beyond scrubbing.**
  Named in the consequences as uncovered; deciding it needs the receipt surface
  to exist first.
