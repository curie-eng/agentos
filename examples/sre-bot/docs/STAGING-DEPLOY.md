# What actually runs in staging, and how to reproduce it

This bundle is not deployable to a cluster as it stands, and the thing running in
our staging cluster is this bundle plus four deltas applied by hand. That gap is
why a reviewer could interrogate the live bot and then find nothing in the
repository that reproduces it. This file closes that gap: it records the deltas,
why each exists, and the exact steps.

Written after the fact from a working deployment rather than from intent, so every
value here was read off the cluster.

**The host is a single-node k3s cluster**, not a managed one, which is why the
chart-hook and registry-access notes below bite here and may not bite on yours.
Do not confuse it with the cluster the bot *reports on*: that is a separate,
larger cluster the read-only connector points at.

**Two placeholders**, kept out of the repository because they name someone's
private deployment rather than this project (`.gitleaks.toml`'s
`downstream-repo-slug` rule):

| placeholder | what it is |
|---|---|
| `<ARCHIVED_ORG>` | the org publishing the archived predecessor repository's images -- the same org as this repo; operators of this install already know it |
| `<ns>` | the release namespace of the install you are reproducing into |

## Why the bundle is not directly deployable

```
kubernetes   image   ghcr.io/containers/kubernetes-mcp-server@sha256:6d650f4b...
k8s-write    build   connectors/k8s-write
k8s-scale    build   connectors/k8s-scale
grafana      image   docker.io/grafana/mcp-grafana@sha256:5efeafd0...
tempo        build   connectors/tempo
```

Three connectors declare `build:`. `curie build --plugin-dir <dir>` without
`--registry` records a local image id, which is usable at the skill and local
tiers and **refused at the cluster tier** — a cluster cannot pull an image that
exists only in one machine's Docker daemon. So a cluster deploy of this bundle
needs either a registry to push to, or images already published.

`.claude-plugin/plugin.json` also declares two gates:

```
mcp__k8s-write__restart_deployment
mcp__k8s-scale__scale_deployment
```

A gate naming a connector the deploy does not carry fails bundle validation, so
dropping a connector means dropping its gate in the same edit.

## The four deltas

### 1. `k8s-scale` and its gate are removed

No published image for it, so it cannot be deployed at the cluster tier without
building and pushing one first. Its gate goes with it, or validation fails.

This means **the live bot has one write verb, not two.** A reviewer asking "what
write actions can you take" gets `restart_deployment` alone, and that is correct
for what is deployed rather than a limitation of the bundle.

### 2. `k8s-write` and `tempo` are pinned to published images

```
k8s-write  ghcr.io/<ARCHIVED_ORG>/sre-bot-k8s-write-mcp:sha-21130cbacf4f1df9e13d98b9b3141a68b73db48c
tempo      ghcr.io/<ARCHIVED_ORG>/sre-bot-tempo-mcp:sha-3cf3bf30c968d22a0de67f9c69bf1cdd26d4f46d
```

These are the images built from the archived predecessor repository this bundle
was extracted from, and they are what staging has been running. They are
pullable; the `build:` contexts in this bundle are their source, but the two are
not verified byte-identical here.

**For a fresh install, do not pin the tempo one.** This repository publishes its
own tempo connector image (`ghcr.io/curie-eng/curie-sre-bot-tempo`, built by the
`Build sre-bot-tempo image` CI job), and `curie example sre-bot install` resolves
that to a digest for you. The pin above is recorded because it is what *this*
install is running, not because it is what a new one should run. No equivalent
in-repo image exists for `k8s-write`, which is why that one still points at the
archived org.

### 3. The write allowlist carries real targets

The bundle ships the sanctioned placeholder:

```
K8S_WRITE_ALLOWLIST: <namespace>/<deployment>
```

A real install sets one entry per Deployment the bot may restart, comma
separated, all in the namespace Curie itself runs in:

