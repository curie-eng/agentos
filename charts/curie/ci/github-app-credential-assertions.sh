#!/usr/bin/env bash
#
# Render-assertion test for the GitHub App credential (ADR-0092).
#
# Two properties, both learned the hard way on a live cluster:
#
#   (a) The App ID must reach the API as the DIGITS. helm's `--set` parses a
#       bare number and a --reuse-values round trip turns it into a float64, so
#       app id 1234567 renders as "1.234567e+06", the JWT's `iss` claim is wrong,
#       and every GitHub call answers 401. The chart must quote whatever it is
#       given rather than emit a bare number, and the CLI must use --set-string.
#   (b) A BYO `githubAppExistingSecret` must make the chart REFERENCE a Secret
#       rather than carry the key, so the PEM never enters helm's stored values.
#       Without it the key is copied into every retained release revision (10 by
#       default) and `helm get values` can print it.
#
# Six assertions.
set -euo pipefail

CHART="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fail() { echo "FAIL [$1] $2" >&2; exit 1; }
VALUES_FILE="$(mktemp)"
trap 'rm -f "$VALUES_FILE"' EXIT

render() { helm template curie "$CHART" "$@" -s templates/api.yaml; }

env_block() {
  python3 - "$1" <<'PY'
import sys, yaml
doc = [d for d in yaml.safe_load_all(sys.argv[1]) if d and d.get("kind") == "Deployment"][0]
env = doc["spec"]["template"]["spec"]["containers"][0]["env"]
print(yaml.safe_dump({e["name"]: e for e in env}))
PY
}

# (a) The App ID is emitted as a quoted string, never a bare number.
OUT="$(render --set-string api.githubAppId=1234567 --set api.githubAppPrivateKey=X)"
grep -q 'name: GITHUB_APP_ID' <<<"$OUT" || fail a "GITHUB_APP_ID is missing"
python3 - "$OUT" <<'PY' || exit 1
import sys, yaml
doc = [d for d in yaml.safe_load_all(sys.argv[1]) if d and d.get("kind") == "Deployment"][0]
env = {e["name"]: e for e in doc["spec"]["template"]["spec"]["containers"][0]["env"]}
value = env["GITHUB_APP_ID"].get("value")
if value != "1234567":
    print(f"FAIL [a] GITHUB_APP_ID rendered as {value!r}, expected '1234567'. "
          "A float here becomes '1.234567e+06' and every GitHub call 401s.",
          file=sys.stderr)
    sys.exit(1)
PY

# (b) An unquoted values file App ID renders as the exact decimal string.
cat >"$VALUES_FILE" <<'EOF'
api:
  githubAppId: 4475970
EOF
OUT="$(render -f "$VALUES_FILE")"
python3 - "$OUT" <<'PY' || exit 1
import sys, yaml
doc = [d for d in yaml.safe_load_all(sys.argv[1]) if d and d.get("kind") == "Deployment"][0]
env = {e["name"]: e for e in doc["spec"]["template"]["spec"]["containers"][0]["env"]}
value = env["GITHUB_APP_ID"].get("value")
if value != "4475970":
    print(f"FAIL [b] GITHUB_APP_ID rendered as {value!r}, expected '4475970'. "
          "An unquoted values file must preserve its decimal digits.",
          file=sys.stderr)
    sys.exit(1)
PY

# (c) Absent config renders the exact empty string, not a broken reference.
OUT="$(render)"
python3 - "$OUT" <<'PY' || exit 1
import sys, yaml
doc = [d for d in yaml.safe_load_all(sys.argv[1]) if d and d.get("kind") == "Deployment"][0]
env = {e["name"]: e for e in doc["spec"]["template"]["spec"]["containers"][0]["env"]}
value = env["GITHUB_APP_ID"].get("value")
if value != "":
    print(f"FAIL [c] GITHUB_APP_ID rendered as {value!r}, expected empty string.",
          file=sys.stderr)
    sys.exit(1)
PY

# (d) Default path reads from the chart's own Secret.
OUT="$(render --set api.githubAppPrivateKey=X)"
env_block "$OUT" | grep -A 4 'GITHUB_APP_PRIVATE_KEY' | grep -q 'githubAppPrivateKey' \
  || fail d "default path must read key githubAppPrivateKey from the chart Secret"

# (e) BYO path references the named Secret instead.
OUT="$(render --set api.githubAppExistingSecret=my-gh-app)"
env_block "$OUT" | grep -q 'my-gh-app' || fail e "githubAppExistingSecret was not referenced"
env_block "$OUT" | grep -q 'privateKey' || fail e "the default BYO key name was not used"

# (f) BYO wins over an inline value, so a leftover inline key cannot silently
#     shadow the Secret an operator deliberately pointed at.
OUT="$(render --set api.githubAppExistingSecret=my-gh-app --set api.githubAppPrivateKey=STALE)"
env_block "$OUT" | grep -q 'my-gh-app' \
  || fail f "githubAppExistingSecret must win over an inline githubAppPrivateKey"

echo "github-app-credential-assertions: all six assertions passed"
