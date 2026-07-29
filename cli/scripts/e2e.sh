#!/bin/bash
# Scripted E2E for the curie CLI (task I1 done-when).
#
# Round-trips a synthetic event through a real local runner container with zero
# Slack involved: init a bundle, start the runner (fake model, offline by
# default; live model under CURIE_E2E_LIVE=1), send a message and stream the
# NDJSON reply, run the eval cases, stop. This is rung 1 (skill) of the
# cold-start parity ladder (issue #690, cli/scripts/e2e-ladder.sh); the
# ladder's local rung (`local deploy` -> `local message` with a real reply
# assertion) covers deploying a bundle against a running platform API, so this
# script no longer does so itself (issue #694).
#
# Requirements: docker, a curie-runner image (build per runner/README.md),
# and a cargo toolchain (or $CURIE_BIN). Run from anywhere:
#
#   bash cli/scripts/e2e.sh
#
# Env knobs:
#   CURIE_E2E_IMAGE     runner image (default curie-runner)
#   CURIE_E2E_PORT      host port (default 7245)
#   CURIE_E2E_NETWORK   docker network to join (e.g. curie_default)
#   CURIE_E2E_OTEL      OTLP endpoint (e.g. http://otel-collector:4318)
#   CURIE_E2E_LIVE      1 = real model, requiring a credential in the
#                         environment (ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN,
#                         or CURIE_CREDENTIALS); default 0 runs the runner's
#                         scripted fake model, offline and credential-free. This
#                         is the SAME env var cli/scripts/e2e-ladder.sh sets for
#                         its own local and cluster rungs, so a single
#                         CURIE_E2E_LIVE=1 now runs every rung live.
#   CURIE_BIN           path to a prebuilt curie binary (skip cargo build)
set -euo pipefail

CLI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${CURIE_E2E_IMAGE:-curie-runner}"
PORT="${CURIE_E2E_PORT:-7245}"
CONTAINER="curie-e2e-runner"
LIVE="${CURIE_E2E_LIVE:-0}"

echo "=== Resolve the curie binary ==="
if [[ -n "${CURIE_BIN:-}" && -x "${CURIE_BIN:-}" ]]; then
    # Absolutize: this script cd's into a scaffolded bundle directory before
    # invoking the binary, so a relative $CURIE_BIN (as the ladder and CI
    # pass) must be pinned to an absolute path here or it stops resolving
    # after the cd.
    BIN="$(cd "$(dirname "$CURIE_BIN")" && pwd)/$(basename "$CURIE_BIN")"
    echo "using prebuilt binary: $BIN"
else
    (cd "$CLI_DIR" && cargo build --release --quiet)
    BIN="$CLI_DIR/target/release/curie"
fi
"$BIN" --version

