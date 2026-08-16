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
  "$(manifest_for "$TMP/selfnamed" secrets.yaml)" \
  "$(manifest_for "$TMP/default" langfuse.yaml)" \
  "$(manifest_for "$TMP/selfnamed" langfuse.yaml)" <<'PY'
import base64
import sys

import yaml

OVERRIDE_HEADER = "Basic YXNzZXJ0OmFzc2VydA=="


def docs(path):
    return [doc for doc in yaml.safe_load_all(open(path)) if doc]


def init_project_public_key(path):
    values = {
        env.get("value")
        for doc in docs(path)
        for container in doc.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        for env in container.get("env", [])
        if env.get("name") == "LANGFUSE_INIT_PROJECT_PUBLIC_KEY"
    }
    assert len(values) == 1, (
        f"{path}: expected exactly one LANGFUSE_INIT_PROJECT_PUBLIC_KEY value on the Langfuse "
        f"web deployment, found {sorted(values)}"
    )
    return values.pop()


def header_halves(label, header):
    """Split `Basic <base64(public:secret)>` into its two halves.

    The chart no longer ships a literal published credential on a sealed
    install: curie.managedSecret generates the init project secret key per
    release. So the assertion that catches a desync is structural -- the header
    must decode to the public key Langfuse is seeded with, paired with the
    secret key this very Secret carries.
    """
    scheme, sep, credential = header.partition(" ")
    assert sep and scheme == "Basic", (
        f"{label}: the collector header must be `Basic <base64>`, got {header!r}"
    )
    try:
        decoded = base64.b64decode(credential, validate=True).decode()
    except Exception as exc:  # noqa: BLE001 - the message is the assertion
        raise AssertionError(
            f"{label}: the collector header credential {credential!r} is not valid base64 ({exc})"
        ) from exc
    public, sep, secret = decoded.partition(":")
    assert sep, (
        f"{label}: the decoded credential must be `<publicKey>:<secretKey>`, got {decoded!r}"
    )
    return public, secret


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
default_public_key = init_project_public_key(sys.argv[9])
selfnamed_public_key = init_project_public_key(sys.argv[10])

