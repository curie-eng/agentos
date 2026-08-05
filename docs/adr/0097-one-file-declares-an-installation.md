# 97. One file declares an installation

Date: 2026-08-05

Status: Accepted

## Context

A bundle already declares almost everything about itself in files that live
beside its source. [ADR 0086](0086-bundles-declare-connectors-the-platform-hosts-them.md)
moved the tools it needs into `connectors.yaml`, and
[ADR 0089](0089-bundles-declare-their-deploy-targets.md) moved where it goes
into `deploy.yaml`, both on the same principle: a reviewable diff beats flags
scattered across whatever invoked the command.

The **installation** never got that treatment. Standing up the platform that
hosts those bundles is still expressed entirely as argv. This is the real
command from a working deployment's runbook:

```bash
curie cluster up --namespace acme-bot --release acme-bot \
  --set nameOverride=acme-bot \
  --set ui.deploy=false --set inference.deploy=false \
  --set agentSandbox.runner.fakeModel=false \
  --set agentSandbox.runner.credentials="$ANTHROPIC_KEY" \
  --set api.githubToken="$FINE_GRAINED_PAT" \
  --set 'security.networkPolicy.allowedEgress[0].cidr=160.79.104.0/23' \
  --set 'security.networkPolicy.allowedEgress[0].ports[0].protocol=TCP' \
  --set 'security.networkPolicy.allowedEgress[0].ports[0].port=443'

curie cluster comms --slack --namespace acme-bot --release acme-bot \
  --app-token "$SLACK_APP_TOKEN" --bot-token "$SLACK_BOT_TOKEN"
```

Four properties of that shape are load-bearing problems, not style complaints:

1. **It is not reviewable.** The egress allowlist is a security control. It
   exists only in a runbook code block and a shell history, so widening it
   leaves no diff and no reviewer.
2. **It is not reproducible from the repository.**
   [ADR 0086](0086-bundles-declare-connectors-the-platform-hosts-them.md) holds
   that the repo must reproduce a cluster. For the bundle it does. For the
   installation hosting it, the source of truth is a human's terminal.
3. **The order is undocumented and load-bearing.** `comms` must follow `up`.
   The runbook says so in prose because nothing in the interface can.
4. **`--reuse-values` has already caused three production incidents** in this
   repo — a float-coerced App ID (#1253), a dropped GitHub App on `up`
   (#1256), and dropped comms settings. Every one is the same root cause:
   `up` cannot see the whole intended state, only the fragment re-passed on
   this invocation. A file that always states the whole intent removes the
   class, rather than patching each instance.

Helm's own `-f values.yaml` solves none of this: it covers the chart's values
but not `--namespace`, `--release`, the Slack tokens, the deploy target, or the
ordering between verbs — and it exposes chart internals as the user's
interface, which is precisely what `curie` exists to wrap.

## Decision

Introduce **`curie.yaml`**: one file that declares an installation, and one
verb that reconciles a cluster to it.

```yaml
version: 1

install:
  namespace: acme-bot
  release: acme-bot

platform:
  ui: false
  inference: false
  egress:
    - host: anthropic          # named hosts, not hand-written CIDRs
    - host: slack

credentials:                    # NAMES only; values resolved at apply time
  model: ANTHROPIC_API_KEY
  github_app:
    id: GITHUB_APP_ID
    private_key: GITHUB_APP_PRIVATE_KEY

comms:
  slack:
    app_token: SLACK_APP_TOKEN
    bot_token: SLACK_BOT_TOKEN
```

```bash
curie apply -f curie.yaml           # converges the cluster to this file
curie apply -f curie.yaml --dry-run # the plan, no mutation
curie diff  -f curie.yaml           # what differs from the live release
```

Four constraints define the decision:

- **`curie.yaml` carries secret NAMES, never values.** The same rule
  `connectors.yaml` already follows. The file is committed; resolution is
  environment or `curie secrets` at apply time. A committed installation file
  that could hold a token would be worse than the flags it replaces.
- **The file states whole intent; apply computes the delta.** Ordering
  (`up` before `comms`) becomes an implementation detail of the planner rather
  than a rule the operator must remember. Anything absent from the file is
  absent from the cluster — which is what makes `--reuse-values`' whole failure
  class unreachable.
- **Exactly one parser, in the CLI**, mirroring ADR 0089's rule for
  `deploy.yaml`. `curie.yaml` describes an *installation*, which only the CLI
  performs; it must not become a second thing the API also reads.
- **Flags keep working and win over the file.** This is additive. A one-off
  `--set` for a scratch cluster stays a one-off, and a released binary's
  behaviour does not change for anyone who never writes the file.

`curie.yaml` describes the **installation**. `deploy.yaml` and
`connectors.yaml` continue to describe the **bundle**, unchanged, and this ADR
does not merge them: they live in the agent repository and are read by the API
and worker, while `curie.yaml` is an operator artifact read by the CLI. Merging
them would put a cluster's Slack tokens' names into every agent repo that
deploys to it.

## Consequences

An installation becomes reviewable. Widening the egress allowlist is a pull
request, which is what a security control needs.

ADR 0086's "the repo must reproduce a cluster" becomes true of the platform and
not just the bundle. A lost laptop stops being a recovery event.

The `--reuse-values` incident class ends by construction rather than by
remembering to re-pass each family, which is what #1253 and #1256 each fixed
one at a time.

Onboarding gets a file to read instead of a command to decode. A new operator
can see the whole installation at once — the stated goal.

Costs, honestly: a second config concept for users to learn, and the CLI grows
a planner that must be kept honest against the chart. The `--dry-run`/`diff`
pair is not a convenience here — without it the planner is unfalsifiable, and
this repo has already been bitten by plans that looked right and deleted eight
live objects. `curie diff` against a cluster stood up by flags must also report
honestly rather than proposing to delete what it did not create.

## Alternatives considered

**Just use `helm -f values.yaml`.** Rejected. It covers chart values only —
not namespace, release, tokens, or verb ordering — and makes chart internals
the user's interface, which is the wrapping `curie` exists to provide.

**Extend `deploy.yaml` to cover the installation.** Rejected. `deploy.yaml`
lives in the agent repository and is parsed by the API (ADR 0089). Installation
concerns are operator concerns; folding them in would leak a cluster's
configuration into every agent repo deployed to it, and would break ADR 0089's
single-parser rule.

**Generate the file from a live cluster (`curie export`).** Not rejected —
deferred. It is the obvious migration aid for existing installs and should be
its own decision once the schema has settled, rather than shipping a serializer
for a shape still under review.

**Do nothing; document the flags better.** Rejected. #1296 documents them,
which helps a reader and changes nothing about reviewability, reproducibility,
or the `--reuse-values` class.
