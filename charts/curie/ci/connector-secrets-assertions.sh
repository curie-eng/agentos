#!/usr/bin/env bash
# Render assertions for the per agent connector secret delivery contract.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART="$(cd "$SCRIPT_DIR/.." && pwd)"

fail() {
  echo "ASSERTION FAILED: $1" >&2
  exit 1
}

# Return one rendered resource by its Kubernetes identity. This intentionally
# reads manifest output, not template source, so a renderer refactor cannot
# satisfy the assertions by preserving only a string in a template.
resource() {
  local rendered="$1"
  local kind="$2"
  local name="$3"
  printf '%s\n' "$rendered" | awk -v kind="$kind" -v name="$name" '
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
  '
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
  printf '%s' "$text" | grep -Eq "$pattern" || fail "$message"
}

forbid_text() {
  local text="$1"
  local pattern="$2"
  local message="$3"
  if printf '%s' "$text" | grep -Eq "$pattern"; then
    fail "$message"
  fi
}

policy_for_agent() {
  local rendered="$1"
  local agent="$2"
  printf '%s\n' "$rendered" | awk -v agent="$agent" '
    /^---$/ {
      if (document != "" && kind == "NetworkPolicy" && agent_label) {
        print document
        emitted = 1
        exit
      }
      document = ""
      kind = ""
      agent_label = 0
    }
    {
      document = document $0 "\n"
      if ($0 == "kind: NetworkPolicy") kind = "NetworkPolicy"
      if ($0 ~ "curietech.ai/agent: \\\"?" agent "\\\"?$") agent_label = 1
    }
    END {
      if (!emitted && document != "" && kind == "NetworkPolicy" && agent_label) print document
    }
  '
}

rendered="$(helm template curie "$CHART" \
  --set-string 'agentSandbox.connectorSecrets.acme-a.CONNECTOR_TOKEN=agent-a-sentinel' \
  --set-string 'agentSandbox.connectorSecrets.acme-b.CONNECTOR_TOKEN=agent-b-sentinel' \
  --set-string 'security.networkPolicy.agentAllowedEgress.acme-a[0].cidr=0.0.0.0/0' \
  --set-string 'security.networkPolicy.agentAllowedEgress.acme-a[0].ports[0].protocol=TCP' \
  --set-string 'security.networkPolicy.agentAllowedEgress.acme-a[0].ports[0].port=443' \
  --set-string 'security.networkPolicy.agentAllowedEgress.acme-b[0].cidr=::/0' \
  --set-string 'security.networkPolicy.agentAllowedEgress.acme-b[0].ports[0].protocol=TCP' \
  --set-string 'security.networkPolicy.agentAllowedEgress.acme-b[0].ports[0].port=443' \
  2>/dev/null)"

default_render="$(helm template curie "$CHART" 2>/dev/null)"

# The generic template and pool remain the fallback for agents without a
# connector binding. Configuring two named agents must not mutate either object.
generic_template="$(require_resource "$default_render" SandboxTemplate curie-runner)"
generic_pool="$(require_resource "$default_render" SandboxWarmPool curie-runner-pool)"
[ "$generic_template" = "$(require_resource "$rendered" SandboxTemplate curie-runner)" ] \
  || fail "connectorSecrets changed the generic SandboxTemplate"
[ "$generic_pool" = "$(require_resource "$rendered" SandboxWarmPool curie-runner-pool)" ] \
  || fail "connectorSecrets changed the generic SandboxWarmPool"

for agent in acme-a acme-b; do
  secret="$(require_resource "$rendered" Secret "curie-agent-${agent}-connector-secrets")"
  template="$(require_resource "$rendered" SandboxTemplate "curie-agent-${agent}-runner")"
  pool="$(require_resource "$rendered" SandboxWarmPool "curie-agent-${agent}-runner-pool")"

  require_text "$secret" "curietech.ai/agent: \\\"?${agent}\\\"?" \
    "Secret for ${agent} lacks its agent identity label"
  require_text "$template" "curietech.ai/agent: \\\"?${agent}\\\"?" \
    "SandboxTemplate for ${agent} lacks its pod agent identity label"
  require_text "$template" "name: CONNECTOR_TOKEN" \
    "SandboxTemplate for ${agent} lacks CONNECTOR_TOKEN env delivery"
  require_text "$template" "secretKeyRef:" \
    "SandboxTemplate for ${agent} does not use secretKeyRef delivery"
  require_text "$template" "name: curie-agent-${agent}-connector-secrets" \
    "SandboxTemplate for ${agent} references the wrong connector Secret"
  require_text "$template" "key: CONNECTOR_TOKEN" \
    "SandboxTemplate for ${agent} references the wrong connector Secret key"
  require_text "$template" "optional: false" \
    "SandboxTemplate for ${agent} makes its connector secret optional"
  require_text "$pool" "sandboxTemplateRef:" \
    "SandboxWarmPool for ${agent} lacks a template reference"
  require_text "$pool" "name: curie-agent-${agent}-runner" \
    "SandboxWarmPool for ${agent} does not select its own SandboxTemplate"
done

