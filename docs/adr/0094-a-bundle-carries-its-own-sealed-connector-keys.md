# 94. A bundle carries its own sealed connector keys

Date: 2026-08-04

Status: Accepted

## Context

Issue #1240, and the last manual step in onboarding an agent.

[ADR 0090](0090-a-reconciler-applies-connectors-so-agent-repos-need-no-cli.md),
[ADR 0091](0091-git-flow-resolves-deploy-targets-so-one-repo-serves-many-agents.md)
and [ADR 0092](0092-a-github-app-gives-the-platform-its-own-repository-identity.md)
together got an agent repository down to agent logic. The first adopting agent
repository now holds skills,
`connectors.yaml`, `deploy.yaml` and evals — 464 lines of deploy plumbing and
an AWS role that could run shell on the cluster node are gone.

One thing still is not in the repository: the credential a connector needs.

`connectors.yaml` can name a secret, but the VALUE has to be put on the cluster
out of band, by an operator running `curie cluster deploy`. Observed live on
both of that repository's agents:

```
acme-bot     operator-credential connector : True
             protected from pruning : [('Secret', 'acme-bot-acme-bot-connector-secrets')]
acme-dev     operator-credential connector : True
```

That protection is not a feature. `connector_agent.reconcile_agent` has to
special-case the Secret and hide it from the plan, because the reconciler's own
rule — own it and no longer declare it, therefore delete it — would otherwise
prune a credential nothing declares. A workaround for a gap, carried in the
code path most able to cause damage.

It also breaks the property [ADR 0086](0086-the-release-is-the-scope.md) exists
for: a cluster rebuilt from the repository does not come back, because the
credential was never in the repository to begin with.

The obvious fix is not available. A credential cannot be committed in the
clear, and the repository is exactly where an agent author expects to declare
what their agent needs.

## Decision

**A bundle may carry connector credentials encrypted to the cluster that will
run them, and Curie decrypts them at reconcile time.**

```yaml
# connectors.yaml
grafana:
  image: grafana/mcp-grafana:0.17.2
  sealed_secrets:
    GRAFANA_TOKEN: AgBv3n2K...      # only this cluster can decrypt
```

This is the Sealed Secrets model, and it is chosen because the property it
gives is the one this ADR needs: **a committed blob is useless to anyone
without the cluster's private key**, so the credential can live in the
repository — public or private — beside the connector that uses it.

Each installation generates a keypair at install. The public half is published
so authors can seal against it; the private half never leaves the cluster.
`curie seal --connector grafana GRAFANA_TOKEN` does the encrypting, so nobody
hand-rolls it and no author needs to know the format.

**Sealed to a cluster, not to an agent.** An agent cannot hold a key — it is a
row in a database and a pod that comes and goes. The isolation that matters is
already elsewhere: a decrypted value is mounted only into its own agent's
Secret, under the agent-scoped naming (#1116), and the reconciler provably
never crosses agents. Sealing per agent would add a key-management problem and
buy nothing the owner label does not already give.

**A blob that will not decrypt fails loudly and changes nothing.** The
reconciler skips that agent, reports why, and leaves the running connector
alone. The tempting alternative — deploy the connector without the credential —
produces a pod that starts, passes its health check, and 401s every call. That
is the #1156 failure shape exactly: healthy in `kubectl get pods`, broken for
every user.

**The operator-supplied path stays.** A credential the platform must not see in
a repository at all, an existing install, a connector whose secret comes from
External Secrets — all still work. This adds a way to declare a credential; it
does not remove the way that exists.

## Consequences

**This changes a frozen contract.** `sealed_secrets` is a new field on
`ConnectorSpec` in `packages/plugin-format`, which `AGENTS.md` requires to land
as its own reviewed, backward-compatible change before anything depends on it.
It is additive and optional, so an existing bundle is unaffected — but the
schema, the generated TypeScript and Rust, and the CLI's mirror declaration all
move together or the lanes drift.

An agent repository becomes fully self-contained: clone it, point a cluster at
it, and the agent comes up with its tools working. That is the whole arc of
0090 through 0092 finished.

The prune-protection special case in `reconcile_agent` can eventually go, once
no agent relies on an operator-supplied secret. Not immediately — both of that
repository's agents do today — but the code path stops being permanent.

**A sealed blob is only as good as the key rotation story, and rotation here is
worse than for the GitHub App.** An App holds several keys at once, so rotation
is generate-deploy-delete with no downtime. A cluster keypair cannot work that
way: rotating it invalidates every blob every agent repository has committed,
and each must be resealed and pushed. This ADR does not solve that. It is the
strongest argument against the whole approach and the thing to get right in the
implementation — at minimum, support two active keys so a rotation can overlap,
and make `curie seal` able to reseal a repository in one command.

**Losing the private key loses every credential sealed to it.** Unlike the App
key, which is regenerable in two clicks because GitHub holds the identity,
nothing outside the cluster can reconstruct this one. The install must say so,
and the key belongs in whatever the operator already uses for durable secrets —
which, per `charts/curie/README.md`, is exactly what `existingSecret` is for.

## Alternatives considered

- **Keep operator-supplied secrets only.** Rejected: it is the status quo, and
  it leaves the prune-protection special case in the reconciler permanently
  plus a cluster that cannot be rebuilt from its repositories.
- **A reference to an external secret manager** (`from_secret_manager: arn:...`)
  instead of a sealed value. Not rejected — this is genuinely good, and for an
  adopter already running External Secrets it is better than sealing. But it
  requires every adopter to run one, which a two-person team spinning up their
  first agent will not. Worth adding later as a second form alongside sealing.
- **Encrypt with the GitHub App key**, avoiding a second keypair. Rejected:
  it conflates two identities with different lifetimes and blast radii, and it
  would make App key rotation — currently zero-downtime and safe — silently
  destroy every sealed credential.
- **Store the credential on the agent row via the API** and have `deploy.yaml`
  reference it. Rejected for ADR 0086's reason: state the repository cannot see
  means a rebuilt cluster does not reproduce, and the change is invisible in
  review.
