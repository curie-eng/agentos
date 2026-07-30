# 88. Per user delegated OAuth for MCP

Date: 2026-07-30
Status: Draft

Extends: [ADR 0009](0009-per-agent-connector-auth.md) by adding a user owned
credential mode alongside per agent credentials.

Depends on: [Discussion 1049](https://github.com/curie-eng/curie/discussions/1049)
for canonical caller identity and invocation authorization.

Related direction: [ADR 0075](0075-the-agent-proxy-credential-and-egress-boundary.md)
for moving credentials and outbound enforcement outside sandbox trust.

## Context

Curie currently gives an agent deployment one set of MCP credentials. A bundle
names the required secrets, the deployment supplies their values, and every
person who invokes the agent reaches the same downstream identity. This is the
correct model for bot accounts and service accounts.

Some remote MCP servers instead authenticate the human using OAuth. Five people
invoking one Curie agent should then reach five different downstream identities
and permission sets. The caller, not the agent deployment, owns the grant.

The two credential models must coexist:

1. A service credential belongs to an agent deployment.
2. A delegated grant belongs to a human principal.

Discussion 1049 establishes who may invoke an agent and how Curie attributes
that invocation. It explicitly leaves downstream delegation out of scope. This
ADR consumes the canonical principal produced by that work and decides how it
selects an MCP credential.

The runtime makes this a platform concern rather than a harness feature. Curie
keeps one sandbox and one harness session alive for a thread. A different human
can send a later turn into that session. MCP configuration is assembled when
the harness starts, and its HTTP headers are static. Harness managed OAuth state
therefore cannot safely represent the identity of the current run.

The current
[MCP authorization specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)
defines the protocol Curie must follow for remote HTTP servers. It includes
protected resource discovery, authorization server discovery, authorization
code with PKCE, resource indicators, audience bound tokens, scopes, refresh,
and client registration.

[Linear MCP](https://linear.app/docs/mcp) demonstrates the product need. It
supports user OAuth as well as bearer tokens for application or service style
access. Dust separately demonstrates personal and workspace MCP connections,
while LibreChat demonstrates encrypted grants keyed by user and server.

## Decision

Every remote MCP connection has one explicit identity mode:

1. `service` uses credentials owned by the agent deployment.
2. `delegated` uses an OAuth grant owned by the human who initiated the run.
3. `none` is permitted when the remote MCP server requires no authentication.

There is no automatic fallback between modes. In particular, a missing,
expired, or revoked delegated grant never widens to a service credential.

The bundle continues to declare the MCP server name and URL. Deployment
configuration owns the identity mode, allowed resource, approved scope ceiling,
client registration, and destination policy. This keeps user identity and
credential policy out of the frozen plugin format.

### The run owns one principal

The worker binds one canonical principal when a run starts. That binding is
immutable until the run reaches a terminal state.

The principal includes the Curie workspace, ingress provider, provider
workspace, provider user identifier, and canonical Curie user identifier when
account linking exists.

A different human who sends a message during an active run cannot inherit the
first caller's delegated authority. A message from the same principal may
steer. A message from another principal waits for the next run or receives a
clear refusal.

A scheduled run has no initiating human. It cannot use delegated mode unless a
future decision defines a durable principal for schedules.

### The control plane owns OAuth

The API owns:

1. Protected resource and authorization server discovery.
2. Client registration selection.
3. Authorization initiation with one time state and PKCE.
4. Callback validation and authorization code exchange.
5. Encrypted access tokens, refresh tokens, and client secrets.
6. Refresh rotation, revocation, and reconnection state.
7. Grant status that never returns token material.

OAuth flow state is bound to the workspace, principal, agent, MCP server,
resource, requested scopes, redirect target, expiry, and one time nonce. A
callback succeeds only for the intended Curie principal. Possession of a link
posted in a shared channel is not sufficient authorization.

The grant store keeps separate records for:

1. The administrator approved MCP server connection.
2. Reusable client registration material.
3. Each human owned OAuth grant.
4. Each short lived authorization flow.
5. Each short lived run principal binding.

Production token material uses envelope encryption. Tokens never appear in API
responses, Kubernetes Secrets, pod environment, mounted files, SandboxClaim
objects, ACI events, traces, logs, bundle artifacts, or model context.

### The MCP gateway applies the grant

A sandbox connects to a Curie MCP gateway instead of connecting directly to a
delegated remote server.

The gateway receives a capability that identifies the agent and sandbox but
does not let the sandbox select a human. It reads the active run principal,
loads or refreshes only that principal's grant, enforces the configured resource
and scope ceiling, and creates a separate HTTPS MCP connection to the remote
server.

The gateway terminates the local MCP connection. It does not transparently
intercept an existing TLS connection. This makes delegated MCP a focused
realization of the Agent Proxy direction without requiring the broader proxy
substrate and TLS termination decisions in ADR 0075.

The runner never receives raw OAuth tokens and never calls an API that dispenses
them.

### Missing authentication pauses the run

Missing authentication is a typed run outcome:

1. The gateway reports that authentication is required.
2. Curie pauses the run and offers a connection action to the initiating
   principal.
3. The authorization URL is bound to that principal.
4. A successful callback stores the grant and resumes only that run.
5. Denial, cancellation, or timeout closes the run without executing the tool.

This may reuse the durable pause and resume mechanism used for approvals. OAuth
connection is not approval and must keep a distinct status and user facing
explanation.

Refresh occurs before a call when expiry is known. A remote unauthorized
response permits one forced refresh and connection rebuild. A read only tool
may be retried after refresh. A write tool is not replayed after uncertain side
effects.

### The parity ladder uses the same model

At `skill`, the developer is the principal. OAuth uses a localhost callback and
private host storage. A small local gateway starts and stops with the runner.

At `local`, the CLI supplies an explicit development principal. The compose
stack uses the same API, persistence, refresh, failure, and gateway behavior as
cluster.

At `cluster`, the ingress identity resolves to the canonical principal. The API
serves a public HTTPS callback and the gateway remains outside sandbox trust.

Automated evals use a fake OAuth server and fake MCP server with two users,
different scopes, expiry, refresh rotation, revocation, and denial. A delegated
eval uses an explicit test principal and never consumes a real human grant by
default.

## Consequences

Service mode remains unchanged, so existing bot and service account connectors
continue to work.

Delegated mode becomes a control plane feature with persistent grants and a new
in path gateway. Curie takes responsibility for OAuth discovery, client
registration, callbacks, refresh, revocation, and provider failure handling.

The sandbox loses direct access to delegated credentials. Compromised agent
code can invoke tools only under the principal already bound to the active run.
It cannot enumerate or select another user's grant.

Tool availability can differ by user because granted scopes can differ. Curie
records requested and granted scopes separately and requires a new
authorization flow for scope escalation.

User removal, workspace removal, and MCP server removal must delete or revoke
the affected grants. Remote revocation is attempted when supported, while local
deletion always occurs.

The first implementation may use the existing Slack workspace and user
identifier for a single workspace prototype. Production support across ingress
providers depends on the canonical identity from Discussion 1049. If the frozen
ACI cannot carry that identity unambiguously, the contract change must be
proposed and reviewed separately before implementation.

## Alternatives considered

### Let the harness manage OAuth

Rejected because its OAuth state is process or filesystem state attached to a
long lived thread session, not to the principal of the active run. It also puts
credentials inside sandbox trust.

### Create one sandbox per human

Rejected because it changes thread routing, shared conversation behavior, warm
pool economics, and prompt cache affinity. It still leaves refresh and
revocation in ephemeral runtime state.

### Put the user token in bundle headers

Rejected because bundle configuration is immutable and loaded when the harness
starts. It would expose token material to the sandbox and bind later turns to
the first caller.

### Let the runner fetch raw tokens

Rejected because it creates a credential dispenser reachable from untrusted
runtime code. The gateway can apply a credential without ever returning it.

### Fall back to the service identity

Rejected because the call would execute with broader or simply different
authority than the caller intended. A visible connection requirement is the
correct failure.

## Acceptance conditions

This ADR remains Draft until maintainers approve the identity modes, immutable
run principal, control plane grant ownership, and gateway boundary.

Implementation work begins only after acceptance and must demonstrate:

1. Two Slack users connect different Linear accounts and invoke the same agent.
   Each call executes under the correct Linear identity.
2. A user without a grant receives a connection action and no remote tool
   executes.
3. Completing OAuth resumes only that user's paused run.
4. A revoked or expired grant refreshes or requests authentication without
   service fallback.
5. A different human cannot steer an active run under the first caller's
   authority.
6. Service mode continues to behave as it did before this decision.
7. The same fake OAuth and MCP cases pass at skill, local, and cluster.
8. Token searches across runtime environment, files, cluster objects, events,
   logs, traces, bundles, and API responses find no token material.

## Open decisions

1. Whether the first production release requires a canonical Curie account
   linked to Slack or can use a Slack workspace user directly.
2. Whether the MCP gateway begins as an independent service or the first
   concrete component of the Agent Proxy.
3. Whether the connection action appears in a direct message, the original
   thread, or both.
4. Which key management systems the first self hosted release supports.
