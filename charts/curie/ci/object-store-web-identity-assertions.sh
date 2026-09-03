#!/usr/bin/env bash
#
# Pointing the bundle store at a real cloud object store used to REQUIRE static
# access keys: the api, the worker, and the sandbox bundle-fetch init container
# each supplied explicit credentials unconditionally, so an ambient role was
# never consulted (#1325).
#
# Clearing `rustfs.auth.accessKey` now selects a key-free path: every credential
# env var is omitted so the AWS SDK falls through its provider chain to the
# web-identity provider, fed by a projected ServiceAccount token.
#
# Two properties make that safe, and both are asserted here rather than trusted:
#
#   * OMISSION IS COMPLETE. A half-omitted credential is worse than either
#     state. `AWS_ACCESS_KEY_ID` set to the empty string is not the same as
#     unset -- the SDK treats an empty explicit credential as a credential and
#     stops walking the chain, so the web-identity provider is never reached and
#     the fetch fails with a signature error rather than falling through.
#
#   * IMDS STAYS DENIED. The obvious workaround on AWS is the node's instance
#     role, and it must not be made to work. Rail 1 denies 169.254.169.254 by
#     construction and computes an `except` so a broad operator `allowedEgress`
#     CIDR cannot re-permit it. NetworkPolicy selects pods, not containers, so
#     opening IMDS for bundle-fetch would also open it for the runner -- a
#     prompt-injectable agent -- handing it the node's IAM role. That is
#     strictly worse than a bucket-scoped IAM user, so this asserts the deny
#     survives the key-free path.
#
# The static path is asserted unchanged alongside, because the failure this
# guards against is a regression in the DEFAULT install, not in the new one.
set -euo pipefail

CHART=${CHART:-charts/curie}

# The cleanup trap is installed BEFORE the first mktemp, not after the last one.
# Every command between the first temp file and the trap runs unprotected, and
# under `set -e` any of them can end the script -- a failed `helm template`, a
# full disk, an interrupted CI step -- leaking whatever had already been
# created. Declaring the names empty first keeps `set -u` happy, and the
# `${VAR:-}` expansions mean a trap that fires before a given mktemp ran passes
# `rm -f` an empty argument it ignores rather than an unbound variable.
STATIC_RENDERED=""
WEB_IDENTITY_RENDERED=""
REGION_OVERRIDE_RENDERED=""
WEB_IDENTITY_VALUES=""
WEB_IDENTITY_TOKEN=""
trap 'rm -f "${STATIC_RENDERED:-}" "${WEB_IDENTITY_RENDERED:-}" "${REGION_OVERRIDE_RENDERED:-}" "${WEB_IDENTITY_VALUES:-}" "${WEB_IDENTITY_TOKEN:-}"' EXIT

STATIC_RENDERED=$(mktemp)
WEB_IDENTITY_RENDERED=$(mktemp)
REGION_OVERRIDE_RENDERED=$(mktemp)
WEB_IDENTITY_VALUES=$(mktemp)
# Stands in for the projected ServiceAccount token the EKS pod-identity webhook
# mounts. Its CONTENT is never read here: the web-identity provider hands back
# deferred credentials and only exchanges the token at the first signed call, so
# a placeholder is enough to observe which provider won and this stays offline.
WEB_IDENTITY_TOKEN=$(mktemp)
printf 'placeholder-projected-service-account-token' > "$WEB_IDENTITY_TOKEN"

cat > "$WEB_IDENTITY_VALUES" <<'EOF'
rustfs:
  deploy: false
  host: s3.example.com
  port: 443
  auth:
    accessKey: ""
  # Dedicated runner allows for the BYO store and STS (#2213). TEST-NET-1
  # stands in for VPC interface-endpoint /32s. The broad allowedEgress list
  # below is a separate Rail 1 pin: it is the configuration in which the IMDS
  # carve-out is observable.
  egress:
    - cidr: 192.0.2.10/32
      ports: [{ protocol: TCP, port: 443 }]
  stsEgress:
    - cidr: 192.0.2.11/32
      ports: [{ protocol: TCP, port: 443 }]
