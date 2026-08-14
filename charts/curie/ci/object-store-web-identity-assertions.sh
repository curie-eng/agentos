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
STATIC_RENDERED=$(mktemp)
WEB_IDENTITY_RENDERED=$(mktemp)
WEB_IDENTITY_VALUES=$(mktemp)
trap 'rm -f "$STATIC_RENDERED" "$WEB_IDENTITY_RENDERED" "$WEB_IDENTITY_VALUES"' EXIT

cat > "$WEB_IDENTITY_VALUES" <<'EOF'
rustfs:
  deploy: false
  host: s3.example.com
  port: 443
  auth:
    accessKey: ""
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

python3 - "$STATIC_RENDERED" "$WEB_IDENTITY_RENDERED" <<'PY'
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


assert_static(sys.argv[1])
assert_web_identity(sys.argv[2])
PY

echo
echo "PASS: the default install keeps static object-store credentials; clearing rustfs.auth.accessKey omits every credential env and shell export across api, worker, and bundle-fetch, binds identity through ServiceAccount annotations, is refused against the in-chart RustFS, and leaves Rail 1's IMDS denial intact."
