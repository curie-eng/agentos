#!/usr/bin/env bash
#
# Render-assertion test for the retired LiteLLM sandbox sidecar. Proves:
#
#   1. The DEFAULT full-chart render contains no LiteLLM surface in any
#      manifest (container, image, volume, or Secret). SandboxTemplate-only
#      checks also keep its ANTHROPIC_BASE_URL and image references safe.
#   2. Every truthy enablement path is refused by Helm with the same actionable
#      ADR-0037 recovery message, before deployment or inference toggles can
#      bypass the guard.
#
# Runnable locally (from anywhere) and from CI. Fails loudly.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART="$(cd "$SCRIPT_DIR/.." && pwd)"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

TPL=templates/agent-sandbox.yaml
REFUSAL='agentSandbox.runner.liteLLM.enabled is unsupported by ADR-0037: in-path LiteLLM gateways are unsupported; remove or disable agentSandbox.runner.liteLLM.enabled and use direct session-pinned provider routing.'

fail() { echo "FAIL: $*" >&2; exit 1; }

render_sandbox_template() {
  # $@ = extra --set flags. agentSandbox.deploy is on by default in values.yaml.
  helm template rel "$CHART" --show-only "$TPL" "$@"
}

expect_default_full_chart_without_litellm() {
  local output="$TMP/default-full-chart.yaml"

  helm template rel "$CHART" > "$output"

  if grep -qi 'litellm' "$output"; then
    fail "default full-chart render must NOT contain a LiteLLM container, image, config volume, or Secret surface."
  fi
  echo "  ok: default full-chart render has no retired LiteLLM surface"
}

expect_safe_sandbox_template_render() {
  local name="$1"
  shift
  local output="$TMP/$name.yaml"

  render_sandbox_template "$@" > "$output"

  if grep -qi 'litellm' "$output"; then
    fail "$name SandboxTemplate render must NOT contain a LiteLLM surface."
  fi
  if grep -q 'localhost:' "$output"; then
    fail "$name SandboxTemplate render must NOT repoint ANTHROPIC_BASE_URL at localhost."
  fi
  if grep -q 'main-stable' "$output"; then
    fail "$name SandboxTemplate render must NOT contain a mutable main-stable image reference."
  fi
  echo "  ok: $name SandboxTemplate render has no retired LiteLLM or localhost surface"
}

expect_refused() {
  local name="$1"
  shift
  local stdout="$TMP/$name.stdout"
  local stderr="$TMP/$name.stderr"

  if render_sandbox_template "$@" > "$stdout" 2> "$stderr"; then
    fail "$name must be refused when agentSandbox.runner.liteLLM.enabled is truthy."
  fi
  if ! grep -Fq -- "$REFUSAL" "$stdout" "$stderr"; then
    fail "$name failed without the required LiteLLM refusal. Expected exact message: $REFUSAL"
  fi
  echo "  ok: $name was refused with the ADR-0037 recovery message"
}

echo "=== Assertion 1: supported renders omit the retired LiteLLM surface ==="
expect_default_full_chart_without_litellm
expect_safe_sandbox_template_render default
expect_safe_sandbox_template_render explicit-false \
  --set agentSandbox.runner.liteLLM.enabled=false
expect_safe_sandbox_template_render explicit-null \
  --set agentSandbox.runner.liteLLM=null

echo "=== Assertion 2: every truthy LiteLLM enablement is refused ==="
expect_refused enabled \
  --set agentSandbox.runner.liteLLM.enabled=true
expect_refused sandbox-disabled \
  --set agentSandbox.runner.liteLLM.enabled=true \
  --set agentSandbox.deploy=false
expect_refused inference-enabled \
  --set agentSandbox.runner.liteLLM.enabled=true \
  --set inference.deploy=true
expect_refused both-bypasses \
  --set agentSandbox.runner.liteLLM.enabled=true \
  --set agentSandbox.deploy=false \
  --set inference.deploy=true
expect_refused string-enabled \
  --set-string agentSandbox.runner.liteLLM.enabled=true

echo
echo "PASS: LiteLLM remains absent by default and all truthy enablement is fail-closed."