# A key-free BYO store still has to be REACHABLE, so this overlay carries the
# broad allowlist such an install realistically sets. That is also the only
# configuration in which Rail 1's IMDS carve-out is observable: with an empty
# allowlist there is no ipBlock permitting anything, so default-deny covers the
# metadata address and there is nothing to except. The interesting case is
# precisely this one -- an operator opens egress wide, and the metadata address
# must stay denied anyway.
security:
  networkPolicy:
    allowedEgress:
      - cidr: 0.0.0.0/0
        ports: [{ protocol: TCP, port: 443 }]
api:
  serviceAccount:
    annotations:
      eks.amazonaws.com/role-arn: arn:aws:iam::000000000000:role/curie-api
worker:
  serviceAccount:
    annotations:
      eks.amazonaws.com/role-arn: arn:aws:iam::000000000000:role/curie-worker
agentSandbox:
  runner:
    serviceAccount:
      annotations:
        eks.amazonaws.com/role-arn: arn:aws:iam::000000000000:role/curie-runner
EOF

helm template curie "$CHART" --namespace dev > "$STATIC_RENDERED"
helm template curie "$CHART" --namespace dev \
  --values "$WEB_IDENTITY_VALUES" > "$WEB_IDENTITY_RENDERED"
helm template curie "$CHART" --namespace dev \
  --set rustfs.region=eu-west-1 > "$REGION_OVERRIDE_RENDERED"

# Assertion 1: clearing the access key while the in-chart RustFS is deployed is
# refused at render. That store is configured with those very credentials and
# has no web-identity path, so the combination would install green and then fail
# every bundle read and write -- the exact silent-misconfiguration shape this
# issue is about. Negative control: the refusal must fire, so a render that
# SUCCEEDS here is the failure.
if helm template curie "$CHART" --namespace dev \
  --set rustfs.auth.accessKey="" > /dev/null 2>&1; then
  echo "FAIL: clearing rustfs.auth.accessKey with rustfs.deploy=true must be refused" >&2
  exit 1
fi
echo "ok: an empty access key with the in-chart RustFS is refused at render"

# Assertion 2: the in-chart RustFS also requires its secret material. Empty
# secretKey must fail before installation, while an explicitly named external
# Secret is a legitimate source for that value and must still render.
if helm template curie "$CHART" --namespace dev \
  --set rustfs.auth.secretKey="" > /dev/null 2>&1; then
  echo "FAIL: clearing rustfs.auth.secretKey with rustfs.deploy=true and no existingSecret must be refused" >&2
  exit 1
fi
echo "ok: an empty secret key with the in-chart RustFS is refused at render"

if ! helm template curie "$CHART" --namespace dev \
  --set rustfs.auth.secretKey="" \
  --set rustfs.existingSecret=externally-managed-rustfs > /dev/null; then
  echo "FAIL: an existing RustFS Secret with an empty rustfs.auth.secretKey must render" >&2
  exit 1
fi
echo "ok: an existing RustFS Secret with an empty rustfs.auth.secretKey renders"

python3 - "$STATIC_RENDERED" "$WEB_IDENTITY_RENDERED" "$REGION_OVERRIDE_RENDERED" <<'PY'
import ipaddress, sys, yaml

CREDENTIAL_KEYS = ("S3_ACCESS_KEY", "S3_SECRET_KEY")
# The metadata address Rail 1 denies. An instance-role path would need this
# reachable; the web-identity path must not make it so.
IMDS = ipaddress.ip_address("169.254.169.254")


def env_for(container):
    return {entry["name"]: entry for entry in container.get("env", [])}


