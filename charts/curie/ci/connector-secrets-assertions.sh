#!/usr/bin/env bash
# Render assertions for per-agent connector Secret storage and pod delivery.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART="$(cd "$SCRIPT_DIR/.." && pwd)"

fail() {
  echo "ASSERTION FAILED: $1" >&2
  exit 1
}

resource() {
  local rendered="$1"
  local kind="$2"
  local name="$3"
  awk -v kind="$kind" -v name="$name" '
    /^---$/ {
      if (document != "" && found_kind && found_name) {
        print document
        emitted = 1
        exit
      }
      document = ""
      found_kind = 0
      found_name = 0
    }
    {
      document = document $0 "\n"
      if ($0 == "kind: " kind) found_kind = 1
      if (found_kind && $0 == "  name: " name) found_name = 1
    }
    END {
      if (!emitted && document != "" && found_kind && found_name) print document
    }
  ' <<<"$rendered"
}

require_resource() {
  local rendered="$1"
  local kind="$2"
  local name="$3"
  local result
  result="$(resource "$rendered" "$kind" "$name")"
  [ -n "$result" ] || fail "${kind}/${name} did not render"
  printf '%s' "$result"
}

require_text() {
  local text="$1"
  local pattern="$2"
  local message="$3"
  grep -Eq "$pattern" <<<"$text" || fail "$message"
}

forbid_text() {
  local text="$1"
  local pattern="$2"
  local message="$3"
  if grep -Eq "$pattern" <<<"$text"; then
    fail "$message"
  fi
}

# The two bindings carry distinct values through the existing name-keyed Secret
# contract. The values themselves are not accepted anywhere but their own Secret.
rendered="$(helm template curie "$CHART" \
  --set-string 'agentSandbox.connectorSecrets.acme-a.GITHUB_PERSONAL_ACCESS_TOKEN=agent-a-sentinel' \
  --set-string 'agentSandbox.connectorSecrets.acme-b.GITHUB_PERSONAL_ACCESS_TOKEN=agent-b-sentinel' \
  2>/dev/null)"

secret_a="$(require_resource "$rendered" Secret curie-agent-acme-a-connector-secrets)"
secret_b="$(require_resource "$rendered" Secret curie-agent-acme-b-connector-secrets)"
require_text "$secret_a" 'curietech.ai/agent: "acme-a"' \
  "acme-a Secret lacks the agent label"
require_text "$secret_b" 'curietech.ai/agent: "acme-b"' \
  "acme-b Secret lacks the agent label"
require_text "$secret_a" 'GITHUB_PERSONAL_ACCESS_TOKEN: "agent-a-sentinel"' \
  "acme-a Secret lacks its connector value"
require_text "$secret_b" 'GITHUB_PERSONAL_ACCESS_TOKEN: "agent-b-sentinel"' \
  "acme-b Secret lacks its connector value"
forbid_text "$secret_a" 'agent-b-sentinel' "acme-b value leaked into acme-a Secret"
forbid_text "$secret_b" 'agent-a-sentinel' "acme-a value leaked into acme-b Secret"

# The shared chart Secret remains separate from every connector Secret.
shared="$(require_resource "$rendered" Secret curie-secrets)"
forbid_text "$shared" 'GITHUB_PERSONAL_ACCESS_TOKEN|agent-a-sentinel|agent-b-sentinel' \
  "connector secret leaked into the shared chart Secret"

# Each existing name-keyed Secret produces an independently named template and
# pool. The template receives the same agent label and references only its own
# Secret by key. This is the delivery boundary, not worker or claim routing.
for agent in acme-a acme-b; do
  template="$(require_resource "$rendered" SandboxTemplate "curie-agent-${agent}-runner")"
  pool="$(require_resource "$rendered" SandboxWarmPool "curie-agent-${agent}-runner-pool")"
  other="acme-a"
  [ "$agent" = "acme-a" ] && other="acme-b"

  require_text "$template" "curietech.ai/agent: \\\"?${agent}\\\"?" \
    "SandboxTemplate for ${agent} lacks its pod agent label"
  require_text "$template" 'name: GITHUB_PERSONAL_ACCESS_TOKEN' \
    "SandboxTemplate for ${agent} lacks connector env delivery"
  require_text "$template" 'secretKeyRef:' \
    "SandboxTemplate for ${agent} does not use secretKeyRef"
  require_text "$template" "name: curie-agent-${agent}-connector-secrets" \
    "SandboxTemplate for ${agent} references the wrong connector Secret"
  require_text "$template" 'key: GITHUB_PERSONAL_ACCESS_TOKEN' \
    "SandboxTemplate for ${agent} references the wrong connector key"
  require_text "$template" 'optional: false' \
    "SandboxTemplate for ${agent} makes its connector Secret optional"
  forbid_text "$template" "curie-agent-${other}-connector-secrets" \
    "SandboxTemplate for ${agent} references ${other}'s connector Secret"
  forbid_text "$template" 'agent-a-sentinel|agent-b-sentinel' \
    "SandboxTemplate for ${agent} contains a connector value"

  require_text "$pool" 'sandboxTemplateRef:' \
    "SandboxWarmPool for ${agent} lacks a template reference"
  require_text "$pool" "name: curie-agent-${agent}-runner" \
    "SandboxWarmPool for ${agent} does not select its own template"
  forbid_text "$pool" "curie-agent-${other}-runner" \
    "SandboxWarmPool for ${agent} selects ${other}'s template"
done

# An empty map stays on the generic substrate path: exactly the generic
# SandboxTemplate and SandboxWarmPool render, with no connector-specific peers.
default_render="$(helm template curie "$CHART" 2>/dev/null)"
generic_template="$(require_resource "$default_render" SandboxTemplate curie-runner)"
generic_pool="$(require_resource "$default_render" SandboxWarmPool curie-runner-pool)"
forbid_text "$default_render" 'curie-agent-.*-(connector-secrets|runner|runner-pool)' \
  "connector-specific resource rendered with no connectorSecrets"
[ "$generic_template" = "$(require_resource "$rendered" SandboxTemplate curie-runner)" ] \
  || fail "connectorSecrets changed the generic SandboxTemplate"
[ "$generic_pool" = "$(require_resource "$rendered" SandboxWarmPool curie-runner-pool)" ] \
  || fail "connectorSecrets changed the generic SandboxWarmPool"

# Existing reserved-key guard remains fail closed, with a paired legitimate key
# control so this assertion cannot pass because connector Secrets stopped rendering.
assert_reserved_render_fails() {
  local key="$1"
  local output
  if output="$(helm template curie "$CHART" \
    --set "agentSandbox.connectorSecrets.demo.${key}=x" 2>&1)"; then
    fail "reserved connector-secret name '${key}' rendered instead of failing"
  fi
  grep -qi 'reserved' <<<"$output" \
    || fail "reserved '${key}' render failed without naming the reservation"
}

for key in ANTHROPIC_BASE_URL ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_AUTH_TOKEN CURIE_BUDGET; do
  assert_reserved_render_fails "$key"
done

if ! helm template curie "$CHART" \
  --set 'agentSandbox.connectorSecrets.demo.GITHUB_PERSONAL_ACCESS_TOKEN=ghp_ok' >/dev/null 2>&1; then
  fail "legitimate connector-secret name GITHUB_PERSONAL_ACCESS_TOKEN failed to render"
fi

echo "OK: per-agent connector-secret render assertions passed"
