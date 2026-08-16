#!/usr/bin/env bash
set -euo pipefail

# Issue #1563: the OTel Collector must read its Langfuse auth header out of the
# same Secret the chart decided to put it in. Under `langfuse.existingSecret`
# the collector has to follow the operator's Secret and the chart-managed Secret
# must ship no stale dev-derived header, and the default-credential gate must
# refuse the published dev header even on that path.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART="$(cd "$SCRIPT_DIR/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

render() {
  local name="$1"
  shift
  helm template curie "$CHART" --output-dir "$TMP/$name" "$@" >/dev/null
}

manifest_for() {
  local manifest
  manifest="$(find "$1" -type f -path "*/templates/$2" -print -quit)"
  [[ -n "$manifest" ]] || fail "$2 was not written to $1"
  printf '%s\n' "$manifest"
}

# Renders that must succeed. The gate renders below are run separately because
# two of them are expected to fail.
render default
render existing --set langfuse.existingSecret='my-langfuse'
render override --set langfuse.existingSecret='my-langfuse' --set otelCollector.otlpAuthHeader='Basic YXNzZXJ0OmFzc2VydA=='
render selfnamed --set langfuse.existingSecret='curie-secrets'

python3 - \
  "$(manifest_for "$TMP/default" otel-collector.yaml)" \
  "$(manifest_for "$TMP/default" secrets.yaml)" \
  "$(manifest_for "$TMP/existing" otel-collector.yaml)" \
  "$(manifest_for "$TMP/existing" secrets.yaml)" \
  "$(manifest_for "$TMP/override" otel-collector.yaml)" \
  "$(manifest_for "$TMP/override" secrets.yaml)" \
  "$(manifest_for "$TMP/selfnamed" otel-collector.yaml)" \
  "$(manifest_for "$TMP/selfnamed" secrets.yaml)" <<'PY'
import base64
import sys

import yaml

DEV_HEADER = "Basic cGstbGYtY3VyaWUtZGV2OnNrLWxmLWN1cmllLWRldg=="
OVERRIDE_HEADER = "Basic YXNzZXJ0OmFzc2VydA=="


def docs(path):
    return [doc for doc in yaml.safe_load_all(open(path)) if doc]


def collector_secret_ref(path):
    deployment = next((doc for doc in docs(path) if doc.get("kind") == "Deployment"), None)
    assert deployment is not None, f"{path}: no OTel Collector Deployment rendered"
    entries = [
        env
        for container in deployment["spec"]["template"]["spec"]["containers"]
        for env in container.get("env", [])
        if env.get("name") == "LANGFUSE_OTLP_AUTH_HEADER"
    ]
    assert len(entries) == 1, (
        f"{path}: expected exactly one LANGFUSE_OTLP_AUTH_HEADER env entry on the collector, "
        f"found {len(entries)}"
    )
    ref = entries[0].get("valueFrom", {}).get("secretKeyRef")
    assert ref, (
        f"{path}: LANGFUSE_OTLP_AUTH_HEADER does not resolve through a secretKeyRef, so the "
        "collector credential is no longer sourced from a Secret"
    )
    return ref


def secret_entries(path):
    secret = next((doc for doc in docs(path) if doc.get("kind") == "Secret"), None)
    assert secret is not None, f"{path}: no chart-managed Secret rendered"
    entries = dict(secret.get("stringData") or {})
    for key, value in (secret.get("data") or {}).items():
        entries[key] = base64.b64decode(value).decode()
    return secret["metadata"]["name"], entries


default_ref = collector_secret_ref(sys.argv[1])
default_secret_name, default_secret = secret_entries(sys.argv[2])
existing_ref = collector_secret_ref(sys.argv[3])
_, existing_secret = secret_entries(sys.argv[4])
override_ref = collector_secret_ref(sys.argv[5])
_, override_secret = secret_entries(sys.argv[6])
selfnamed_ref = collector_secret_ref(sys.argv[7])
_, selfnamed_secret = secret_entries(sys.argv[8])