```
K8S_WRITE_ALLOWLIST: <namespace>/<namespace>-api,<namespace>/<namespace>-worker,<namespace>/<namespace>-dispatcher
```

**This value and `manifests/write-role.yaml`'s `resourceNames` must be edited
together.** They are two allowlists over the same question in two files. On this
install they disagreed for four days: the Role was re-scoped by hand when the box
was rebuilt, while the connector kept a target from the previous cluster
(`public/api`). The intersection was empty, so the write path was inert and
nothing reported it. `scripts/check-write-path-gated.py` is the check for exactly
this and should be run before any deploy that touches either file.

### 4. `deploy.yaml` is removed before deploying

The shipped `deploy.yaml` carries placeholder Slack channel ids
(`C0EXAMPLE1`, `C0EXAMPLE2`). Deploying with it can rebind an existing agent to a
channel that does not exist, and the failure is silent — the bot simply stops
answering, with nothing in any log naming the cause. Delete the file, or pass
`--target` only when the ids are real.

## Reproduction

Assumes a Curie release in `<ns>` with the observability stack from
`examples/sre-bot/observability/` already installed, and `manifests/read-access.yaml`
applied.

### Step 1 — build the deployable bundle

```bash
cp -R examples/sre-bot /tmp/bundle
rm -f /tmp/bundle/deploy.yaml
python3 - <<'PY'
import yaml, json, pathlib
ALLOW = "<ns>/<deploy-a>,<ns>/<deploy-b>"          # your real targets
IMAGES = {
    "k8s-write": "ghcr.io/<ARCHIVED_ORG>/sre-bot-k8s-write-mcp:sha-21130cbacf4f1df9e13d98b9b3141a68b73db48c",
    "tempo":     "ghcr.io/<ARCHIVED_ORG>/sre-bot-tempo-mcp:sha-3cf3bf30c968d22a0de67f9c69bf1cdd26d4f46d",
}
p = pathlib.Path("/tmp/bundle/connectors.yaml")
d = yaml.safe_load(p.read_text()); c = d["connectors"]
for name, image in IMAGES.items():
    c[name].pop("build", None); c[name]["image"] = image
c["k8s-write"]["env"]["K8S_WRITE_ALLOWLIST"] = ALLOW
c.pop("k8s-scale", None)                            # no published image
p.write_text(yaml.safe_dump(d, sort_keys=False))

m = pathlib.Path("/tmp/bundle/.claude-plugin/plugin.json")
j = json.loads(m.read_text())
j["approvalPolicy"]["gates"] = [g for g in j["approvalPolicy"]["gates"]
                                if "k8s-scale" not in g["gate"]]
m.write_text(json.dumps(j, indent=2))
PY
```

Then edit `/tmp/bundle/manifests/write-role.yaml` so its namespace and
`resourceNames` match `ALLOW` exactly, and verify before going further:

```bash
python3 scripts/check-write-path-gated.py     # expects the bundle under examples/
```

### Step 2 — the write identity

```bash
kubectl apply -f /tmp/bundle/manifests/write-role.yaml
kubectl auth can-i patch deployments/<deploy-a> -n <ns> \
  --as=system:serviceaccount:<ns>:sre-bot-writer          # expect: yes
kubectl auth can-i delete deployments -n <ns> \
  --as=system:serviceaccount:<ns>:sre-bot-writer          # expect: no
kubectl auth can-i get secrets -n <ns> \
  --as=system:serviceaccount:<ns>:sre-bot-writer          # expect: no
```

### Step 3 — the Grafana credential

The bundle reads the Grafana token from a Secret named `curie-grafana-connector`:

```yaml
secrets:
  - name: GRAFANA_SERVICE_ACCOUNT_TOKEN
    from_secret: curie-grafana-connector
    key: GRAFANA_SERVICE_ACCOUNT_TOKEN
```