# T1: the default render keeps reading the chart-managed Secret, which carries
# the header composed from the project public key and the RESOLVED init project
# secret key. That resolution generates a random per release on a sealed install
# (issue #195), so the assertion is that the header and the Secret agree, not
# that either carries a particular literal.
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
default_public, default_secret_half = header_halves("T1", default_secret["otlpAuthHeader"])
assert default_public == default_public_key, (
    "T1: the collector header's public half must be the project public key Langfuse is seeded "
    f"with ({default_public_key!r}), got {default_public!r}. A mismatch authenticates the trace "
    "export against a project that does not exist"
)
assert default_secret_half == default_secret.get("langfuseInitProjectSecretKey"), (
    "T1: the collector header's secret half must be the langfuseInitProjectSecretKey this very "
    f"Secret carries ({default_secret.get('langfuseInitProjectSecretKey')!r}), got "
    f"{default_secret_half!r}. That is the desync issue #1563 closes: the collector would "
    "authenticate with a key Langfuse was never seeded with and 401 on every trace export"
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
selfnamed_public, selfnamed_secret_half = header_halves("T6", selfnamed_secret["otlpAuthHeader"])
assert selfnamed_public == selfnamed_public_key, (
    "T6: the chart-managed Secret is the one the collector reads, so its otlpAuthHeader must "
    f"carry the project public key Langfuse is seeded with ({selfnamed_public_key!r}), got "
    f"{selfnamed_public!r}"
)
assert selfnamed_secret_half == selfnamed_secret.get("langfuseInitProjectSecretKey"), (
    "T6: the chart-managed Secret is the one the collector reads, so its otlpAuthHeader must "
    "carry the langfuseInitProjectSecretKey emitted alongside it "
    f"({selfnamed_secret.get('langfuseInitProjectSecretKey')!r}), got {selfnamed_secret_half!r}. "
    "A non-empty but unrelated value lets the collector start and 401 to Langfuse in silence"
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
# configuration and asserts the header agrees with the Secret). A gate that only reads
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

# T13: whitespace INSIDE the credential is the same copy/paste and line-wrap
# artifact as whitespace around it. The receiver strips it before base64
# decoding, so "cGst...bGYtY3Vy<TAB>aWUtZGV2..." presents exactly the published
# dev credential. Taking the last whitespace-separated field would compare only
# the tail and let both spellings through. langfuse.existingSecret is foreign
# here for the same reason as in T4, so the failure can only come from the
# otlpAuthHeader check.
rc="$(gate_rc dev-header-interior-tab --set security.checkDefaultCredentials=true --set langfuse.existingSecret='my-langfuse' --set-string "otelCollector.otlpAuthHeader=$(printf 'Basic cGstbGYtY3VyaWUtZGV2OnNrLWxmLWN1\tcmllLWRldg==')")"
[[ "$rc" != "0" ]] || fail "T13: security.checkDefaultCredentials=true rendered successfully while otelCollector.otlpAuthHeader carried the published dev credential with a tab inside the base64. Langfuse strips the whitespace and accepts pk-lf-curie-dev:sk-lf-curie-dev, so splitting the header on whitespace and comparing only the last field defeats the gate"
grep -qF 'otelCollector.otlpAuthHeader' "$TMP/dev-header-interior-tab.stderr" || fail "T13: the render failed but its message never names otelCollector.otlpAuthHeader. stderr was: $(cat "$TMP/dev-header-interior-tab.stderr")"

rc="$(gate_rc dev-header-interior-space --set security.checkDefaultCredentials=true --set langfuse.existingSecret='my-langfuse' --set-string 'otelCollector.otlpAuthHeader=Basic cGstbGYtY3VyaWUtZGV2OnNrLWxmLWN1 cmllLWRldg==')"
[[ "$rc" != "0" ]] || fail "T13: security.checkDefaultCredentials=true rendered successfully while otelCollector.otlpAuthHeader carried the published dev credential with a plain space inside the base64. The credential still decodes to pk-lf-curie-dev:sk-lf-curie-dev, so the gate must normalise whitespace across the whole credential rather than take its last field"
grep -qF 'otelCollector.otlpAuthHeader' "$TMP/dev-header-interior-space.stderr" || fail "T13: the render failed but its message never names otelCollector.otlpAuthHeader. stderr was: $(cat "$TMP/dev-header-interior-space.stderr")"

# T14: the check is deliberately independent of otelCollector.deploy -- the
# chart writes the published dev credential into its Secret either way. So the
# refusal is correct here, but the message must not assert a collector that this
# render does not produce: with deploy=false no collector manifest exists, and a
# message claiming a header "the OTel Collector would send" describes something
# the operator cannot find. Asserting on the wording is what stops a future edit
# from silently reintroducing that false claim.
rc="$(gate_rc dev-header-no-collector --set security.checkDefaultCredentials=true --set otelCollector.deploy=false --set langfuse.existingSecret='my-langfuse' --set-string 'otelCollector.otlpAuthHeader=Basic cGstbGYtY3VyaWUtZGV2OnNrLWxmLWN1cmllLWRldg==')"
[[ "$rc" != "0" ]] || fail "T14: security.checkDefaultCredentials=true rendered successfully with otelCollector.deploy=false and the published dev header. The chart still writes that credential into its Secret whether or not this release runs a collector, so the gate must still refuse"
grep -qF 'as the OTel Collector auth credential in its Secret' "$TMP/dev-header-no-collector.stderr" || fail "T14: the render failed but its message does not say the credential is what the chart ships in its Secret, which is the part that is true whether or not a collector is deployed. stderr was: $(cat "$TMP/dev-header-no-collector.stderr")"
if grep -qF 'header the OTel Collector would send to Langfuse' "$TMP/dev-header-no-collector.stderr"; then
  fail "T14: with otelCollector.deploy=false no collector manifest renders, but the message still describes the header as one the OTel Collector would send to Langfuse. It asserts a component this render does not contain. stderr was: $(cat "$TMP/dev-header-no-collector.stderr")"
fi

# T15/T16: the success control for the gate. Every other gate assertion above
# proves the gate REFUSES something, so a gate that refused every non-empty
# header would satisfy all of them. These two renders are the shapes a real
# hardened install takes -- an operator Secret plus an own-project header, and
# the chart's own Secret with every langfuse.init credential overridden -- and
# they must render with security.checkDefaultCredentials on. This is what stops
# a further round of strictness from blocking legitimate installs.
render_rc() {
  local name="$1"
  shift
  local rc=0
  helm template curie "$CHART" --output-dir "$TMP/$name" "$@" >/dev/null 2>"$TMP/$name.stderr" || rc=$?
  printf '%s\n' "$rc"
}

LEGIT_FOREIGN_HEADER='Basic cGstbGYtb3duZWQ6c2stbGYtb3duZWQ='
LEGIT_SELFNAMED_HEADER='Basic cGstbGYtbWluZTpzay1sZi1taW5l'

rc="$(render_rc gate-legit-foreign --set security.checkDefaultCredentials=true --set langfuse.existingSecret='my-langfuse' --set-string "otelCollector.otlpAuthHeader=$LEGIT_FOREIGN_HEADER")"
[[ "$rc" == "0" ]] || fail "T15: security.checkDefaultCredentials=true with a foreign langfuse.existingSecret and an operator's own otelCollector.otlpAuthHeader must render. The gate exists to refuse this repository's published dev credential, not to refuse a legitimate header, and refusing one blocks exactly the hardened install the gate is meant to protect. stderr was: $(cat "$TMP/gate-legit-foreign.stderr")"

rc="$(render_rc gate-legit-selfnamed --set security.checkDefaultCredentials=true --set langfuse.existingSecret='curie-secrets' --set langfuse.init.projectPublicKey='pk-lf-mine' --set langfuse.init.projectSecretKey='sk-lf-mine' --set langfuse.init.userPassword='not-the-dev-password' --set-string "otelCollector.otlpAuthHeader=$LEGIT_SELFNAMED_HEADER")"
[[ "$rc" == "0" ]] || fail "T16: security.checkDefaultCredentials=true with langfuse.existingSecret naming the chart's own Secret, every langfuse.init credential overridden and an operator's own otelCollector.otlpAuthHeader must render. Nothing published is left on this path, so the gate must let it through. stderr was: $(cat "$TMP/gate-legit-selfnamed.stderr")"

python3 - \
  "$LEGIT_FOREIGN_HEADER" \
  "$(manifest_for "$TMP/gate-legit-foreign" secrets.yaml)" \
  "$LEGIT_SELFNAMED_HEADER" \
  "$(manifest_for "$TMP/gate-legit-selfnamed" secrets.yaml)" <<'PY'
import base64
import sys

import yaml


def docs(path):
    return [doc for doc in yaml.safe_load_all(open(path)) if doc]


def secret_entries(path):
    secret = next((doc for doc in docs(path) if doc.get("kind") == "Secret"), None)
    assert secret is not None, f"{path}: no chart-managed Secret rendered"
    entries = dict(secret.get("stringData") or {})
    for key, value in (secret.get("data") or {}).items():
        entries[key] = base64.b64decode(value).decode()
    return entries


legit_foreign_header = sys.argv[1]
legit_foreign_secret = secret_entries(sys.argv[2])
legit_selfnamed_header = sys.argv[3]
legit_selfnamed_secret = secret_entries(sys.argv[4])

# T15/T16: the render succeeding is not enough. The header the operator passed
# must reach the chart-managed Secret the collector reads, verbatim, so a gate
# that silently normalised or blanked the value on its way through is caught
# here rather than at a 401 in production.
assert legit_foreign_secret.get("otlpAuthHeader") == legit_foreign_header, (
    "T15: with the gate on and a foreign langfuse.existingSecret, the chart-managed Secret must "
    f"carry the operator's otelCollector.otlpAuthHeader verbatim ({legit_foreign_header!r}), got "
    f"{legit_foreign_secret.get('otlpAuthHeader')!r}"
)
assert legit_selfnamed_secret.get("otlpAuthHeader") == legit_selfnamed_header, (
    "T16: with the gate on and langfuse.existingSecret naming the chart's own Secret, that Secret "
    f"must carry the operator's otelCollector.otlpAuthHeader verbatim ({legit_selfnamed_header!r}), "
    f"got {legit_selfnamed_secret.get('otlpAuthHeader')!r}"
)
PY

echo "Collector follows langfuse.existingSecret, the chart Secret ships no stale dev header, the override still wins, and the default-credential gate refuses the published dev header: OK"