# T1: the default render keeps reading the chart-managed Secret, which carries
# the header derived from the langfuse.init keys.
assert default_ref["name"] == "curie-secrets", (
    "T1: on the default render the collector must read otlpAuthHeader from the chart-managed "
    f"Secret curie-secrets, but secretKeyRef.name is {default_ref['name']!r}"
)
assert default_ref["key"] == "otlpAuthHeader", (
    f"T1: collector secretKeyRef.key must stay otlpAuthHeader, got {default_ref['key']!r}"
)
assert default_secret_name == "curie-secrets", (
    f"T1: chart-managed Secret must be named curie-secrets, got {default_secret_name!r}"
)
assert "otlpAuthHeader" in default_secret, (
    "T1: the chart-managed Secret dropped otlpAuthHeader on the default path, so the collector "
    "would fail to start with CreateContainerConfigError on a plain install"
)
assert default_secret["otlpAuthHeader"] == DEV_HEADER, (
    "T1: the default chart-managed Secret must carry the header derived from the langfuse.init "
    f"keys ({DEV_HEADER!r}), got {default_secret['otlpAuthHeader']!r}"
)

# T2: langfuse.existingSecret redirects the collector and the chart stops
# shipping a dev-derived header the collector no longer reads (issue #1563).
assert existing_ref["name"] == "my-langfuse", (
    "T2: with langfuse.existingSecret=my-langfuse the collector must read otlpAuthHeader from "
    f"my-langfuse, but secretKeyRef.name is {existing_ref['name']!r}. The collector would keep "
    "authenticating to Langfuse with the chart's published dev key and 401 silently"
)
assert existing_ref["key"] == "otlpAuthHeader", (
    f"T2: collector secretKeyRef.key must stay otlpAuthHeader, got {existing_ref['key']!r}"
)
assert "otlpAuthHeader" not in existing_secret, (
    "T2: with langfuse.existingSecret set the chart-managed Secret must not contain an "
    "otlpAuthHeader key at all, but it does with value "
    f"{existing_secret['otlpAuthHeader']!r}. An empty or stale value lets the collector start "
    "and 401 silently instead of failing loudly with CreateContainerConfigError"
)

# T3: an explicit otelCollector.otlpAuthHeader override still wins and still
# materialises into the chart-managed Secret the collector points at.
assert override_ref["name"] == "curie-secrets", (
    "T3: with otelCollector.otlpAuthHeader set the collector must read the chart-managed Secret "
    f"curie-secrets that holds the override, but secretKeyRef.name is {override_ref['name']!r}"
)
assert override_secret.get("otlpAuthHeader") == OVERRIDE_HEADER, (
    "T3: the chart-managed Secret must carry the otelCollector.otlpAuthHeader override verbatim "
    f"({OVERRIDE_HEADER!r}), got {override_secret.get('otlpAuthHeader')!r}"
)

# T6: langfuse.existingSecret may name the chart's own Secret. Then the Secret
# the collector reads IS the chart-managed one, so the key must be emitted and
# carry a real header. The secretKeyRef and the emission condition are the same
# decision, and they must not disagree (issue #1563).
assert selfnamed_ref["name"] == "curie-secrets", (
    "T6: with langfuse.existingSecret=curie-secrets the collector must read the chart-managed "
    f"Secret curie-secrets, but secretKeyRef.name is {selfnamed_ref['name']!r}"
)
assert "otlpAuthHeader" in selfnamed_secret, (
    "T6: the collector's secretKeyRef resolves to the chart-managed Secret curie-secrets, but "
    "that Secret omits the otlpAuthHeader key. The secretKeyRef site and the secrets.yaml "
    "emission condition disagree about which Secret carries the header, so the collector pod "
    "would enter CreateContainerConfigError"
)
assert selfnamed_secret["otlpAuthHeader"], (
    "T6: the chart-managed Secret is the one the collector reads, so its otlpAuthHeader must be "
    "non-empty, but it is empty. The collector would start with an empty Authorization header "
    "and 401 to Langfuse in silence"
)
PY

