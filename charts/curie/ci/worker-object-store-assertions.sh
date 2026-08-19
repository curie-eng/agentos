#!/usr/bin/env bash
#
# The API WRITES each uploaded bundle to the object store and the worker READS it
# back. If the two disagree about where that store is, the write succeeds, the
# read fails, and the failure surfaces nowhere near its cause.
#
# This was a real outage. worker.yaml set none of the four variables, so the
# worker fell back to its compose default (http://localhost:29000) and every
# bundle fetch got ECONNREFUSED. The post-deploy eval silently stopped running
# with "unresolvable suite/bundle", which alerts nothing, because a suite that
# cannot resolve looks exactly like a suite nobody asked for.
#
# So this asserts AGREEMENT, not presence. Presence alone would still pass the
# day someone points the worker at a different-but-populated endpoint.
#
# The key set is DERIVED from the API's rendered env rather than listed here, so
# a fifth object-store variable added to api.yaml alone is a failure instead of
# being invisible. Every secretKeyRef is resolved against the rendered Secret,
# because a reference identically wrong on both sides passes an equality check
# and leaves both pods in CreateContainerConfigError.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART="${CHART:-$(cd "$SCRIPT_DIR/.." && pwd)}"
EXTERNAL_VALUES=$(mktemp)
trap 'rm -f "$EXTERNAL_VALUES"' EXIT

cat > "$EXTERNAL_VALUES" <<'EOF'
rustfs:
  deploy: false
  host: s3.example.com
  port: 443
EOF

# Capture Helm output in Python so a large manifest cannot be truncated by a
# shell command substitution or redirected into the script's stdin.
python3 - "$CHART" "$EXTERNAL_VALUES" <<'PY'
import copy
import io
import subprocess
import sys
from contextlib import redirect_stdout

import yaml

CHART, EXTERNAL_VALUES = sys.argv[1], sys.argv[2]


