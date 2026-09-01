# 75. The Agent Proxy: credentials and egress leave the sandbox

Date: 2026-07-23
Status: Accepted

Supersedes: [ADR-0032](0032-explicit-provider-egress.md) (the
interim install-time CIDR resolve becomes the FQDN proxy the durable-mechanism
clause of that ADR already promised).

Extends: [ADR-0009](0009-per-agent-connector-auth.md) (credentials stay
per-agent, but move out of the sandbox), [ADR-0015](0015-credential-plane.md)
(the credential plane grows a proxy boundary), [ADR-0006](0006-security-rails-as-chart-defaults.md)
(default-deny egress remains a chart default).

## Context

This ADR merges two reviews that converged on the same primitive. The first was
a gap analysis of Curie against Anthropic's Slack-native "Claude Tag" agent;
the second was an audit of Curie's own egress and credential surface. Both
landed on the same missing component, so they are recorded here as one decision.

### What we have today

The design intent is fail-closed and mostly ahead of where a first look would
expect:

- **Default-deny egress.** `security.networkPolicy.allowedEgress` is empty on a
  fresh install; the chart renders `runner-default-deny-egress` with narrow
  carve-outs for DNS, the collector, MinIO, and in-cluster inference
  (`charts/curie/templates/security-networkpolicy.yaml`).
- **Named-provider egress, resolved at install.** `--allow-egress-host anthropic|openrouter`
  resolves hostnames to `/32` host routes at install time (ADR-0032), TLS-only on
  443. `--allow-web-egress <CIDR>` is the raw escape hatch.
- **A credential alone opens no egress.** An explicit, documented invariant
  (ADR-0032), the reverse of the usual footgun.
- **A name-based credential fence.** `RESERVED_BOOT_ENV` blocks a connector
  secret from being named `ANTHROPIC_API_KEY`, `HTTPS_PROXY`,
  `NODE_EXTRA_CA_CERTS`, etc., closing the redirect / TLS-MITM capture vectors.
- **A preflight that refuses to false-pass.** `preflight-networkpolicy.yaml`
  probes before and after and fails loudly if the CNI does not actually enforce
  NetworkPolicy.

### The three gaps that share one cause

1. **The allowlist is fail-open in practice (#765).** The vendored agent-sandbox
   controller emits its own `curie-runner-network-policy` granting
   `0.0.0.0/0` minus RFC1918. NetworkPolicy egress rules are a union, so that
   policy negates both our default-deny and the operator's allowlist. This is a
   precondition, tracked separately under #765 and ADR-0067; the proxy decision
   here assumes it is fixed and does not depend on the controller's policy for
   containment.

2. **No credential-to-domain binding.** Credentials are injected as plain env
   vars into the sandbox process (`binding.py`, `env[name] = value`), gated only
   by the name-based blocklist above. Nothing checks a credential's destination
   against `allowedEgress`, and the allowlist is release-wide, not per-agent, so
   agent A's connector host is reachable from agent B's pod with A's secret
   sitting in an env var readable by anything in the sandbox. ADR-0009 names the
   per-agent egress half as required; #440 item 4 shows it never shipped. The
   security review's SEC-C1 (a shared MinIO root credential readable inside the
   runner container via a shared `emptyDir`) is the concrete instance of this
   class.

3. **No package-manager story.** Zero special-casing of npm / pypi / crates.io /
   apt / ghcr. Operators must hand-resolve CIDRs for every registry, and because
   enforcement is CIDR-based while registry hosts are CDN-fronted with rotating
   IPs, the install-time-resolve trick that works for `api.anthropic.com` does
   not work for `files.pythonhosted.org`. An agent that cannot `pip install` is a
   demo, not a product.

### The model both reviews pointed at

Claude Tag separates two lists that Curie conflates into one:

- **Where traffic may go** (a bundle-level domain allowlist; a `*` entry is
  permitted here but never carries a credential, and `*` still blocks
  link-local / metadata endpoints).
- **Where a given credential may go** (a per-connection allowed-websites list).

The credential is held by a proxy, outside the sandbox; the agent process sees a
sentinel, and the proxy substitutes the real value only for the hosts bound to
that credential. Enforcement is a proxy everywhere, not a firewall: a blocked
request fails at the proxy's CONNECT stage
(`403`, `curl: (56) CONNECT tunnel failed`). ADR-0032's own deferral clause
already promised this: "The durable FQDN-based mechanism is tracked separately as
a future ADR; this ADR is the interim decision it will eventually supersede."
This is that ADR.

## Decision

Introduce **the Agent Proxy**: an in-path forward proxy on the runner sandbox's
egress path. All sandbox egress transits it; the sandbox holds no long-lived
credential and reaches nothing outbound except through it. It provides four
properties as one mechanism:

