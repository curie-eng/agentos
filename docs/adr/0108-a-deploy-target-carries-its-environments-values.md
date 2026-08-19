# 108. A deploy target carries its environment's values

Date: 2026-08-15

Status: Draft

Evidence: the environment-coupling issue filed against a downstream SRE agent
bundle, and the partial fix that landed in that bundle's own repository. Both
are cited by number in this ADR's pull request, and are deliberately not linked
here, per the `downstream-repo-slug` rule: this repository is the platform, and
the repositories that use it are somebody's private deployment.

## Context

[ADR 0089](0089-bundles-declare-their-deploy-targets.md) states the constraint
this ADR exists to make true: **the artifact stays identical across targets, only
the binding differs.** That is what lets prod promote the exact bundle dev
validated. A target today binds three things (`agent`, `env`, `slack_channel`)
and nothing else.

A downstream SRE agent bundle is the first bundle to deploy somewhere other than
the installation it was written against, and the constraint did not hold. Three
couplings were found by deploying the unmodified bundle to a scratch k3s cluster
on 2026-08-14:

| coupling | file | what breaks |
|---|---|---|
| prod Grafana URL, in two connector blocks | `connectors.yaml` | connectors come up pointing at prod, or the bundle must be edited |
| prod datasource UIDs and instance catalogue | the agent's `SKILL.md` | the agent burns turns rediscovering them elsewhere |
| prod-only content as eval anchors | `evals/cases.json` | 9 to 11 of 12 cases structurally red off prod, so the promotion gate cannot run pre-prod |

Two of the three were fixable inside the bundle's own repository, and were, in a
follow-up there. The skill now names the UIDs as *this* install's with a
`list_datasources` fallback, and the eval cases declare a `requires` tier so a
portable subset can be selected. The third was not fixable there, and the reason
is precise rather than a matter of effort.

`connector_render.py` substitutes exactly four placeholders into a connector's
`args` and `env`:

```
${CURIE_ALLOWED_HOSTS} ${CURIE_CONNECTOR_HOST} ${CURIE_CONNECTOR_PORT} ${CURIE_CONNECTOR_URL}
```

All four derive from the Service Curie itself creates. `_PLACEHOLDER_RE` matches
`\$\{(CURIE_[A-Z0-9_]*)\}`, and anything outside that closed set is rejected at
validation as `connectors.unknown_placeholder`. So there is no in-bundle
expression for "this value differs per target". `deploy.yaml` cannot supply one
either: a target names an agent and a channel, not connector configuration.

The status quo is therefore a committed comment at the offending line, saying
which line to edit per install. That is a documented edit to the artifact, which
is exactly what ADR 0089 said must not be required, and it fails the same three
ways ADR 0089 enumerated. An edited bundle is no longer the artifact that
passed. The edit is invisible in review. And a *missing* edit deploys
successfully while pointing a scratch cluster's agent at production Grafana.

The last of those is the one that matters. Every failure mode here is silent.

## Decision

**A deploy target declares the values its environment binds, and those values
substitute into the bundle at render.** The bundle stays byte-identical.

```yaml
# deploy.yaml
targets:
  prod:
    agent: acme-bot
    env: prod
    slack_channel: C0EXAMPLE1
    values:
      GRAFANA_URL: https://grafana.example.com
  scratch:
    agent: acme-scratch
    env: dev
    values:
      GRAFANA_URL: http://grafana.monitoring.svc:3000
```

```yaml
# connectors.yaml: unchanged shape, one new placeholder family
connectors:
  grafana:
    image: grafana/mcp-grafana:0.17.2
    env: {GRAFANA_URL: "${CURIE_VALUE_GRAFANA_URL}"}
    secrets: [GRAFANA_SERVICE_ACCOUNT_TOKEN]
```

Five constraints define the decision.

**The placeholder family is `CURIE_VALUE_*`, inside the existing lexer.**
`_PLACEHOLDER_RE` already matches the whole `CURIE_*` namespace, so only the
closed-set membership check changes: from "one of four" to "one of four, or a
declared target value". No new syntax, no second substitution pass, and a bundle
that references a value nobody declared still fails as
`connectors.unknown_placeholder` does today.

**Every target must declare every referenced key.** Validation fails the whole
file, naming the target and the key, when one target omits a key another
supplies. There is deliberately **no default and no inheritance**: a default is
how a scratch deploy silently inherits the production URL, which is the exact
failure this ADR exists to remove. The cost, that adding one value means editing
every target, is accepted and is the point.

