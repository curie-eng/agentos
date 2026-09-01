# What the tree actually installs, and how to reproduce it

The reproduction is one command. It used to be this bundle plus four deltas
applied by hand; that gap is why a reviewer could interrogate a live bot and
then find nothing in the repository that reproduced it. The installer has
landed, so this file describes what `curie example sre-bot install` does
today, including the two upgrade paths the previous write-up never named.

```bash
curie example sre-bot install --observability
```

`--observability` is required. With no targeting flags, Curie lands as release
`curie` in namespace `curie`, and the Grafana/Loki/Alloy/Tempo/Prometheus stack
lands in namespace `observability`. To install beside an existing release:

```bash
curie example sre-bot install --observability \
  --namespace <ns> \
  --release <release> \
  --observability-namespace <obs-ns>
```

`--dry-run` prints the same plan without mutating the cluster. Read that
output before accepting `--platform-upgrade`: it is the one listing that
names the widest identity this installer can create.

The installer copies a runtime bundle out of the checked-in tree. It does
**not** apply `deploy.yaml` as routing. Agent name comes from `plugin.json`;
Slack binding comes from `--slack-channel` if you pass it. `curie cluster
deploy --plugin-dir examples/sre-bot --target ...` is a different path and
is the one that reads `deploy.yaml`.

## What the default install keeps

| Connector     | Default install                                              |
|---------------|--------------------------------------------------------------|
| `kubernetes`  | kept, read-only                                              |
| `grafana`     | kept, pointed at the bundled stack                           |
| `tempo`       | kept, pinned to the published `curie-sre-bot-tempo` digest   |
| `k8s-write`   | kept, with an empty `K8S_WRITE_ALLOWLIST`                    |
| `k8s-scale`   | stripped                                                     |
| `self-upgrade`| stripped                                                     |

`k8s-write` ships declared, not commented out. With no `--write-allowlist`
the connector is kept with an empty `K8S_WRITE_ALLOWLIST`. Empty means
the process **refuses to start**, not that a healthy connector refuses
every call: `connectors/k8s-write/server.py` exits 1 when the allowlist
is empty, so the writer pod CrashLoopBackOffs until you name targets.
**No Role is rendered.** An empty `resourceNames` would grant every
Deployment, so the empty case omits the Role rather than rendering one
with no names. The writer identity is still minted, because bring-up
refuses without `K8S_WRITE_KUBECONFIG`.

`k8s-scale` stays out even though a published image now exists. Scale to
zero is an outage, and that blast radius is its own opt-in, not a ride on
the write path.

`self-upgrade` stays out for a stronger reason: its identity holds
namespace-wide `create` on `jobs` in the namespace that holds the platform
API key. That grant is an operator decision made while reading
`manifests/upgrade-role.yaml`, never a side effect of the default install.

The installer also applies `manifests/read-access.yaml` and mints the
read-only kubeconfig the `kubernetes` connector needs.

## The write allowlist

```bash
curie example sre-bot install --observability \
  --write-allowlist <ns>/<deploy-a>,<ns>/<deploy-b>
```

One input renders both ceilings: `manifests/write-role.yaml`'s
`resourceNames` and the connector's `K8S_WRITE_ALLOWLIST`. They are two
allowlists over the same question in two files, and editing one without
the other is not hypothetical. Deriving both from one flag makes that
disagreement unrepresentable.

`--no-write` strips the connector and its gate together. `--no-write` and
`--write-allowlist` contradict each other; the command refuses rather than
picking one.

`scripts/check-write-path-gated.py` remains the check for a hand-edited
bundle that did not go through this flag.

## The upgrade path: `--platform-upgrade`

This is the part the previous write-up omitted. Passing the flag keeps the
`self-upgrade` connector and installs the **platform** upgrade Job:

```bash
curie example sre-bot install --observability --platform-upgrade
```

That does four extra things, after the deploy, never before:

1. Applies `manifests/upgrade-role.yaml`. This is the **connector**
   identity (`sre-bot-upgrader`): `create` on `jobs`, so the bot can start
   an upgrade. It cannot run `helm upgrade` itself.
