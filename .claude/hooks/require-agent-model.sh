#!/usr/bin/env bash
# PreToolUse Agent gate: a sub-agent spawn must name its model explicitly.
#
# WHY THIS IS A HOOK AND NOT A LINE IN AGENTS.md. It was a line in AGENTS.md,
# and it did not hold. A spawn that omits `model` inherits the parent session's
# model. When the parent is an expensive tier, every sub-agent in the fan-out
# silently bills at that tier, and nothing in the transcript says so: the spawn
# looks identical to a correct one. One measured incident ran a fleet on the
# parent's tier for 24 hours at roughly $109 API-equivalent before anyone
# noticed, and it was caught by a human asking "have you been using fable or
# opus to run subagents?", not by any gate. CI cannot see this -- a subagent
# spawn is a runtime event that leaves no artifact in the diff -- so the only
# place to catch it is here, at the call.
#
# WHY BLOCKING IS SAFE HERE. Blocking gates fail when the signal is ambiguous
# and the fix is unclear, which is how two of them died in a sibling repo. This
# signal is neither: the `model` field is present or it is not, and the fix is
# to add it and retry. There is no state to reconcile and no window to
# misjudge, so the failure mode costs one turn.
#
# ESCAPE HATCH. Export CLAUDE_ALLOW_INHERITED_MODEL=1 in the launching shell
# for a deliberate inherit. Not in settings.json: an opt-out that lives in
# committed config is an opt-out for everyone, forever.
set -uo pipefail

# Negative control. AGENTS.md requires a new gate to demonstrate by execution
# that it rejects a violating input, so the suite runs the real script over real
# payloads rather than asserting anything about its source.
if [ "${1:-}" = "--self-test" ]; then
  self="${BASH_SOURCE[0]}"
  fails=0
  run() { printf '%s' "$1" | env -u CLAUDE_ALLOW_INHERITED_MODEL bash "$self" >/dev/null 2>&1; echo $?; }

  expect() { # expect <want> <label> <payload>
    got=$(run "$3")
    if [ "$got" != "$2" ]; then
      echo "  FAIL $1 (want exit $2, got $got)" >&2
      fails=$((fails + 1))
    fi
  }

  echo "== agent-model gate: self-test (negative control) =="
  expect "spawn with no model is blocked" 2 \
    '{"tool_name":"Agent","tool_input":{"prompt":"x","subagent_type":"general-purpose"}}'
  expect "spawn with empty model is blocked" 2 \
    '{"tool_name":"Agent","tool_input":{"prompt":"x","model":"   "}}'
  expect "spawn with a model passes" 0 \
    '{"tool_name":"Agent","tool_input":{"prompt":"x","model":"opus"}}'
  expect "a non-Agent tool is untouched" 0 \
    '{"tool_name":"Bash","tool_input":{"command":"ls"}}'
  expect "malformed json is untouched" 0 'not json at all'
  expect "empty stdin is untouched" 0 ''

  got=$(printf '%s' '{"tool_name":"Agent","tool_input":{"prompt":"x"}}' \
    | CLAUDE_ALLOW_INHERITED_MODEL=1 bash "$self" >/dev/null 2>&1; echo $?)
  if [ "$got" != "0" ]; then
    echo "  FAIL escape hatch does not release the gate (got exit $got)" >&2
    fails=$((fails + 1))
  fi

  if [ "$fails" -ne 0 ]; then
    echo "SELF-TEST FAILED: $fails case(s)." >&2
    exit 1
  fi
  echo "OK: self-test passed (7 cases, blocking and non-blocking paths both proven)."
  exit 0
fi

input=$(cat 2>/dev/null || true)   # drain stdin first; a closed pipe is not an error
[ -z "$input" ] && exit 0

[ "${CLAUDE_ALLOW_INHERITED_MODEL:-}" = "1" ] && exit 0

# python3 rather than jq: jq ships on neither a stock macOS nor a stock Ubuntu
# runner, and this repo already requires python everywhere.
model=$(printf '%s' "$input" | python3 -c '
import json, sys
try:
    payload = json.load(sys.stdin)
except (json.JSONDecodeError, ValueError):
    print("__SKIP__")    # not our payload; stay out of the way
    sys.exit(0)
if not isinstance(payload, dict) or payload.get("tool_name") != "Agent":
    print("__SKIP__")
    sys.exit(0)
value = payload.get("tool_input", {}).get("model")
print(value.strip() if isinstance(value, str) and value.strip() else "")
' 2>/dev/null) || exit 0

[ "$model" = "__SKIP__" ] && exit 0
[ -n "$model" ] && exit 0

cat >&2 <<'MSG'
Blocked: this Agent spawn does not set `model`.

A spawn with no `model` inherits the parent session's model. The whole fan-out
then bills at the parent's tier with nothing in the transcript to show it. That
has already cost this project a day of silent spend once.

Fix: pass `model` explicitly on the Agent call.
  - judgement-bearing work (planning, review, implementation): the default tier
  - read-only locate, fetch, or report spawns: the cheap tier

An agent definition's frontmatter pin does NOT satisfy this: a spawn-time model
overrides frontmatter, so the pin is only a default, never a guarantee.

Deliberate inherit: export CLAUDE_ALLOW_INHERITED_MODEL=1 in your shell.
MSG
exit 2