**Values are non-secret, and the validator enforces it.** Credentials keep the
existing path: `secrets:` names, resolved to a `secretKeyRef` the platform never
reads (ADR 0086, ADR 0009). A key that also appears in any connector's
`secrets:` list is rejected, because `deploy.yaml` is committed and a value
placed there is published. Nothing here becomes a second way to carry a
credential.

**Flags stay overrides.** `--set-value KEY=VALUE` wins over the target, so a
one-off deploy to a scratch cluster never requires committing a target. This is
the same rule ADR 0089 set for `--agent`, `--env`, and `--slack-channel`, and
ADR 0097 set for `curie.yaml`.

**One parser, where `deploy.yaml` is already parsed.** `values` is a field on
the existing `DeployTarget` model in `packages/plugin-format`, read through
`read_deploy_targets`. Substitution happens in `connector_render`, which already
receives the agent a target binds. Nothing new reads the file.

Absent `values`, nothing changes. The field is additive, exactly as
`deploy.yaml` itself was.

## Consequences

`deploy.yaml` now holds two kinds of fact: where the bundle goes, and what
differs where it goes. They are close enough to belong together, since both
change when the deployment topology changes and both are dead when the target is
deleted. But the file is no longer purely about routing, and it will attract
pressure to absorb configuration that is not per-environment. The non-secret
rule and the no-default rule are what keep it from becoming a general settings
file.

The promote-what-you-validated guarantee becomes checkable rather than
aspirational. A bundle with no remaining per-install edits is one whose dev and
prod artifacts are provably the same bytes, and `--dry-run` can print the
resolved values alongside the resolved binding.

Every target declaring every key is verbose by construction, and gets worse with
target count. That is the deliberate trade for removing the silent-inherit
failure. If it becomes genuinely painful at scale, the answer is a later
decision about explicit inheritance with an explicit opt-in per key, not a
default added quietly to this one.

Values render into a Deployment's env and are readable by anyone who can read
the namespace or the repository. The secret-collision check catches the obvious
mistake. It does not catch a value that is sensitive without being declared a
secret anywhere. Documented, not solved.

**This ADR does not fix the eval half of that issue.** Parameterizing an anchor
value is in scope here. Deciding *which cases may run against which environment*
is a different fact, and the bundle's follow-up had to invent an undeclared
`requires` field because Curie's eval case schema has no expression for it. That
is its own
decision, and it should be made against the schema-compatibility rules in
[ADR 0101](0101-schema-compatibility-for-closed-schemas.md) rather than folded
in here.

`SKILL.md` values stay out of scope for the same reason a skill is prose: there
is no render step to substitute into, and the bundle's own fix (name the values
as this install's, and give the agent a discovery fallback) is the right shape for
a document an agent reads rather than a template a platform expands.

## Alternatives rejected

**A per-environment values overlay file (`values.prod.yaml`).** Rejected. It
separates a value from the target that selects it, so nothing structurally
forces every target to define every key. The file a target forgot is simply
absent, and absence is how the silent inherit comes back. It also duplicates
ADR 0089's selection mechanism: `--target` already names one environment, and a
second selector for the same axis is a second thing to get out of sync. Helm's
`-f` is the same argument ADR 0097 already rejected, one level down.

**Per-target env inside `connectors.yaml`.** Rejected. That file is *what the
agent needs wherever it runs*. Keying its contents by target makes it
unparseable without knowing where you are deploying, which is the split ADR 0089
drew and ADR 0086 drew before it. It also puts topology next to a container
image, the same objection ADR 0089 raised against collapsing the two files.

**Put the values in `curie.yaml` (ADR 0097).** Rejected. `curie.yaml` is an
operator artifact describing an *installation*. A bundle deployed to four
installations would have its configuration spread across four operators' files,
none of them reviewable in the bundle repository. ADR 0097 explicitly refuses to
absorb bundle concerns for the mirror-image reason.

**Carry the URL as a Secret.** Rejected. It works, since `secrets:` already
resolves per install without touching the bundle, but it works by declaring a
non-secret to be a credential. That teaches every future author that
per-environment configuration is spelled "Secret", degrades the signal of the
secrets list, and points rotation tooling at values that never rotate.

**Do nothing, and document the line to edit.** Rejected. It is the current state
after that follow-up, and it is a documented instruction to modify the artifact
between dev and prod. Documentation does not diff, and the failure mode of
skipping the edit is a scratch cluster talking to production.