secret_a="$(require_resource "$rendered" Secret curie-agent-acme-a-connector-secrets)"
secret_b="$(require_resource "$rendered" Secret curie-agent-acme-b-connector-secrets)"
require_text "$secret_a" 'CONNECTOR_TOKEN: "agent-a-sentinel"' \
  "acme-a Secret lacks its connector value"
require_text "$secret_b" 'CONNECTOR_TOKEN: "agent-b-sentinel"' \
  "acme-b Secret lacks its connector value"
forbid_text "$secret_a" 'agent-b-sentinel' "acme-b value leaked into acme-a Secret"
forbid_text "$secret_b" 'agent-a-sentinel' "acme-a value leaked into acme-b Secret"
forbid_text "${secret_a%%stringData:*}" 'agent-a-sentinel|agent-b-sentinel' \
  "connector value appeared in acme-a Secret metadata"
forbid_text "${secret_b%%stringData:*}" 'agent-a-sentinel|agent-b-sentinel' \
  "connector value appeared in acme-b Secret metadata"

template_a="$(require_resource "$rendered" SandboxTemplate curie-agent-acme-a-runner)"
template_b="$(require_resource "$rendered" SandboxTemplate curie-agent-acme-b-runner)"
pool_a="$(require_resource "$rendered" SandboxWarmPool curie-agent-acme-a-runner-pool)"
pool_b="$(require_resource "$rendered" SandboxWarmPool curie-agent-acme-b-runner-pool)"
forbid_text "$template_a" 'curie-agent-acme-b-connector-secrets' \
  "acme-a SandboxTemplate references acme-b Secret"
forbid_text "$template_b" 'curie-agent-acme-a-connector-secrets' \
  "acme-b SandboxTemplate references acme-a Secret"
forbid_text "$pool_a" 'curie-agent-acme-b-runner' \
  "acme-a SandboxWarmPool references acme-b SandboxTemplate"
forbid_text "$pool_b" 'curie-agent-acme-a-runner' \
  "acme-b SandboxWarmPool references acme-a SandboxTemplate"

# Neither value can enter a template, pool, metadata, or claim shaped manifest.
# The sole permitted rendered location is its own Secret stringData.
non_secret_render="$(printf '%s\n' "$rendered" | awk '
  /^---$/ {
    if (document != "" && kind != "Secret") print document
    document = ""
    kind = ""
  }
  {
    document = document $0 "\n"
    if ($0 == "kind: Secret") kind = "Secret"
  }
  END { if (document != "" && kind != "Secret") print document }
')"
forbid_text "$non_secret_render" 'agent-a-sentinel|agent-b-sentinel' \
  "connector value appeared outside its Secret"

# The scoped policies must select the normal runner labels plus exactly one agent
# label. IPv4 and IPv6 broad egress must retain their metadata exclusions.
policy_a="$(policy_for_agent "$rendered" acme-a)"
policy_b="$(policy_for_agent "$rendered" acme-b)"
[ -n "$policy_a" ] || fail "no per-agent NetworkPolicy selected acme-a"
[ -n "$policy_b" ] || fail "no per-agent NetworkPolicy selected acme-b"
require_text "$policy_a" 'app.kubernetes.io/component: runner-sandbox' \
  "per-agent policy for acme-a does not select runner sandboxes"
require_text "$policy_a" 'curietech.ai/agent: \\\"?acme-a\\\"?' \
  "per-agent policy for acme-a does not select its agent label"
require_text "$policy_b" 'app.kubernetes.io/component: runner-sandbox' \
  "per-agent policy for acme-b does not select runner sandboxes"
require_text "$policy_b" 'curietech.ai/agent: \\\"?acme-b\\\"?' \
  "per-agent policy for acme-b does not select its agent label"
require_text "$policy_a" 'cidr: 0.0.0.0/0' "acme-a policy lacks its IPv4 CIDR"
require_text "$policy_a" '169.254.0.0/16' "acme-a policy reopens IPv4 metadata"
require_text "$policy_b" 'cidr: ::/0' "acme-b policy lacks its IPv6 CIDR"
require_text "$policy_b" 'fd00:ec2::254/128' "acme-b policy reopens IPv6 metadata"
forbid_text "$policy_a" 'curietech.ai/agent: "?acme-b"?' \
  "acme-a policy selects acme-b"
forbid_text "$policy_b" 'curietech.ai/agent: "?acme-a"?' \
  "acme-b policy selects acme-a"

# Negative controls use the chart's existing fail-closed reservation and agent
# name validation paths. A malformed values key cannot become a Kubernetes
# identity merely because a values file bypassed the API.
assert_render_fails() {
  local args=("$@")
  if helm template curie "$CHART" "${args[@]}" >/dev/null 2>&1; then
    fail "invalid connector-secret input rendered instead of failing: ${args[*]}"
  fi
}

for key in ANTHROPIC_BASE_URL ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_AUTH_TOKEN CURIE_BUDGET; do
  assert_render_fails --set "agentSandbox.connectorSecrets.demo.${key}=x"
done
assert_render_fails --set 'agentSandbox.connectorSecrets.bad_name.CONNECTOR_TOKEN=x'

echo "OK: per-agent connector-secret delivery render assertions passed"
