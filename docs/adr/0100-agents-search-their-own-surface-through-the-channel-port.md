# 100. Agents search their own surface through the channel port

Date: 2026-08-06

Status: Draft

## Context

An agent needs to recover decisions and context from the place where it works.
For a channel bound agent, that place is its installed surface and its current
channel. It is not the workspace, another channel, or a general company search
surface.

Today that readable surface is undefined. The channel port in
[ADR 0020](0020-message-port-rendering-free-channel-interface.md) sends
messages, but an agent cannot read a known message, a thread, or a bounded
history window. That leaves a live turn unable to answer a question such as
what the channel decided earlier without depending on its current context or
an independently maintained memory file.

[ADR 0095](0095-tiered-memory-lifecycle.md) has a related unresolved
requirement. Memory must stay scoped to an agent or channel tier and must have
an escape valve to the fuller source material. That escape valve needs a
properly bounded readable surface, but surface search is not memory itself.

The architecture review on 2026 08 17 made the intended boundary explicit:
an agent may search only the surface and channel where it is installed. The
outcome of building that capability is not yet known, so this ADR authorizes a
small experimental spike rather than a production implementation.

## Decision

**The candidate capability is an agent searching its own installed surface and
current channel. The platform, not a bundle supplied credential or a search
query, enforces that boundary. We will prove the capability with a bounded MCP
server spike before deciding its permanent port contract or implementation.**

### The boundary

For a channel oriented adapter, the bound surface is the channel identity from
the agent installation. Every read or search request is constrained to that
identity before it reaches the adapter. A request cannot select a different
channel, surface, or workspace.

This is a structural capability boundary. It must not depend on model
instructions, query filtering, a user supplied channel identifier, or a claim
that the caller probably has access. A result outside the bound surface is not
available to the agent.

The capability is deliberately narrower than enterprise search. It does not
provide cross channel recall, a workspace corpus, or ambient access to every
surface reachable by an integration token.

### The spike

The spike exposes a minimal MCP server for an installed agent to search and
read its bound surface. It must establish whether an adapter can provide useful
bounded reads without widening the boundary or retaining a second copy of
surface content.

The spike must demonstrate all of the following through its real consumer path:

1. A bound agent can find and read relevant content in its own current channel.
2. The same agent cannot use any request input to read or search another
   channel or surface.
3. Retrieved content is returned as data with source provenance, not as
   trusted instructions to the agent.
4. The implementation does not persist message bodies, a search index, or
   embeddings as platform state.
5. The result and per turn resource bounds are enforced below the model.

The spike may determine that the proposed shape is not viable. In that case it
records the constraint and returns no production interface. It must not quietly
substitute a broader search capability to make the experiment appear useful.

### Relationship to memory and lifecycle work

The resulting readable surface may become ADR 0095's escape valve. It does not
make channel history into memory, and it does not decide memory retention,
compaction, or cross tier promotion.

Likewise, hooks and memory files may later form a default compaction algorithm.
That connector deserves its own decision because its authorization and
unattended execution semantics are different from a live agent turn. This ADR
does not grant hooks additional read authority.

## Consequences

1. The product definition is clear before an implementation shape is chosen:
   agents search their own installed surface, never a larger ambient corpus.
2. The work remains experimental until the spike demonstrates both useful
   retrieval and the negative boundary proof.
3. A production follow up must name the adapter contract, enablement model,
   result limits, authorization for unattended turns, and durable tests. It
   must not inherit those details from this Draft.
4. ADR 0095 remains responsible for deciding how memory is scoped and for
   incorporating any proven escape valve.

## Alternatives considered

1. **Workspace search.** Rejected. It gives an installed agent access beyond
   the place where it works and makes the security boundary dependent on search
   filters or a broad integration credential.
2. **Cross channel search.** Rejected. This is a different product capability
   with different authorization and consent requirements.
3. **Treat memory as the readable surface.** Rejected. Memory is a curated,
   retained artifact. Search must be able to retrieve source material that was
   not promoted into memory.
4. **Commit to channel port verbs now.** Deferred. The spike must first prove
   the concrete adapter can preserve the boundary and provide useful retrieval.
5. **Build a persistent search index.** Rejected for the spike. It creates a
   second retained copy of surface content before the value and deletion model
   are understood.