1. **FQDN-aware allowlisting.** The allow decision is made on hostname, not CIDR,
   so registry hosts and rotating-IP APIs are expressible as names. This
   supersedes ADR-0032's install-time CIDR resolve (kept only as the fallback for
   environments where the proxy is not deployed).

2. **Two separate lists, credential held at the boundary.** A domain allowlist
   ("where may traffic go") and a per-credential host binding ("where may this
   credential go"). The credential lives in a write-only store the proxy reads;
   it is **never placed in the sandbox**. The proxy injects it only on requests
   to that credential's bound hosts. This is the Claude Tag separation, adopted
   verbatim in shape.

3. **Per-agent scoping.** Both lists are scoped per agent, closing the
   release-wide-allowlist gap (#440 item 4). Agent A's credential and its bound
   hosts are invisible to agent B's sandbox.

4. **A complete audit trail.** Every CONNECT decision (allowed or blocked, with
   agent identity, destination host, and which credential if any was injected) is
   a durable, append-only event. Unlike Claude Tag's network-events export, which
   the reviewers showed excludes GitHub and MCP traffic and so leaves the agent's
   highest-impact actions untraced, the proxy sits in the path of **all** egress,
   MCP and git included. Full-coverage audit is a deliberate competitive line, and
   it is nearly free once every request already transits the proxy.

**Package-manager preset.** On top of the FQDN capability, ship a curated
"trusted registries" bundle (npm, pypi, crates.io, apt/ubuntu archives, ghcr,
and peers) that an operator can enable as one allowance rather than
hand-resolving CIDRs. It is domain-list-only and carries no credential, matching
Claude Tag's Trusted level.

### Why this is not the broker ADR-0009 rejected

ADR-0009 rejected "a networked secret broker/vault the runner calls at
tool-invocation time... a new trust boundary reached from inside the untrusted
sandbox path." The Agent Proxy is a different shape and the distinction is
load-bearing:

- The sandbox never calls the proxy to **fetch** a secret. There is no
  get-me-the-credential API reachable from inside the sandbox, so no new
  credential-exfiltration surface is exposed to untrusted code.
- The proxy is **in-path and transparent**: it injects on the wire, on the way
  out, for bound hosts only. The sandbox's view is a sentinel and an egress path,
  not a vault client.

The thing ADR-0009 feared (untrusted code holding a client that dispenses
secrets) is exactly what this design removes: today the secret is a plaintext env
var readable by any process in the sandbox; under the proxy it is not in the
sandbox at all.

## Honest caveats

- **Domain fronting.** Without TLS termination, the allow decision uses the
  client-supplied hostname (SNI / CONNECT target), which a determined agent can
  spoof to reach a different backend on a shared CDN. Anthropic documents this
  for Claude Tag rather than pretending otherwise; we inherit the same limit.
  Whether the proxy terminates TLS (stronger enforcement, at the cost of a
  MITM CA in the sandbox trust store and the ability to read plaintext traffic)
  is an open sub-decision, deferred to the implementing ADR below.
- **A new in-path dependency.** Every egress now depends on the proxy being up;
  its failure mode must be fail-closed (deny), consistent with the chart's
  default-deny posture, never fail-open.
- **This does not by itself fix #765.** The controller's union policy must be
  resolved first, or the proxy can be bypassed by direct egress the controller
  still permits.

## Future ADRs this one sets up

This ADR is the direction; the following are the extensions it anticipates, each
its own decision when it lands:

- **The proxy substrate and TLS-termination decision.** Sidecar vs mesh vs
  node-level; terminate TLS or CONNECT-only. Resolves the domain-fronting caveat
  one way or the other.
- **The write-only credential store and injection contract.** How credentials
  reach the proxy and how a per-credential host binding is declared in the bundle
  manifest.
- **The network-events audit export.** The durable schema and CLI surface for the
  full-coverage egress log, explicitly including MCP and git.
- **Untrusted-content tagging at the tool boundary.** Orthogonal to egress but
  from the same Claude Tag review: marking externally-fetched content (web,
  issues, files) as untrusted where a tool returns it, so a poisoned issue body is
  distinguishable from an operator instruction under the runner's
  `bypassPermissions` mode. Its own ADR.
- **Memory write-provenance and visibility scope.** Also from the review: ADR-0025
  memory is per-agent and shared across every user and thread, with no
  public/private split; a write-provenance field and a visibility scope belong in
  a follow-up.

## Status note

Accepted. This ADR supersedes ADR-0032's install-time CIDR resolve as the
durable FQDN mechanism. Substrate, TLS-termination, and the credential
injection contract remain the follow-up ADRs listed above; they are not pinned
by this acceptance.