def read(path):
    api = worker = bundle_fetch = None
    fetch_command = ""
    service_accounts = {}
    egress_excepts = []
    with open(path) as fh:
        for doc in yaml.safe_load_all(fh):
            if not doc:
                continue
            kind = doc.get("kind")
            name = doc.get("metadata", {}).get("name", "")
            if kind == "Deployment":
                containers = doc["spec"]["template"]["spec"].get("containers", [])
                assert containers, f"{name} Deployment has no containers"
                if name.endswith("-api"):
                    api = env_for(containers[0])
                elif name.endswith("-worker"):
                    worker = env_for(containers[0])
            elif kind == "ServiceAccount":
                service_accounts[name] = doc["metadata"].get("annotations") or {}
            elif kind == "SandboxTemplate":
                pod_spec = doc["spec"]["podTemplate"]["spec"]
                matches = [
                    container
                    for container in pod_spec.get("initContainers", [])
                    if container.get("name") == "bundle-fetch"
                ]
                assert len(matches) == 1, (
                    "SandboxTemplate must render exactly one bundle-fetch init container"
                )
                bundle_fetch = env_for(matches[0])
                fetch_command = " ".join(matches[0].get("command", []))
            elif kind == "NetworkPolicy":
                for rule in doc.get("spec", {}).get("egress", []) or []:
                    for peer in rule.get("to", []) or []:
                        block = peer.get("ipBlock") or {}
                        egress_excepts.extend(block.get("except", []) or [])

    assert api is not None, "api Deployment not rendered"
    assert worker is not None, "worker Deployment not rendered"
    assert bundle_fetch is not None, "SandboxTemplate bundle-fetch not rendered"
    return api, worker, bundle_fetch, fetch_command, service_accounts, egress_excepts


def assert_static(path):
    api, worker, bundle_fetch, fetch_command, _, _ = read(path)
    for label, env in (("api", api), ("worker", worker), ("bundle-fetch", bundle_fetch)):
        missing = [key for key in CREDENTIAL_KEYS if key not in env]
        assert not missing, (
            f"the DEFAULT install must keep static credentials; {label} is missing {missing}. "
            "The in-chart RustFS accepts nothing else."
        )
    assert "AWS_ACCESS_KEY_ID" in fetch_command, (
        "the default bundle-fetch must still export AWS_ACCESS_KEY_ID"
    )
    print("ok: static: api, worker, and bundle-fetch all carry S3_ACCESS_KEY and S3_SECRET_KEY")


def assert_web_identity(path):
    api, worker, bundle_fetch, fetch_command, service_accounts, egress_excepts = read(path)

    # Omission is complete: not present, not empty-valued. An empty explicit
    # credential stops the provider chain just as a real one does, so it would
    # defeat the whole path while looking like it had been removed.
    for label, env in (("api", api), ("worker", worker), ("bundle-fetch", bundle_fetch)):
        present = [key for key in CREDENTIAL_KEYS if key in env]
        assert not present, (
            f"{label} still carries {present} on the key-free path. The env var must be "
            "ABSENT, not empty: the SDK treats an empty explicit credential as a credential "
            "and never reaches the web-identity provider."
        )

    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        assert name not in fetch_command, (
            f"bundle-fetch still exports {name} on the key-free path; the shell export is a "
            "second credential source the env-var check alone would not catch."
        )

    # The endpoint still has to be configured -- omitting credentials must not
    # omit the address, or the fetch fails for an unrelated reason.
    assert bundle_fetch.get("S3_ENDPOINT", {}).get("value") == "https://s3.example.com:443", (
        f"bundle-fetch lost its endpoint: {bundle_fetch.get('S3_ENDPOINT')}"
    )
    assert api.get("S3_ENDPOINT_URL", {}).get("value") == "https://s3.example.com:443", (
        f"api lost its endpoint: {api.get('S3_ENDPOINT_URL')}"
    )

    # The role binds on the ServiceAccount, so an annotation that does not render
    # means there is no way to name an identity at all.
    annotated = {
        name: annotations["eks.amazonaws.com/role-arn"]
        for name, annotations in service_accounts.items()
        if "eks.amazonaws.com/role-arn" in annotations
    }
    for suffix in ("-api", "-worker", "-runner"):
        matches = [name for name in annotated if name.endswith(suffix)]
        assert matches, (
            f"no ServiceAccount ending in {suffix} carries eks.amazonaws.com/role-arn. "
            "Without it the key-free path has no identity to assume."
        )
    print(f"ok: web-identity: {len(annotated)} ServiceAccounts carry a role-arn annotation")

    # Rail 1 must be untouched by any of the above. Assert CONTAINMENT rather
    # than a literal: the chart sizes the except to stay a strict subset of the
    # allowed CIDR, so a /0 allowlist yields the 169.254.0.0/16 link-local block
    # while a narrower one yields the /32 host. Both deny the metadata address;
    # string-matching one of them would fail the moment an operator narrowed
    # their allowlist, for no security reason.
    covering = [
        entry
        for entry in egress_excepts
        if IMDS in ipaddress.ip_network(entry, strict=False)
    ]
    assert covering, (
        f"no egress `except` covers {IMDS}; the rendered excepts were {egress_excepts}. "
        "Web identity reads a mounted token and needs no metadata access, so the key-free "
        "path must not have opened IMDS -- NetworkPolicy selects pods, so opening it for "
        "bundle-fetch opens it for the prompt-injectable runner too."
    )
    print(f"ok: web-identity: Rail 1 still excepts {IMDS} via {covering}; no instance-role path")
    print("ok: web-identity: no S3 credential env or shell export reaches any consumer")


