# 122. A snapshot is sealed to the connector that wrote it

Date: 2026-08-25

Status: Draft

Answers the exposure question [ADR-0117](0117-a-tool-that-changes-the-world-reports-what-it-changed.md)
left to the connector author and
[ADR-0121](0121-a-restore-is-the-connectors-own-verb-run-under-the-same-pinned-connector.md)
named as its own prerequisite. Changes no isolation boundary:
[ADR-0006](0006-security-rails-as-chart-defaults.md)'s rails and
[ADR-0008](0008-multi-tenancy.md)'s tenant compute stand as they are.

## Context

> Revised after spiking it. The first draft proposed sealing to the cluster key.
> Measuring that found it does not compose with ADR-0121 -- the runner holds no
> sealing key by design -- and that the party which actually needs to read a
> snapshot is neither the cluster nor the platform. The probes are on
> `spike/adr-0121-restore-executor`.

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

**A snapshot is sealed to the connector that produced it. The platform stores a
blob it cannot read, and the conflict check compares a version token instead of a
state.**

1. **The connector seals its own snapshot.** A write connector that reports a
   prior state reports it sealed to a key only that connector holds, mounted the
   way its credential already is. `prior_state` becomes opaque at the moment it
   leaves the process that read it.

   This is decision 1 of ADR-0117 followed to its conclusion. The tool's reply is
   the declaration; the tool also decides what the platform may read.

2. **Exactly one party can open it, and it is the one that replays it.** Not the
   API, not the worker, not the runner. That is what makes the executor's
   location a question about reachability alone: under
   [ADR-0121](0121-a-restore-is-the-connectors-own-verb-run-under-the-same-pinned-connector.md)
   the runner carries a blob it cannot open, so it needs no key and the sandbox
   keeps the property it exists for.

3. **The conflict check compares a version token.** A sealed box is
   non-deterministic, so ciphertexts of the same state differ and ADR-0117's
   state comparison cannot survive sealing. The connector reports the version its
   call left -- `resourceVersion`, an ETag, a generation counter -- and an undo
   carries the version observed now. The platform compares two opaque strings.

   This is a better question than state equality, not merely a compatible one. It
   catches a change that reverted to the same value, which a state comparison
   silently permits, and it does not false-positive on fields that churn on their
   own.

4. **A resource with no version token has no conflict check, and its actions are
   not undoable.** Deny-by-default, the same rule the rest of this design runs
   on, rather than a restore performed without knowing whether the world moved.

5. **The receipt renders a summary, never a snapshot.** It is built from
   `summary`, `target` and the tool name, which is all it has ever needed to say
   "scaled 3 to 10, undo". Under this decision the receipt *cannot* render the
   snapshot even by mistake, which is the difference between a rule and a
   guarantee.

6. **The ACI frames stay verbatim.** They carry a sealed blob, so there is
   nothing left to scrub, and scrubbing the product contract in flight would
   corrupt the field a restore replays.

## Consequences

- **A write connector that snapshots sensitive state needs a durable keypair.**
  A real cost, and it lands on the authors who should be carrying it. A connector
  whose snapshot holds nothing sensitive -- `{"spec": {"replicas": 3}}` is the
  common case -- may report it in the clear and keep the richer refusal message.

- **A refusal names versions, not states.** "v2 vs v3" instead of "10 vs 7". That
  is a real loss against ADR-0117's stated intent, which valued an operator
  seeing their own fix named. The mitigation is that the summary line and the
  target still are, and the operator learns their change is what stopped it.

- **A lost or rotated connector key makes old snapshots unrestorable.** Same
  shape as ADR-0121's unavailable pinned digest, and the same answer: the undo is
  refused with that as its stated reason.

- **The ledger stops being a credential-bearing store**, rather than becoming a
  secured one. Nothing that dumps `agent_actions` -- a support bundle, a backup,
  a debug endpoint -- can leak a snapshot, because none of them can read it.

- **`arguments` and `result` are still in the clear.** They can carry secrets and
  are read by humans on the receipt path, so they take the scrubbing half. This
  decision does not cover them and does not pretend to.

## Alternatives considered

- **Scrub the snapshot before storing.** Rejected. A snapshot is the input to a
  write, not a log line: ADR-0121 replays it. Scrubbing sets the resource's token
  to the literal string `[REDACTED:bearer_token]` and reports success -- a silent
  corruption of the workload it was restoring, which is worse than the exposure
  it prevents.

- **Seal to the cluster key, as this ADR first proposed.** Rejected by measuring
  it. Sealing needs only the public half so the worker can seal; opening needs
  the private key, which the runner deliberately does not have. ADR-0121 puts the
  executor in the runner, so a cluster-sealed snapshot is one the executor cannot
  open. Every way out of that traded something real away -- move the executor and
  lose ADR-0121's reachability argument, open in the worker and spend the seal on
  the hop it protected, or give the sandbox a key it exists not to have.

  Sealing to the connector dissolves the tension rather than trading it, and is
  the stronger property besides: cluster-sealed, the platform holds the key and
  an operator has to evaluate that; connector-sealed, there is nothing to
  evaluate.

- **Scrub the ACI frames.** Rejected per decision 6: the same corruption one hop
  earlier, and it makes the product contract's content depend on a mutable rule
  table.

- **The connector stores the snapshot and returns a handle.** Strongest
  confidentiality -- nothing leaves at all -- and rejected on cost. Connectors are
  stateless, and durable per-action storage is a larger burden on every author
  than a mounted keypair.

- **Do not store snapshots for resources that can carry secrets.** ADR-0117's
  current answer, kept as the escape hatch and demoted from being the whole
  policy: it asks each author to classify a resource kind correctly, forever, and
  the penalty for guessing wrong is a credential in the control plane.

## Out of scope

- **Key management and rotation for a connector's sealing key.** ADR-0094's
  cluster keypair is a different key with a different holder; this decision needs
  its own, and how it is minted, mounted and rotated is not settled here.
- **Retention and pruning of the ledger**, still open from ADR-0117.
- **Whether `arguments` and `result` need their own treatment beyond scrubbing.**
  Named in the consequences as uncovered; deciding it needs the receipt surface
  to exist first.