echo
echo "=== Resolve model mode ==="
if [[ "$LIVE" == "1" ]]; then
    if [[ -z "${ANTHROPIC_API_KEY:-}" && -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" && -z "${CURIE_CREDENTIALS:-}" ]]; then
        echo "error: CURIE_E2E_LIVE=1 needs a model credential in the environment, and none is set." >&2
        echo "fix: export ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN, or CURIE_CREDENTIALS, or drop CURIE_E2E_LIVE to run sealed against the fake model." >&2
        exit 1
    fi
    echo "model mode: LIVE (real model; \`skill up\` forwards the ambient credential)"
else
    echo "model mode: FAKE (sealed; --fake-model, offline, no credential)"
fi

WORKDIR="$(mktemp -d)"
cleanup() {
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    rm -rf "$WORKDIR"
}
trap cleanup EXIT

echo
echo "=== curie init --from-spec (non-interactive, agent-authored spec) ==="
# AC #2: a coding agent writes a spec, the CLI scaffolds a runnable bundle from
# it with zero prompts, and the spec-scaffolded evals/cases.json runs on the eval
# path. The grader is falsifiable (it requires the agent to name itself), NOT
# tuned to the fake model's canned "all done" reply: a grader written to match
# the fake manufactures a green, and CI carrying one is exactly why #612 went
# unnoticed. Under --fake-model the grader is never consulted at all -- the run
# reports the non-graded plumbing_ok (ADR-0055).
cat > "$WORKDIR/agent-spec.json" <<'EOF'
{
  "name": "deal-desk",
  "description": "Prices and reviews deal desk requests.",
  "skills": [
    {
      "name": "deal-desk",
      "description": "Invoke when a rep submits a pricing exception request.",
      "allowed_tools": ["WebSearch", "WebFetch"],
      "instructions": "Price the exception against the guardrails, then summarize the decision.\n"
    }
  ],
  "evals": [
    {
      "id": "introduces-itself",
      "input": "In one short sentence, introduce yourself as the deal-desk agent.",
      "grader": { "kind": "contains", "expected": "deal-desk", "case_sensitive": false }
    }
  ]
}
EOF
"$BIN" init --from-spec "$WORKDIR/agent-spec.json" --dir "$WORKDIR/deal-desk"

cd "$WORKDIR/deal-desk"

# A second suite for the explicit `--cases` leg. Its graders are real domain
# graders, deliberately NOT matched to the fake-model script's canned "all done"
# reply: this run is offline under --fake-model, so the graders are never
# consulted and the suite reports plumbing_ok. Writing them to match the fake
# would only re-create the bypass that let #612 ship green.
cat > evals/e2e-cases.json <<'EOF'
{
  "name": "e2e",
  "cases": [
    {
      "id": "introduces-itself",
      "input": "In one short sentence, introduce yourself as the deal-desk agent.",
      "grader": { "kind": "contains", "expected": "deal-desk", "case_sensitive": false }
    },
    {
      "id": "names-its-domain",
      "input": "What kind of requests do you handle?",
      "grader": { "kind": "contains", "expected": "pricing", "case_sensitive": false }
    }
  ]
}
EOF

echo
if [[ "$LIVE" == "1" ]]; then
    echo "=== curie skill up (live model) ==="
else
    echo "=== curie skill up (fake model, offline) ==="
fi
START_ARGS=(--plugin-dir . --image "$IMAGE" --port "$PORT" --name "$CONTAINER")
if [[ "$LIVE" == "1" ]]; then
    : # Real credential resolution (CURIE_CREDENTIALS, else the ambient SDK
      # creds) happens inside `skill up` itself once --fake-model is omitted;
      # see commands::select_passthrough_env.
else
    START_ARGS+=(--fake-model)
fi
if [[ -n "${CURIE_E2E_NETWORK:-}" ]]; then
    START_ARGS+=(--network "$CURIE_E2E_NETWORK")
fi
if [[ -n "${CURIE_E2E_OTEL:-}" ]]; then
    START_ARGS+=(--otel-endpoint "$CURIE_E2E_OTEL")
fi
"$BIN" skill up "${START_ARGS[@]}"

echo
echo "=== curie skill status ==="
"$BIN" skill status

echo
echo "=== curie skill status --json (#1087: capture the initial bundle digest) ==="
STATUS_JSON_1="$("$BIN" skill status --json)"
printf '%s\n' "$STATUS_JSON_1"
DIGEST_1="$(printf '%s' "$STATUS_JSON_1" | python3 -c 'import json,sys; print(json.load(sys.stdin)["bundle_digest"])')"
if [[ -z "$DIGEST_1" || "$DIGEST_1" == "None" ]]; then
    echo "error: expected \`skill status --json\` to report a non-null bundle_digest for a just-booted runner; got '$DIGEST_1'." >&2
    echo "payload: $STATUS_JSON_1" >&2
    exit 1
fi
echo "initial bundle digest: $DIGEST_1"

echo
echo "=== #1087 AC1: edit the source after boot, confirm the running container keeps the ORIGINAL snapshot ==="
SKILL_FILE_PATH="$WORKDIR/deal-desk/skills/deal-desk/SKILL.md"
SKILL_FILE_ORIGINAL="$(cat "$SKILL_FILE_PATH")"
MARKER="e2e-post-boot-source-edit-$$"
echo "$MARKER" >> "$SKILL_FILE_PATH"

MOUNTED_SKILL_MD="$(docker exec "$CONTAINER" cat /plugin/skills/deal-desk/SKILL.md)"
if grep -qF "$MARKER" <<<"$MOUNTED_SKILL_MD"; then
    echo "error: the running container's /plugin/skills/deal-desk/SKILL.md contains the marker '$MARKER', added to the HOST source after boot." >&2
    echo "expected: the runner keeps executing the immutable snapshot it packed at \`skill up\` time, unaffected by a later host edit." >&2
    exit 1
fi
echo "confirmed: the running container's /plugin mount does not see the post-boot host edit"

STATUS_JSON_1B="$("$BIN" skill status --json)"
DIGEST_1B="$(printf '%s' "$STATUS_JSON_1B" | python3 -c 'import json,sys; print(json.load(sys.stdin)["bundle_digest"])')"
if [[ "$DIGEST_1B" != "$DIGEST_1" ]]; then
    echo "error: \`skill status --json\` reported bundle_digest '$DIGEST_1B' after a host-only source edit; expected it to stay '$DIGEST_1'." >&2
    echo "expected: the digest identifies the snapshot mounted at boot, not the current (mutated) source." >&2
    exit 1
fi
echo "confirmed: bundle_digest is unchanged by the post-boot host edit ($DIGEST_1)"

echo
echo "=== curie skill message (synthetic event, streamed NDJSON reply) ==="
"$BIN" skill message "@curie can we approve the Meridian deal at 18% discount?"

echo
echo "=== curie skill eval (spec-scaffolded evals/cases.json) ==="
# No --cases: exercise the evals/cases.json the --from-spec scaffold wrote,
# proving spec -> bundle -> skill eval passes end to end offline (AC #2).
"$BIN" skill eval

echo
echo "=== curie skill eval (explicit cases file) ==="
"$BIN" skill eval --cases evals/e2e-cases.json

echo
echo "=== curie skill down ==="
"$BIN" skill down

echo
echo "=== #1087 AC3: re-up on the mutated source, confirm a NEW bundle digest is packed ==="
"$BIN" skill up "${START_ARGS[@]}"
STATUS_JSON_2="$("$BIN" skill status --json)"
DIGEST_2="$(printf '%s' "$STATUS_JSON_2" | python3 -c 'import json,sys; print(json.load(sys.stdin)["bundle_digest"])')"
if [[ -z "$DIGEST_2" || "$DIGEST_2" == "None" ]]; then
    echo "error: expected \`skill status --json\` to report a non-null bundle_digest after re-\`skill up\`; got '$DIGEST_2'." >&2
    echo "payload: $STATUS_JSON_2" >&2
    exit 1
fi
if [[ "$DIGEST_2" == "$DIGEST_1" ]]; then
    echo "error: bundle_digest is still '$DIGEST_1' after a fresh \`skill up\` on a source that changed since the first boot." >&2
    echo "expected: a restart packages the changed source as a NEW digest." >&2
    exit 1
fi
echo "confirmed: re-up on changed source produced a new bundle digest ($DIGEST_1 -> $DIGEST_2)"

echo
echo "=== restore the source and tear down the second runner ==="
printf '%s\n' "$SKILL_FILE_ORIGINAL" > "$SKILL_FILE_PATH"
"$BIN" skill down

echo
echo "=== #1087: confirm teardown released the bundle snapshot ==="
SNAPSHOT_ROOT="$WORKDIR/deal-desk/.curie/snapshots"
if [[ -d "$SNAPSHOT_ROOT" ]]; then
    REMAINING="$(find "$SNAPSHOT_ROOT" -mindepth 1 -maxdepth 1)"
    if [[ -n "$REMAINING" ]]; then
        echo "error: expected $SNAPSHOT_ROOT to be empty (or absent) after the final \`skill down\`, but it still contains:" >&2
        echo "$REMAINING" >&2
        exit 1
    fi
fi
echo "confirmed: no bundle snapshot left under $SNAPSHOT_ROOT"

echo
echo "E2E PASS"