def assert_bundle_fetch_region(path, expected_region):
    _, _, bundle_fetch, _, _, _ = read(path)
    rendered = bundle_fetch.get("AWS_DEFAULT_REGION", {}).get("value")
    assert rendered == expected_region, (
        f"bundle-fetch AWS_DEFAULT_REGION rendered as {rendered!r}; expected "
        f"{expected_region!r} from rustfs.region"
    )
    print(f"ok: bundle-fetch AWS_DEFAULT_REGION renders {expected_region}")


LANGFUSE_REGION_KEYS = (
    "LANGFUSE_S3_EVENT_UPLOAD_REGION",
    "LANGFUSE_S3_MEDIA_UPLOAD_REGION",
)


def langfuse_envs(path):
    """Langfuse web and worker env, selected by container name.

    The two Langfuse Deployment suffixes are more specific than `-worker`,
    which also matches langfuse-worker. Container name, not index, is the
    application container.
    """
    web = worker = None
    with open(path) as fh:
        for doc in yaml.safe_load_all(fh):
            if not doc or doc.get("kind") != "Deployment":
                continue
            name = doc.get("metadata", {}).get("name", "")
            containers = doc["spec"]["template"]["spec"].get("containers", [])
            if name.endswith("-langfuse-web"):
                matches = [c for c in containers if c.get("name") == "langfuse-web"]
                assert len(matches) == 1, (
                    f"{name} must render exactly one langfuse-web container, "
                    f"got {len(matches)}"
                )
                web = env_for(matches[0])
            elif name.endswith("-langfuse-worker"):
                matches = [c for c in containers if c.get("name") == "langfuse-worker"]
                assert len(matches) == 1, (
                    f"{name} must render exactly one langfuse-worker container, "
                    f"got {len(matches)}"
                )
                worker = env_for(matches[0])
    assert web is not None, "langfuse-web Deployment not rendered"
    assert worker is not None, "langfuse-worker Deployment not rendered"
    return web, worker


def assert_langfuse_upload_regions(path, expected_region):
    web, worker = langfuse_envs(path)
    for label, env in (("langfuse-web", web), ("langfuse-worker", worker)):
        for key in LANGFUSE_REGION_KEYS:
            rendered = env.get(key, {}).get("value")
            assert rendered == expected_region, (
                f"{label} {key} rendered as {rendered!r}; expected "
                f"{expected_region!r} from rustfs.region"
            )
    print(
        f"ok: langfuse event/media upload regions render {expected_region} "
        "on web and worker"
    )


assert_static(sys.argv[1])
assert_web_identity(sys.argv[2])
assert_bundle_fetch_region(sys.argv[1], "us-east-1")
assert_bundle_fetch_region(sys.argv[3], "eu-west-1")
assert_langfuse_upload_regions(sys.argv[1], "us-east-1")
assert_langfuse_upload_regions(sys.argv[2], "us-east-1")
assert_langfuse_upload_regions(sys.argv[3], "eu-west-1")
PY

