#!/usr/bin/env bash
#
# Render-assertion test for the retired LiteLLM sandbox sidecar. Proves:
#
#   1. DEFAULT and explicit false renders preserve the supported sandbox path:
#      no LiteLLM container, image, config volume, Secret surface, localhost
#      ANTHROPIC_BASE_URL override, or mutable main-stable image reference.
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

render() {
  # $@ = extra --set flags. agentSandbox.deploy is on by default in values.yaml.
  helm template rel "$CHART" --show-only "$TPL" "$@"
}

expect_safe_render() {
  local name="$1"
  shift
  local output="$TMP/$name.yaml"

  render "$@" > "$output"

  if grep -qi 'litellm' "$output"; then
    fail "$name render must NOT contain a LiteLLM container, image, config volume, or Secret surface."
  fi
  if grep -q 'localhost:' "$output"; then
    fail "$name render must NOT repoint ANTHROPIC_BASE_URL at localhost."
  fi
  if grep -q 'main-stable' "$output"; then
    fail "$name render must NOT contain a mutable main-stable image reference."
  fi
  echo "  ok: $name render has no retired LiteLLM or localhost surface"
}

expect_refused() {
  local name="$1"
  shift
  local stdout="$TMP/$name.stdout"
  local stderr="$TMP/$name.stderr"

  if render "$@" > "$stdout" 2> "$stderr"; then
    fail "$name must be refused when agentSandbox.runner.liteLLM.enabled is truthy."
  fi
  if ! grep -Fq -- "$REFUSAL" "$stdout" "$stderr"; then
    fail "$name failed without the required LiteLLM refusal. Expected exact message: $REFUSAL"
  fi
  echo "  ok: $name was refused with the ADR-0037 recovery message"
}

echo "=== Assertion 1: supported renders omit the retired LiteLLM surface ==="
expect_safe_render default
expect_safe_render explicit-false \
  --set agentSandbox.runner.liteLLM.enabled=false

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
