# 86. Bundles declare connectors; the platform hosts them

Date: 2026-07-29
Status: Draft

## Context

Issue #1063. `.mcp.json` lets a bundle **declare** an MCP server. Nothing in
Curie **runs** one.

| Declaration | Declarable | Runnable |
|---|---|---|
| stdio (`command` + `args`) | yes | only if the binary is baked into the runner image -- the read-only rootfs blocks a runtime install |
| remote (`type` + `url`) | yes | yes, but the bundle author hosts it and wires its egress themselves |

`runner/Dockerfile` states the intended path for the stdio case:

```dockerfile
RUN npm install -g @modelcontextprotocol/server-github
# `"command": "mcp-server-github"` -- see examples/github-issues. Add a line here
```

Adding a connector therefore means editing the platform's Dockerfile: a Curie
change, and a runner-image release, per integration. GitHub is the only server
baked in; there is no catalog.

### What this costs a bundle author, measured

Building a read-only Grafana bot end to end produced a bundle repository of:

```
the agent itself   184 lines   SKILL.md, evals/cases.json, .mcp.json, plugin.json
platform plumbing  524 lines   GitHub Actions, k8s Deployment/Service/NetworkPolicy
```

Of that plumbing, **184 lines were Kubernetes written solely to run one MCP
server** -- a Deployment, a Service, a Secret reference, container hardening,
probes, and a NetworkPolicy. Three times more platform code than product code,
and every future bundle author writes their own slightly different copy.

Two defects in that hand-written YAML, both non-obvious and both hit in
practice:

1. **A NetworkPolicy naming a Service ClusterIP never matches.** kube-proxy
   DNATs the destination to a pod IP before NetworkPolicy is evaluated
   (netfilter runs `nat` before `filter`), so an `ipBlock` of the ClusterIP is
   dead on arrival. The symptom is a bare connection refused. Worse, it
   *appeared* to work on minikube, whose default CNI has no NetworkPolicy
   controller -- so the broken rule and a correct rule were indistinguishable
   until the bundle reached a cluster that enforces.
2. **`mcp-grafana` 0.17 validates the `Host` header** and defaults its allowlist
   to loopback variants of `--address`, so an in-cluster caller reaching it by
   Service DNS gets `forbidden: host not allowed`. The author must know to pass
   `-allowed-hosts` listing every name the sandbox might dial.

Neither is something an agent author should need to know exists. Both are
properties of *how Curie runs a connector*, which is precisely the knowledge the
platform should hold once rather than every bundle rediscovering.

### Why this is not solved by shipping more integrations

The MCP ecosystem grows faster than any platform team can track, and a large
share of what teams connect will be internal servers Curie will never know
about. A per-service integration list is a treadmill; a generic mechanism is
not. Service-specific knowledge belongs in an `examples/` bundle, not in the
platform.

## Decision

**A bundle declares the connectors it needs in a file; Curie realises them.**

Declaration is a file rather than a CLI verb. The bundle is already four
declarative files (`plugin.json`, `SKILL.md`, `.mcp.json`, `evals/cases.json`);
a `curie connect` verb would hide state the repository cannot see, breaking the
property that a bundle is reproducible from its own contents.

```yaml
# connectors.yaml
connectors:
  # Curie runs this container and wires it up.
  grafana:
    image: grafana/mcp-grafana:0.17.2
    args: [-t, streamable-http, -disable-write, -disable-api]
    env:
      GRAFANA_URL: https://grafana.example.com
    secrets: [GRAFANA_SERVICE_ACCOUNT_TOKEN]

  # Or point at something already running.
  internal-thing:
    url: https://mcp.internal/mcp
    headers:
      Authorization: "Bearer ${INTERNAL_TOKEN}"
    secrets: [INTERNAL_TOKEN]
```

Twelve lines replacing 184, none of them Kubernetes. From the hosted form Curie
derives the Deployment, the Service, the container hardening, the host
allowlist, the sandbox-to-connector NetworkPolicy, the secret injection, and the
URL it writes into the agent's MCP configuration.

Load-bearing consequences of that derivation:

- **The ClusterIP trap becomes unreachable.** Curie generates the egress policy
  with a `podSelector`, never an `ipBlock`, because it knows which pods it just
  created. An author cannot express the broken form.
- **The host allowlist is derived, not authored.** Curie names the Service, so
  Curie can pass `-allowed-hosts` correctly for connectors that enforce it.
- **Hardening is the default.** Non-root, read-only rootfs, dropped
  capabilities, resource bounds -- applied by the platform, not remembered by
  each author.
- **Secrets keep the existing model.** The file names secrets; values are
  supplied at deploy time exactly as ADR-0009 already specifies. No new concept.
- **Adding a connector is a reviewable diff.** A pull request shows that the
  agent gained access to a new system. With a CLI verb that change is invisible
  to code review.

## Consequences

Curie takes on reconciling connector specs into cluster objects, which is real
new surface: a controller or a deploy-time expansion, lifecycle for the objects
it creates, and a failure mode when an image will not pull.

The parity ladder needs an answer at every tier. `cluster` hosts a Deployment;
`local` hosts a container; `skill` cannot host anything and must report a
connector as *declared but not exercisable here* rather than red (#1093). The
skill tier's `--network none` offline contract is unchanged.

Bundles keep working unchanged: `.mcp.json` remains valid, and `connectors.yaml`
is additive.

This is the substrate for scoped Kubernetes access (#1096) rather than an
independent cleanup. If connector hosting exists, giving an agent read-only
cluster access is a `connectors.yaml` entry plus a ClusterRole -- the credential
lives in the connector pod and the sandbox never holds it, which is exactly the
property that makes the Grafana connector's read-only guarantee hold today (a
write attempt with the agent's token returns 403 server-side). Write access
later is a second connector whose tools are listed in
`approval_required_tools`, so the existing Slack approval cards gate it. That
turns the write-access roadmap into configuration rather than new platform work.

It also resolves #1060 without a bespoke feature: an agent reading its own
Langfuse traces becomes one more connector entry.

## Alternatives considered

- **A `curie connect <service>` CLI verb.** Rejected: it stores connector state
  somewhere the bundle repository cannot see, so a rebuilt cluster does not
  reproduce from the repo, and the change is invisible in review. The bundle
  being wholly declarative is the property worth keeping.
- **Bake more servers into the runner image.** Rejected: it is the current path,
  it requires a platform change and image release per integration, and it cannot
  serve internal servers at all.
- **Document the Kubernetes an author must write.** Rejected: the two defects
  above were both hit by someone who knew Kubernetes, and one of them was
  invisible on the most common local cluster. Documentation does not remove an
  error class that the platform can make unrepresentable.
- **Require every connector to be remote (`url` only), hosted by the team out
  of band.** Rejected: it pushes the same 184 lines into a different repository
  and leaves the sandbox-egress wiring, which is the part Curie is uniquely
  positioned to get right, still hand-written.