# Everything above reads the rendered YAML. That is necessary and not
# sufficient: an absent env var only means the chart stopped SUPPLYING a
# credential, and the thing that actually has to happen is that the SDK then
# RESOLVES one from the web-identity provider. Those are different claims, and
# the gap between them is exactly where this feature was inert -- the manifests
# were already clean while the api crashlooped at boot, because the settings
# objects carry non-empty `rustfs`/`rustfssecret` DEFAULTS and the client
# builder passes them unconditionally. Omitting the env var just falls back to
# the default, and botocore reads that default as an explicit credential.
#
# So this second block stops asserting about fields and starts asserting about
# an outcome: build the real production objects under the env each rendered
# workload would actually get, and ask the constructed boto3 client which
# provider won. `.method` is that answer, and it is the only check here that
# cannot be satisfied by a rename or a moved field:
#
#   * "assume-role-with-web-identity" -- the chain was walked and the projected
#     token provider supplied the identity. This is the feature working.
#   * "explicit" -- a credential was handed to the client constructor, so
#     botocore short-circuited at the first resolver and never reached the
#     provider chain at all. An empty string counts: botocore treats
#     `aws_access_key_id=""` as a supplied credential, not an absent one, so
#     "we blanked the credential" is NOT the same fix as "we passed None".
#
# It goes through `BundleStore` rather than the shared `build_s3_client` helper
# on purpose. `BundleStore` is what production constructs; testing the helper
# alone would stay green if a store grew its own credential handling on top.
#
# Offline by construction: the web-identity provider returns deferred
# credentials and defers the STS exchange to the first signed request, which
# never happens here. No network, no cluster, no bucket.
uv run --python 3.13 python - "$STATIC_RENDERED" "$WEB_IDENTITY_RENDERED" "$WEB_IDENTITY_TOKEN" <<'PY'
import base64, os, sys, boto3, yaml

ROLE_ANNOTATION = "eks.amazonaws.com/role-arn"


def load_manifest(path):
    """Index the workloads, Secrets, and ServiceAccounts of one rendered chart."""
    deployments, secrets, service_accounts = {}, {}, {}
    with open(path) as fh:
        for doc in yaml.safe_load_all(fh):
            if not doc:
                continue
            kind = doc.get("kind")
            name = doc.get("metadata", {}).get("name", "")
            if kind == "Deployment":
                deployments[name] = doc
            elif kind == "Secret":
                values = {
                    key: base64.b64decode(encoded).decode()
                    for key, encoded in (doc.get("data") or {}).items()
                }
                values.update(doc.get("stringData") or {})
                secrets[name] = values
            elif kind == "ServiceAccount":
                service_accounts[name] = doc["metadata"].get("annotations") or {}
    return deployments, secrets, service_accounts


def deployment_for(deployments, name):
    """The Deployment by its exact rendered name.

    Named exactly rather than by suffix because the chart also renders
    `curie-langfuse-worker`, so a `-worker` suffix match picks up two workloads
    and whichever one document order happens to leave last.
    """
    assert name in deployments, (
        f"Deployment {name} was not rendered (rendered {sorted(deployments)})"
    )
    return deployments[name]


def container_named(label, deployment, container_name):
    """The container this workload actually runs, chosen by name, never by index.

    Indexing `containers[0]` reads whichever container the pod spec happens to
    list first, and that is not the same claim as "the application container".
    A sidecar prepended to the spec -- a proxy, a log shipper, a mesh injector
    -- would carry no S3 credential env at all, so both lanes would read a clean
    environment and pass while the real api or worker container kept its
    explicit keys. The gate would then assert about a container nobody stores a
    bundle from.

    Missing is a hard failure rather than a fallback to index 0 for the same
    reason: a silent fallback restores exactly the positional read this exists
    to remove, and a container rename is a real change to what the gate covers.
    """
    containers = deployment["spec"]["template"]["spec"].get("containers", []) or []
    matches = [c for c in containers if c.get("name") == container_name]
    assert len(matches) == 1, (
        f"{label}: Deployment {deployment['metadata']['name']} must run exactly one "
        f"container named {container_name!r}, but it renders "
        f"{[c.get('name') for c in containers]}. This gate asserts about the "
        "application container specifically, so it will not guess which one that is."
    )
    return matches[0]