2. Applies `manifests/platform-upgrade-role.yaml`. This is the **Job**
   identity (`curie-platform-upgrader`): namespace-admin in all but name,
   because `helm upgrade` rewrites nearly every object the release owns.
   Read that file before accepting the flag. The sandbox never sees this
   credential; it exists for the ~90s an upgrade runs.
3. Applies a ConfigMap whose body is `platform-upgrade/upgrade.sh`, and a
   CronJob (`suspend: true`) that runs it. The CronJob never fires on its
   own. The gated tool `mcp__self-upgrade__upgrade_platform` creates a Job
   from that template when a human approves one.
4. Mints `SELF_UPGRADE_KUBECONFIG` from the connector identity and
   reconciles it onto the deployed version.

The same connector also publishes `mcp__self-upgrade__upgrade_self`, which
starts a Job from this bot's own CronJob template
(`self-upgrade/cronjob.yaml`) rather than the platform one. The installer
does not apply `self-upgrade/cronjob.yaml`. Without that hand apply,
`upgrade_self` is a gated tool pointed at a CronJob that is not there.
`--platform-upgrade` is what installs `upgrade_platform`; reproducing
`upgrade_self` still means applying `self-upgrade/cronjob.yaml` yourself
after reading `manifests/upgrade-role.yaml`.

`--dry-run` lists those four applies. If they are not in the plan, the
flag was not passed.

The script the Job runs is `platform-upgrade/upgrade.sh`. It takes no
target version from the bot: it reads the newest published release of the
source repository, refuses when that is already installed, and does not
roll back. Rollback is an operator action; `docs/PERMISSION-MAP.md`
entry 4 says why.

## Slack

```bash
curie example sre-bot install --observability --slack-channel <channel-id>
```

Bind by channel ID, never by `#name`. The dispatcher routes by ID, so a
name binds nothing and the deploy still reports success.

`deploy.yaml` used to ship documentation placeholder ids (`C0EXAMPLE1`,
`C0EXAMPLE2`) as live `slack_channel` values. Those match the Slack id
shape, so `curie cluster deploy --target` reported success and rebound
the bot to a channel that does not exist. The shipped file no longer
carries live `slack_channel` lines. Uncomment them only with a real id,
and only when you are using `--target` / `--all-targets` rather than this
installer.

## What a hand deploy still has to do

The installer is the cluster-tier path. A manual
`curie cluster deploy --plugin-dir examples/sre-bot` of the checked-in
tree is a different shape:

- Four connectors declare `build:` (`k8s-write`, `k8s-scale`,
  `self-upgrade`, `tempo`). `curie build --plugin-dir` without
  `--registry` records a local image id, which the cluster tier refuses.
  The installer resolves published digests instead.
- `.claude-plugin/plugin.json` declares gates for every write the bundle
  names. A gate naming a connector the deploy does not carry fails bundle
  validation, so dropping a connector means dropping its gate in the same
  edit. The installer does that pairing; a hand edit must too.
- Both kubeconfigs must be available (`K8S_READONLY_KUBECONFIG` and, if
  the write connector is kept, `K8S_WRITE_KUBECONFIG`). The installer
  mints those as one-off secret overrides and does not persist them to
  the host vault, so a later manual deploy still needs them supplied.

`--env` defaults to `dev`. Deploying a prod-environment agent without
`--env prod` creates a version that is never applied. `--target` supplies
this when you use `deploy.yaml`; the installer does not.

## Historical staging cluster

The original write-up of this file was read off a single-node k3s host
that was running a hand-patched copy of this bundle: `k8s-scale` removed,
`k8s-write` and `tempo` pinned to images from an archived predecessor
repository, a real write allowlist typed into two files by hand, and
`deploy.yaml` deleted so its placeholder channel ids could not rebind
the bot.

Do not copy those deltas onto a fresh install. The installer is the
source of truth for what this tree deploys: published images under
`ghcr.io/curie-eng/curie-sre-bot-*`, one allowlist flag, and
`--platform-upgrade` for the two upgrade identities.

The chart-hook note from that host still bites on an older release: the
Grafana connector Secret named `curie-grafana-connector` is created by
the chart's `grafanaConnector` hook, which did not exist in chart 0.7.0.
On such a release the connector pods fail with
`CreateContainerConfigError` while the previous ReplicaSet keeps serving,
so `kubectl get deploy` still reads `1/1`. Check pod names and ages, not
the ready column.