That Secret is created by the chart's `grafanaConnector` hook, which **does not
exist in chart 0.7.0**. On an older release, create it by hand or the connector
pods fail with `CreateContainerConfigError: secret "curie-grafana-connector" not
found` — and note that the *previous* ReplicaSet keeps serving, so
`kubectl get deploy` still reads `1/1` while the new pods never start. Check pod
names and ages, not the ready column.

Mint a token against the Grafana the bot should observe (its own cluster's, for a
self-observing install), then:

```bash
kubectl create secret generic curie-grafana-connector -n <ns> \
  --from-literal=GRAFANA_SERVICE_ACCOUNT_TOKEN="$TOKEN"
```

`GRAFANA_URL` in the bundle already points at
`http://grafana.observability.svc.cluster.local`. If a deployment overrides it to
a different Grafana, the token must match that Grafana — changing the URL without
the token gives a connector that starts and cannot authenticate.

### Step 4 — deploy

```bash
curie cluster deploy --plugin-dir /tmp/bundle \
  --agent <agent> --env <dev|prod> \
  --namespace <ns> --release <release>
```

**`--env` defaults to `dev`.** Deploying a prod-environment agent without
`--env prod` creates a version that is never applied: the command prints
`deployed <agent> ... -> dev` and `[dev] active`, the connector reconciler reports
`applied, 0 failed`, and the running connectors keep their old configuration. The
only way to catch it is to read the connector env afterwards. `--target` normally
supplies this, which is why removing `deploy.yaml` makes it explicit.

### Step 5 — verify, by reading the cluster rather than the deploy output

```bash
kubectl get deploy -n <ns> <release>-<agent>-mcp-k8s-write \
  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="K8S_WRITE_ALLOWLIST")].value}'
kubectl get role -n <ns> sre-bot-writer -o jsonpath='{.rules[0].resourceNames}'
kubectl get pods -n <ns> | grep mcp-        # names and ages, not just ready counts
```

The allowlist and the `resourceNames` must name the same set. If they do not, the
write path is either inert or will 403 after a human approves a call.

## What removes all of this

Every delta above is hand work that a tool should do. PR #1923 adds
`curie example sre-bot install --write-allowlist ns/name[,ns/name]`, which renders
the Role's `resourceNames` and the connector's `K8S_WRITE_ALLOWLIST` from one
input — making their disagreement unrepresentable rather than merely detectable —
keeps exactly the gate for the connector it keeps, and mints the writer kubeconfig
the same way the reader one is minted. Once that lands, Steps 1 through 4 collapse
into one command and this file describes history.

The remaining delta it does not cover is `k8s-scale`: it needs a published image
before any cluster deploy can carry it.

## Verified after the fact

Both of the things this file originally left open were checked.

**The pinned `k8s-write` image is behaviourally the same code as this bundle's
source.** Its `/app/server.py` matches the archived repository's file exactly
(`md5 3c9b4919573ba43e62b79ae86a8d5c35`) and differs from
`connectors/k8s-write/server.py` here (`md5 217a4eb009a1f21fa5c964e557e26687`) by
thirteen lines: one docstring cross-reference, and two long strings wrapped for
line length. The allowlist check, the constant patch body, and the
`insecure-skip-tls-verify` refusal are identical. So reading the source in this
directory tells you what the running connector does — but the digests above remain
the authority for what actually ran.

**`k8s-scale` has no published image.** Confirmed by pulling from the cluster that
does have registry access:

```
없음  ghcr.io/curie-eng/curie-sre-bot-k8s-scale:0.8.0
없음  ghcr.io/<ARCHIVED_ORG>/sre-bot-k8s-scale-mcp:latest
없음  ghcr.io/curie-eng/curie-sre-bot-k8s-scale:latest
```

The same run pulled `sre-bot-k8s-write-mcp` successfully, so this is an absent
image rather than a credentials problem. Until one is published, no cluster deploy
can carry that connector, and the bundle's second gate has nothing to guard.