gate_rc() {
  local name="$1"
  shift
  local rc=0
  helm template curie "$CHART" "$@" >/dev/null 2>"$TMP/$name.stderr" || rc=$?
  printf '%s\n' "$rc"
}

# T4: the default-credential gate must refuse the published dev header even on
# the langfuse.existingSecret path, which is exactly where it used to go green.
rc="$(gate_rc dev-header --set security.checkDefaultCredentials=true --set langfuse.existingSecret='my-langfuse' --set otelCollector.otlpAuthHeader='Basic cGstbGYtY3VyaWUtZGV2OnNrLWxmLWN1cmllLWRldg==')"
[[ "$rc" != "0" ]] || fail "T4: security.checkDefaultCredentials=true rendered successfully while otelCollector.otlpAuthHeader carried the published dev header. The gate goes green on a render that ships pk-lf-curie-dev:sk-lf-curie-dev to the collector"
grep -qF 'otelCollector.otlpAuthHeader' "$TMP/dev-header.stderr" || fail "T4: the render failed but its message never names otelCollector.otlpAuthHeader, so the operator cannot tell which setting to fix. stderr was: $(cat "$TMP/dev-header.stderr")"

# T5: the two pre-existing gate behaviours are unchanged. This is what stops a
# well-meaning "make the gate unconditional" refactor from landing.
rc="$(gate_rc dev-defaults --set security.checkDefaultCredentials=true)"
[[ "$rc" != "0" ]] || fail "T5: security.checkDefaultCredentials=true rendered successfully on the default values, where langfuse.init.projectSecretKey is still the published dev key sk-lf-curie-dev"
grep -qF 'langfuse.init.projectSecretKey' "$TMP/dev-defaults.stderr" || fail "T5: the render failed but its message never names langfuse.init.projectSecretKey. stderr was: $(cat "$TMP/dev-defaults.stderr")"

rc="$(gate_rc existing-secret --set security.checkDefaultCredentials=true --set langfuse.existingSecret='my-langfuse')"
[[ "$rc" == "0" ]] || fail "T5: security.checkDefaultCredentials=true with langfuse.existingSecret set and no dev header override must still render. The langfuse.init credential checks must stay exempt on the BYO Secret path. stderr was: $(cat "$TMP/existing-secret.stderr")"

# T7: reach the userPassword check. T5 renders with both published defaults, so
# projectSecretKey fails first and that check is never exercised; overriding
# projectSecretKey is what makes this assertion pin the second one.
rc="$(gate_rc dev-password --set security.checkDefaultCredentials=true --set langfuse.init.projectSecretKey=sk-owned)"
[[ "$rc" != "0" ]] || fail "T7: security.checkDefaultCredentials=true rendered successfully with langfuse.init.userPassword still the published dev default curie-dev-password, which allows Langfuse admin takeover on a reachable UI"
grep -qF 'langfuse.init.userPassword' "$TMP/dev-password.stderr" || fail "T7: the render failed but its message never names langfuse.init.userPassword. stderr was: $(cat "$TMP/dev-password.stderr")"

