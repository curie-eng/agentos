# 111. The default memory compaction algorithm

Date: 2026-08-17

Status: Draft

Sits between [ADR-0095](0095-tiered-memory-lifecycle.md) (where a memory
document is stored and how it is injected) and
[ADR-0099](0099-hooks-are-bundle-declared-turns-the-system-starts.md) (how
background work runs at all). Neither says how a document comes to exist. This
one does, as an opt-in default rather than a requirement.

## Context

ADR-0095, as narrowed by the 2026-08-17 architecture review, defines exactly two
things: where a memory document lives durably, and that it is injected into a
session when it exists. It is explicit that *"how the heck did these get
created? Who updates them? Not the responsibility of that ADR."*

ADR-0099 gives an agent a way to run work no user started, on a schedule.

Between them is a gap that is technically the builder's to fill and in practice
nobody's. A platform can ship both halves and tell the builder to write the
connector, and the honest description of that state is the review's own: *"who
cares if memory.md is getting injected if nobody even has the ability to create
a memory.md? Like you're injecting 2 blank files."* Injection with no producer
is a feature that reads as finished and does nothing, which is the same shape as
a port with no caller — a failure this repository has shipped before.

So the platform should offer a default algorithm, opt-in, that a builder can
extend, replace, or switch off.

Three facts about the surrounding system shape it:

1. **The platform already holds the source.** Per-thread transcripts (ADR-0029)
   are the platform's own record of what was said. Reading the surface itself is
   a separate, optional capability (ADR-0100) that a surface may not have.
2. **The write door already exists.** ADR-0095's document is a state-store key
   the platform key may write. An operator hand-authoring a document and a
   scheduled job producing one use the same endpoint.
3. **Writes are already capped.** The state router refuses a value over the
   configured per-value size, so an oversized document is refused rather than
   silently injected.

## Decision

**The platform ships one default compaction algorithm: a scheduled turn that
rebuilds a scope's memory document from the platform's own transcripts, from
scratch, and writes it through the ordinary document write path. It is off
unless a bundle opts in, and a builder may extend, override, or disable it.**

### 1. From scratch, every time. Never a compaction of a compaction.

A run reads source material and produces the whole document. It does not read
yesterday's document and fold new material into it.

This is the load-bearing clause. Folding a summary into a summary compounds
error: each pass restates the last pass's paraphrase, and what began as a fact
arrives some runs later as an assertion nobody made. The review named it
plainly — *"you're going to hallucinate the bejesus out of it, because you're
compacting compactions; it's just a game of telephone at that point."*

The cost of this clause is the reason for clause 3.

### 2. The source is the platform's transcripts, not the surface.

A run reads the thread transcripts belonging to the scope, which the platform
already stores, and never the surface's own history.

This keeps the algorithm available on every surface from its first day: a
surface with no readable backlog (email) is not a special case, because the
algorithm never wanted the backlog. Where ADR-0100's read capability is enabled,
a run may additionally record pointers into the surface, but it may not depend
on them.

**This is also why no in-turn write path is required.** When a person says
"remember that our fiscal year ends in March", that utterance is already in the
transcript. A run picks it up without a tool the agent has to think to call, and
without opening a third write channel into memory — which the poisoning taxonomy
in ADR-0095 counts as a distinct exposure. The cost is latency, made explicit in
clause 7.

### 3. Off by default, opt-in per bundle.

Rebuilding from scratch is the expensive shape, and expense is not something to
enable on a builder's behalf. A bundle declares that it wants the default
algorithm; absent that declaration nothing runs and no document appears, which
is the state ADR-0095 already handles as ordinary.

### 4. Scope tier by default. No default agent-tier algorithm.

What a place knows is derivable from what was said in that place. What an agent
should carry across every place it works is a judgement about generality that
this algorithm has no basis to make, and promoting a scope fact to the agent
tier is exactly the move ADR-0095's trust posture says must stay conservative.

A builder who wants an agent-tier document writes one, by hand or with their own
algorithm, through the same write path.

### 5. A run writes through the ordinary document write path.

Not a private back door. The consequence worth stating: an operator can
hand-author a document today and a scheduled run can replace it tomorrow, and
neither needs to know about the other.

### 6. A run that cannot produce a valid document leaves the old one standing.

The write is capped, and a rebuilt document that exceeds the cap is refused. A
refused write must leave yesterday's document in place rather than replacing it
with nothing: stale memory is a degraded turn, and absent memory that used to be
present is a regression the agent cannot report.

A run that overflows should therefore shrink its output and retry within the
run, and a run that still cannot fit records the failure rather than clearing
the document.

### 7. Latency is a property of the design, not an accident.

A fact stated today is remembered from the next scheduled run, not from the next
turn. For work on a quarterly or weekly cadence this is invisible. For an agent
that must recall something said minutes ago, this algorithm is the wrong choice
and an in-turn write path is a separate decision, not a patch to this one.

### 8. Extension points

- **Replace** the prompt a run uses.
- **Replace** the whole algorithm with a bundle-declared hook of the builder's
  own, using the same write path.
- **Disable** it, which is also the default.

## Consequences

- Cost scales with transcript volume per scope, not with agent count. A scope
  with no new activity since its last run has nothing to rebuild from and should
  be skipped, so an idle deployment costs nothing.
- The first run against a long-lived place is the expensive one, and it is
  expensive exactly once.
- A builder who reads a wrong fact in a document fixes it by fixing the source
  or by overriding the algorithm, not by editing the document — the next run
  would overwrite the edit. This is a real ergonomic cost of clause 1 and the
  price of not compounding error.
- Nothing here requires ADR-0100. Where it is enabled a run gains pointers;
  where it is not the document is self-contained.
- Because the algorithm is a scheduled turn, everything ADR-0099 says about
  silent turns, run records, and per-scope serialisation applies unchanged. This
  ADR adds no new machinery.

## Alternatives considered

**Incremental notes folded periodically into the document.** The shape ADR-0095
originally carried: in-turn writes append notes, a scheduled pass folds them in.
Rejected because the fold is either a compaction of a compaction, which clause 1
refuses, or it re-reads the source anyway — at which point the notes are a
second write channel that changes nothing about the output.

**An in-turn `remember` tool as the primary write path.** Rejected as
unnecessary rather than wrong: the utterance a tool would capture is already in
the transcript this algorithm reads. It also asks the model to decide what is
worth keeping mid-task, which is a judgement made with less context than a run
that sees the whole period.

**On by default.** Rejected on cost. A platform that quietly bills every
deployment for a nightly full rebuild has made an expensive choice on the
builder's behalf.

**Compaction per thread rather than per scope.** Rejected: a thread's context is
already replayed from its transcript (ADR-0029). Memory exists for what crosses
threads.