def resolve_env(label, deployment, container_name, secrets):
    """The env the kubelet would hand this container, secretKeyRefs included.

    Reading only inline `value` entries is the trap here. On the static path the
    api and the worker carry `S3_ACCESS_KEY` inline but source `S3_SECRET_KEY`
    from a secretKeyRef, so a value-only reader produces an access key with no
    secret -- which botocore rejects with PartialCredentialsError, a failure
    that looks like a broken gate rather than the passing static lane it is.
    """
    container = container_named(label, deployment, container_name)
    resolved = {}
    for entry in container.get("env", []) or []:
        name = entry["name"]
        if "value" in entry:
            resolved[name] = str(entry["value"])
            continue
        source = (entry.get("valueFrom") or {}).get("secretKeyRef")
        if source is None:
            # fieldRef, resourceFieldRef and friends are pod metadata, never a
            # credential, so they cannot steer provider resolution.
            continue
        secret = secrets.get(source["name"])
        assert secret is not None, (
            f"{label} env {name} references Secret {source['name']}, which the chart "
            "did not render"
        )
        assert source["key"] in secret, (
            f"{label} env {name} references key {source['key']} of Secret "
            f"{source['name']}, which holds {sorted(secret)}"
        )
        resolved[name] = secret[source["key"]]
    return resolved


def resolve_role_arn(label, deployment, service_accounts):
    """The role ARN bound to THIS workload, via the ServiceAccount it names.

    Follows serviceAccountName rather than suffix-matching an account, because
    the api and the worker bind different roles and picking the wrong one would
    silently assert against an identity the workload never gets.
    """
    account = deployment["spec"]["template"]["spec"].get("serviceAccountName")
    assert account, f"{label} Deployment names no serviceAccountName"
    assert account in service_accounts, (
        f"{label} binds ServiceAccount {account}, which the chart did not render "
        f"(rendered {sorted(service_accounts)})"
    )
    arn = service_accounts[account].get(ROLE_ANNOTATION)
    assert arn, (
        f"ServiceAccount {account} carries no {ROLE_ANNOTATION} annotation, so {label} "
        "has no identity to assume"
    )
    return arn


def apply_env(manifest_env, overrides):
    """Scrub every ambient AWS_/S3_ key, then apply only manifest-derived values.

    Load-bearing, not hygiene. A developer box or a CI runner with an ambient
    AWS_PROFILE, an instance role, or a leftover AWS_ACCESS_KEY_ID would decide
    the provider chain instead of the chart, and the key-free assertion would
    then pass or fail for a reason that has nothing to do with this chart.
    """
    for key in [key for key in os.environ if key.startswith(("AWS_", "S3_"))]:
        del os.environ[key]
    for key, value in manifest_env.items():
        if key.startswith(("AWS_", "S3_")):
            os.environ[key] = value
    os.environ.update(overrides)


def api_client():
    from curie_api.config import Settings
    from curie_api.storage import BundleStore

    # `_env_file=None` is mandatory: Settings declares `env_file=".env"`, so a
    # stray dotenv in the checkout would otherwise supply credentials and quietly
    # decide the result of this gate.
    return BundleStore(Settings(_env_file=None))._client


def worker_client():
    from curie_worker.bundle_store import BundleStore
    from curie_worker.config import WorkerConfig

    # WorkerConfig declares no env file, so there is nothing to suppress.
    return BundleStore(WorkerConfig())._client


def credentials_of(label, client):
    credentials = client._request_signer._credentials
    assert credentials is not None, (
        f"{label} resolved no credentials at all; the provider chain was walked and "
        "came back empty"
    )
    return credentials


static_deployments, static_secrets, static_accounts = load_manifest(sys.argv[1])
free_deployments, free_secrets, free_accounts = load_manifest(sys.argv[2])
token_file = sys.argv[3]

# Release name is fixed at `curie` by the two `helm template` calls above, and
# the container name is spelled out beside the Deployment name rather than
# derived from the label, so a chart-side rename of either one fails loudly here
# instead of quietly re-pointing the gate at a different container.
WORKLOADS = (
    ("api", "curie-api", "api", api_client),
    ("worker", "curie-worker", "worker", worker_client),
)