def render(*values):
    command = ["helm", "template", "curie", CHART, "--namespace", "dev"]
    for path in values:
        command += ["--values", path]
    try:
        rendered = subprocess.run(command, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as error:
        print("FAIL: helm template could not render the chart", file=sys.stderr)
        if error.stderr:
            print(error.stderr, file=sys.stderr, end="")
        raise SystemExit(error.returncode)
    return [doc for doc in yaml.safe_load_all(rendered.stdout) if doc]


def assert_external_store_requires_hostname():
    command = [
        "helm",
        "template",
        "curie",
        CHART,
        "--namespace",
        "dev",
        "--set",
        "rustfs.deploy=false",
    ]
    rendered = subprocess.run(command, capture_output=True, text=True)
    if rendered.returncode == 0:
        print(
            "FAIL: rustfs.deploy=false without rustfs.host must fail Helm rendering",
            file=sys.stderr,
        )
        raise SystemExit(1)

    diagnostic = rendered.stderr + rendered.stdout
    if "hostname" not in diagnostic.lower():
        print(
            "FAIL: the missing rustfs.host error must say that a hostname is required",
            file=sys.stderr,
        )
        if diagnostic:
            print(diagnostic, file=sys.stderr, end="")
        raise SystemExit(1)

    print("ok: an external object store without rustfs.host is rejected as missing a hostname")


def value_source(entry):
    return {key: entry[key] for key in ("value", "valueFrom") if key in entry}


def has_usable_source(entry):
    return bool(entry.get("value")) or bool(entry.get("valueFrom"))


def container_env(docs, kind, component, container_name, init=False):
    """Select by the component LABEL and the container NAME.

    `endswith("-worker")` also matches `<release>-curie-langfuse-worker`, which
    renders earlier and legitimately has no object-store env -- so a suffix
    match silently inspected Langfuse and reported the curie worker as
    misconfigured while the chart was correct. The label is unambiguous, and the
    container name keeps a sidecar rendered first from being inspected instead
    of the application container.
    """

    for d in docs:
        if d.get("kind") != kind:
            continue
        if d["metadata"].get("labels", {}).get("app.kubernetes.io/component") != component:
            continue
        pod_template = "template" if kind == "Deployment" else "podTemplate"
        pod_spec = d["spec"][pod_template]["spec"]
        containers = pod_spec.get("initContainers" if init else "containers", [])
        matches = [c for c in containers if c.get("name") == container_name]
        assert len(matches) == 1, (
            f"{kind} {component} must render exactly one container named "
            f"{container_name}, got {len(matches)}"
        )
        return matches[0].get("env", [])
    return None


def env_of(docs, kind, component, container_name, init=False):
    env = container_env(docs, kind, component, container_name, init)
    return None if env is None else {entry["name"]: entry for entry in env}


def object_store_keys(env):
    return {name for name in env if name.startswith("S3_") or name == "BUNDLE_BUCKET"}


def assert_contract(docs, label, expected_endpoint):
    for d in docs:
        if d.get("kind") == "Deployment":
            assert d["spec"]["template"]["spec"].get("containers"), (
                f"{d['metadata']['name']} Deployment has no containers"
            )

    api = env_of(docs, "Deployment", "api", "api")
    worker = env_of(docs, "Deployment", "worker", "worker")
    bundle_fetch = env_of(
        docs, "SandboxTemplate", "agent-sandbox", "bundle-fetch", init=True
    )
    assert api is not None, "api Deployment not rendered"
    assert worker is not None, "worker Deployment not rendered"
    assert bundle_fetch is not None, "SandboxTemplate bundle-fetch not rendered"

    failures = []

    required_keys = {"S3_ENDPOINT_URL", "S3_ACCESS_KEY", "S3_SECRET_KEY", "BUNDLE_BUCKET"}
    for k in sorted(required_keys - api.keys()):
        failures.append(f"api is missing object-store key {k}")

    # The API is the writer and defines the full object store configuration, so
    # the key set is read off its rendered env. This keeps a newly introduced S3
    # setting from being invisible to this gate.
    keys = object_store_keys(api)
    worker_keys = object_store_keys(worker)

    for source, env, env_keys in (("api", api, keys), ("worker", worker, worker_keys)):
        for k in sorted(env_keys):
            if not has_usable_source(env[k]):
                failures.append(f"{source} {k} has neither a usable value nor valueFrom")

    for k in sorted(keys - worker_keys):
        failures.append(
            f"worker is missing {k}. Its config defaults these to the compose stack "
            "(http://localhost:29000), so every bundle fetch fails with ECONNREFUSED."
        )
    for k in sorted(worker_keys - keys):
        failures.append(f"worker has {k}, but api does not")

    # Equality, not merely presence. Two different but present endpoints is the
    # subtler version of the same bug and would pass a presence only check.
    for k in sorted(keys & worker_keys):
        if value_source(api[k]) != value_source(worker[k]):
            failures.append(
                f"api and worker disagree on {k}:\n"
                f"      api    = {value_source(api[k])}\n"
                f"      worker = {value_source(worker[k])}\n"
                "    The API writes bundles and the worker reads them, so a difference here "
                "means the write lands somewhere the read never looks."
            )

    # A secretKeyRef that names a Secret or key the chart never renders leaves the
    # pod in CreateContainerConfigError. Identically wrong on both sides, it is
    # indistinguishable from a correct reference to the equality check above.
    for source, env, env_keys in (
        ("api", api, keys),
        ("worker", worker, worker_keys),
        (
            "bundle fetch",
            bundle_fetch,
            {k for k in ("S3_ENDPOINT", "BUNDLE_BUCKET") if k in bundle_fetch},
        ),
    ):
        for k in sorted(env_keys):
            ref = env[k].get("valueFrom", {}).get("secretKeyRef")
            if not ref:
                continue
            name, key = ref.get("name"), ref.get("key")
            secret = next(
                (
                    d for d in docs
                    if d.get("kind") == "Secret"
                    and d.get("metadata", {}).get("name") == name
                ),
                None,
            )
            if secret is None:
                failures.append(f"{source} {k} secretKeyRef names missing Secret {name!r}")
                continue
            secret_keys = set(secret.get("data", {})) | set(secret.get("stringData", {}))
            if key not in secret_keys:
                failures.append(
                    f"{source} {k} secretKeyRef names missing key {key!r} in Secret {name!r}"
                )

    # The sandbox bundle-fetch init container reads the same store under a
    # different variable name, and on Kubernetes it is what actually fetches the
    # bundle on the turn path.
    for api_key, fetch_key in (
        ("S3_ENDPOINT_URL", "S3_ENDPOINT"),
        ("BUNDLE_BUCKET", "BUNDLE_BUCKET"),
    ):
        if fetch_key not in bundle_fetch:
            failures.append(f"bundle fetch is missing {fetch_key}")
            continue
        if not has_usable_source(bundle_fetch[fetch_key]):
            failures.append(
                f"bundle fetch {fetch_key} has neither a usable value nor valueFrom"
            )
        for source, env in (("api", api), ("worker", worker)):
            if api_key in env and value_source(env[api_key]) != value_source(
                bundle_fetch[fetch_key]
            ):
                failures.append(
                    f"bundle fetch {fetch_key} disagrees with {source} {api_key}:\n"
                    f"      bundle fetch = {value_source(bundle_fetch[fetch_key])}\n"
                    f"      {source} = {value_source(env[api_key])}"
                )

    for source, env, key in (
        ("api", api, "S3_ENDPOINT_URL"),
        ("worker", worker, "S3_ENDPOINT_URL"),
        ("bundle fetch", bundle_fetch, "S3_ENDPOINT"),
    ):
        if env.get(key, {}).get("value") != expected_endpoint:
            failures.append(
                f"{label} {source} endpoint must be {expected_endpoint}, got {env.get(key)}"
            )

    # The compose default must never be what a Kubernetes pod receives.
    ep = worker.get("S3_ENDPOINT_URL", {}).get("value", "")
    if "localhost" in ep or "127.0.0.1" in ep:
        failures.append(f"worker S3_ENDPOINT_URL points at the pod itself: {ep!r}")

    # The credential must be a reference, never inline: helm keeps its values in
    # the release Secret, so an inline password is readable in every retained
    # revision.
    entry = worker.get("S3_SECRET_KEY", {})
    ref = entry.get("valueFrom", {}).get("secretKeyRef")
    if "value" in entry:
        failures.append("worker S3_SECRET_KEY must not be an inline value")
    if not ref:
        failures.append("worker S3_SECRET_KEY must come from a secretKeyRef")

    if failures:
        print(f"FAIL: {label}: api/worker object-store configuration diverges\n")
        for f in failures:
            print("  - " + f)
        raise SystemExit(1)

    def shown(env_entry):
        if "value" in env_entry:
            return env_entry["value"]
        secret_ref = env_entry.get("valueFrom", {}).get("secretKeyRef")
        if secret_ref:
            return f"<secretKeyRef {secret_ref.get('name')}/{secret_ref.get('key')}>"
        return "<no value>"

    print(f"ok: {label}: api and worker agree on {', '.join(sorted(keys))}")
    for k in sorted(keys):
        print(f"      {k:18} = {shown(api[k])}")
    print(f"ok: {label}: all S3 endpoints are {expected_endpoint}")
    print(f"ok: {label}: S3_SECRET_KEY is a ref -> {ref['name']}/{ref['key']}")


def expect_failure(docs, messages):
    captured = io.StringIO()
    try:
        with redirect_stdout(captured):
            assert_contract(docs, "default probe", "http://curie-rustfs:9000")
    except SystemExit as error:
        assert error.code == 1
    else:
        raise AssertionError("object store contract probe unexpectedly passed")

    output = captured.getvalue()
    for message in messages:
        assert message in output, f"missing diagnostic {message!r} in:\n{output}"
    for unsupported in ("Traceback", "ClaimTimeoutError", "Slack turn", "CPU"):
        assert unsupported not in output, f"unsupported diagnostic {unsupported!r} in:\n{output}"


assert_external_store_requires_hostname()
default_docs = render()
assert_contract(default_docs, "default", "http://curie-rustfs:9000")

missing_api_key = copy.deepcopy(default_docs)
api_env = container_env(missing_api_key, "Deployment", "api", "api")
assert api_env is not None, "api Deployment was not rendered"
api_env[:] = [entry for entry in api_env if entry.get("name") != "S3_ACCESS_KEY"]
expect_failure(
    missing_api_key,
    (
        "api is missing object-store key S3_ACCESS_KEY",
        "worker has S3_ACCESS_KEY, but api does not",
    ),
)

three_divergences = copy.deepcopy(default_docs)
api_env = container_env(three_divergences, "Deployment", "api", "api")
assert api_env is not None, "api Deployment was not rendered"
worker_env = container_env(three_divergences, "Deployment", "worker", "worker")
assert worker_env is not None, "worker Deployment was not rendered"
bundle_env = container_env(
    three_divergences,
    "SandboxTemplate",
    "agent-sandbox",
    "bundle-fetch",
    init=True,
)
assert bundle_env is not None, "SandboxTemplate bundle-fetch was not rendered"
api_env[:] = [entry for entry in api_env if entry.get("name") != "S3_ACCESS_KEY"]
worker_endpoint = next(
    entry for entry in worker_env if entry.get("name") == "S3_ENDPOINT_URL"
)
worker_endpoint.pop("valueFrom", None)
worker_endpoint["value"] = "http://other-object-store:9000"
bundle_bucket = next(
    entry for entry in bundle_env if entry.get("name") == "BUNDLE_BUCKET"
)
bundle_bucket.pop("valueFrom", None)
bundle_bucket["value"] = "other-bucket"
expect_failure(
    three_divergences,
    (
        "api is missing object-store key S3_ACCESS_KEY",
        "worker has S3_ACCESS_KEY, but api does not",
        "api and worker disagree on S3_ENDPOINT_URL",
        "bundle fetch BUNDLE_BUCKET disagrees with api BUNDLE_BUCKET",
    ),
)

unsupported_diagnostic = copy.deepcopy(default_docs)
worker_env = container_env(unsupported_diagnostic, "Deployment", "worker", "worker")
assert worker_env is not None, "worker Deployment was not rendered"
worker_env[:] = [
    entry for entry in worker_env if entry.get("name") != "S3_ACCESS_KEY"
]
expect_failure(
    unsupported_diagnostic,
    ("worker is missing S3_ACCESS_KEY",),
)

assert_contract(render(EXTERNAL_VALUES), "external", "https://s3.example.com:443")
PY