# T8: the same dev credential written without base64 padding is still accepted
# by the receiver, so an exact-string gate that only matches the padded literal
# leaves the hole open. langfuse.existingSecret is set for the same reason as in
# T4, so the failure can only come from the otlpAuthHeader check.
rc="$(gate_rc dev-header-unpadded --set security.checkDefaultCredentials=true --set langfuse.existingSecret='my-langfuse' --set otelCollector.otlpAuthHeader='Basic cGstbGYtY3VyaWUtZGV2OnNrLWxmLWN1cmllLWRldg')"
[[ "$rc" != "0" ]] || fail "T8: security.checkDefaultCredentials=true rendered successfully while otelCollector.otlpAuthHeader carried the published dev credential with the base64 padding stripped. That header decodes to pk-lf-curie-dev:sk-lf-curie-dev and Langfuse accepts it, so dropping two '=' characters defeats the gate"
grep -qF 'otelCollector.otlpAuthHeader' "$TMP/dev-header-unpadded.stderr" || fail "T8: the render failed but its message never names otelCollector.otlpAuthHeader. stderr was: $(cat "$TMP/dev-header-unpadded.stderr")"

# T9: the chart itself composes and ships the dev header on this path. No
# operator override is set, and langfuse.existingSecret names the chart's own
# Secret, so curie.otlpAuthHeaderSecretName resolves back to curie-secrets and
# the header is derived from the langfuse.init keys (T6 renders this exact
# configuration and asserts the dev header is present). A gate that only reads
# the otelCollector.otlpAuthHeader input never sees it. The chart composes that
# header only out of both published dev init keys, so the projectSecretKey check
# now covers this render too and, running first, is the message the operator
# gets. Either check refusing the render satisfies what T9 exists to protect:
# this configuration must never render. The rendered-header check itself stays
# pinned by T4, T8, T10 and T12 on the foreign-Secret path, where it is the only
# check that applies.
rc="$(gate_rc selfnamed-dev-header --set security.checkDefaultCredentials=true --set langfuse.existingSecret='curie-secrets')"
[[ "$rc" != "0" ]] || fail "T9: security.checkDefaultCredentials=true rendered successfully with langfuse.existingSecret=curie-secrets, where the chart composes the published dev header out of the langfuse.init keys and ships it to the collector in its own Secret. The gate must judge the header the collector would send, not just the otelCollector.otlpAuthHeader input"
grep -qE 'langfuse\.init\.(projectPublicKey|projectSecretKey)' "$TMP/selfnamed-dev-header.stderr" || fail "T9: the render failed but its message names neither langfuse.init.projectPublicKey nor langfuse.init.projectSecretKey, so the operator cannot tell the credential was composed by the chart out of its own values rather than set as an override. stderr was: $(cat "$TMP/selfnamed-dev-header.stderr")"

# T10: a trailing space is a plausible copy/paste artifact, not only a
# deliberate bypass. The receiver splits the Authorization header on a space and
# reads element 1, so the padding whitespace is discarded and the dev credential
# is accepted exactly as if it had been pasted cleanly.
rc="$(gate_rc dev-header-trailing-space --set security.checkDefaultCredentials=true --set langfuse.existingSecret='my-langfuse' --set otelCollector.otlpAuthHeader='Basic cGstbGYtY3VyaWUtZGV2OnNrLWxmLWN1cmllLWRldg== ')"
[[ "$rc" != "0" ]] || fail "T10: security.checkDefaultCredentials=true rendered successfully while otelCollector.otlpAuthHeader carried the published dev header with a trailing space. Langfuse ignores the surrounding whitespace and accepts pk-lf-curie-dev:sk-lf-curie-dev, so a stray space defeats the gate"
grep -qF 'otelCollector.otlpAuthHeader' "$TMP/dev-header-trailing-space.stderr" || fail "T10: the render failed but its message never names otelCollector.otlpAuthHeader. stderr was: $(cat "$TMP/dev-header-trailing-space.stderr")"