# `boto3.DEFAULT_SESSION = None` before every client build is load-bearing, not
# defensive tidiness, and it is the half of the isolation `apply_env` cannot do.
# `build_s3_client` calls `boto3.client(...)`, which is a method on boto3's
# GLOBAL default session, and a session caches the credentials it resolves for
# the life of the process. All four clients below are built in ONE interpreter,
# so without this reset the first resolution wins and every later iteration is
# handed the cached credential object rather than resolving under the
# environment that iteration just applied. The whole point of scrubbing and
# reapplying the env per workload is that each lane resolves from scratch; a
# reused session silently turns the second, third, and fourth assertions into
# restatements of the first, and they would keep passing while asserting
# nothing. Dropping the reference forces boto3 to build a new session, and a new
# session starts with an empty credential cache. Verified by the objects: with
# the reset the api and worker resolve to distinct credential instances, without
# it they are the same object.

# Static lane. Same web-identity env present, so this also proves the key-free
# path is selected by the ABSENCE of configured keys and not by the ambient
# environment: a default install must keep signing with its own credentials even
# on a cluster where a role happens to be bound.
for label, workload, container_name, build_client in WORKLOADS:
    deployment = deployment_for(static_deployments, workload)
    manifest_env = resolve_env(label, deployment, container_name, static_secrets)
    expected_key = manifest_env["S3_ACCESS_KEY"]
    apply_env(
        manifest_env,
        {
            "AWS_ROLE_ARN": "arn:aws:iam::000000000000:role/curie-unused",
            "AWS_WEB_IDENTITY_TOKEN_FILE": token_file,
            "AWS_REGION": "us-east-1",
        },
    )
    boto3.DEFAULT_SESSION = None
    credentials = credentials_of(label, build_client())
    assert credentials.method == "explicit", (
        f"static: {label} resolved credentials via {credentials.method!r}; the default "
        "install signs with the in-chart RustFS keys, which no provider chain can supply"
    )
    # The method alone would still pass if the store signed with the wrong key,
    # so pin the value the manifest actually carries.
    assert credentials.access_key == expected_key, (
        f"static: {label} signs with access key {credentials.access_key!r}, but the "
        f"rendered manifest carries {expected_key!r}"
    )
    print(f"ok: static: {label} signs explicitly with the rendered access key")

# Key-free lane. AWS_ROLE_ARN and AWS_WEB_IDENTITY_TOKEN_FILE are the two vars
# the EKS pod-identity webhook injects; together they are the entire input to
# the provider this path depends on.
for label, workload, container_name, build_client in WORKLOADS:
    deployment = deployment_for(free_deployments, workload)
    apply_env(
        resolve_env(label, deployment, container_name, free_secrets),
        {
            "AWS_ROLE_ARN": resolve_role_arn(label, deployment, free_accounts),
            "AWS_WEB_IDENTITY_TOKEN_FILE": token_file,
            "AWS_REGION": "us-east-1",
        },
    )
    boto3.DEFAULT_SESSION = None
    method = credentials_of(label, build_client()).method
    assert method == "assume-role-with-web-identity", (
        f"key-free: {label} built a client whose credentials resolved via {method!r}, "
        "not 'assume-role-with-web-identity'. 'explicit' means a credential was passed "
        "to the client constructor, so botocore stopped at the first resolver and the "
        "web-identity provider was never reached -- the key-free path is inert no "
        "matter how clean the rendered manifest looks. Note that an empty-string "
        "credential is still explicit to botocore; the constructor has to receive no "
        "credential at all."
    )
    print(f"ok: key-free: {label} resolves credentials via {method}")
PY

echo
echo "PASS: the default install keeps static object-store credentials and still signs with the access key its manifest carries; clearing rustfs.auth.accessKey omits every credential env and shell export across api, worker, and bundle-fetch, binds identity through ServiceAccount annotations, is refused against the in-chart RustFS, leaves Rail 1's IMDS denial intact, and makes the api and worker BundleStore objects resolve real credentials through the web-identity provider rather than an explicit key."