# T11: langfuse.existingSecret naming the chart's OWN Secret does not move the
# Langfuse credentials anywhere else -- the chart still composes them from the
# langfuse.init keys and ships them in that very Secret. So the two #198 checks
# must run on that path. Overriding only the public half is what makes this
# assertion pin check 1 rather than the rendered-header check: the composed
# header is no longer the published dev header, so only the projectSecretKey
# check can catch the published dev SECRET key still going to the collector.
rc="$(gate_rc selfnamed-init-keys --set security.checkDefaultCredentials=true --set langfuse.existingSecret='curie-secrets' --set langfuse.init.projectPublicKey='pk-mine')"
[[ "$rc" != "0" ]] || fail "T11: security.checkDefaultCredentials=true rendered successfully with langfuse.existingSecret=curie-secrets and only the public key overridden, so the chart ships Basic base64(pk-mine:sk-lf-curie-dev) to the collector with the published dev secret key sk-lf-curie-dev in it. langfuse.existingSecret naming the chart's own Secret must not exempt the langfuse.init checks, because that Secret is the one the chart fills from those very values"
grep -qF 'langfuse.init.projectSecretKey' "$TMP/selfnamed-init-keys.stderr" || fail "T11: the render failed but its message never names langfuse.init.projectSecretKey. stderr was: $(cat "$TMP/selfnamed-init-keys.stderr")"

# T12: the HTTP auth scheme token is case-insensitive and is separated from the
# credential by whitespace, so "basic <cred>" and "Basic  <cred>" are the same
# credential to the receiver. The gate must judge the credential token, not the
# spelling of the scheme. langfuse.existingSecret is foreign here for the same
# reason as in T4, so the failure can only come from the otlpAuthHeader check.
rc="$(gate_rc dev-header-lowercase-scheme --set security.checkDefaultCredentials=true --set langfuse.existingSecret='my-langfuse' --set-string otelCollector.otlpAuthHeader='basic cGstbGYtY3VyaWUtZGV2OnNrLWxmLWN1cmllLWRldg==')"
[[ "$rc" != "0" ]] || fail "T12: security.checkDefaultCredentials=true rendered successfully while otelCollector.otlpAuthHeader carried the published dev credential under a lowercase scheme token. RFC 9110 makes the auth scheme case-insensitive, so Langfuse accepts pk-lf-curie-dev:sk-lf-curie-dev exactly as it would from the canonical spelling"
grep -qF 'otelCollector.otlpAuthHeader' "$TMP/dev-header-lowercase-scheme.stderr" || fail "T12: the render failed but its message never names otelCollector.otlpAuthHeader. stderr was: $(cat "$TMP/dev-header-lowercase-scheme.stderr")"

rc="$(gate_rc dev-header-double-space --set security.checkDefaultCredentials=true --set langfuse.existingSecret='my-langfuse' --set-string otelCollector.otlpAuthHeader='Basic  cGstbGYtY3VyaWUtZGV2OnNrLWxmLWN1cmllLWRldg==')"
[[ "$rc" != "0" ]] || fail "T12: security.checkDefaultCredentials=true rendered successfully while otelCollector.otlpAuthHeader carried the published dev credential separated from the scheme by two spaces. The receiver takes the last whitespace-separated field, so the extra space changes nothing about which credential is presented"
grep -qF 'otelCollector.otlpAuthHeader' "$TMP/dev-header-double-space.stderr" || fail "T12: the render failed but its message never names otelCollector.otlpAuthHeader. stderr was: $(cat "$TMP/dev-header-double-space.stderr")"

# T12: the empty rendered header is not a credential and must not be read as
# one. With a foreign Secret and no override the chart ships nothing, and the
# last-field comparison must not collapse that to a match.
rc="$(gate_rc existing-secret-empty-header --set security.checkDefaultCredentials=true --set langfuse.existingSecret='my-langfuse')"
[[ "$rc" == "0" ]] || fail "T12: security.checkDefaultCredentials=true with a foreign langfuse.existingSecret and no header override must still render. The chart ships no header at all on that path, so there is no default credential to refuse. stderr was: $(cat "$TMP/existing-secret-empty-header.stderr")"

echo "Collector follows langfuse.existingSecret, the chart Secret ships no stale dev header, the override still wins, and the default-credential gate refuses the published dev header: OK"
