#!/usr/bin/env bash
# AI-attribution gate on commit messages (issue #962). AGENTS.md's branch and
# commit conventions forbid naming an AI assistant in a commit message and forbid
# `Co-Authored-By` lines referencing AI, but nothing enforced it and it landed on
# main twice in one day (#952, #953). This is that enforcement.
#
# WHAT IT FLAGS -- attribution, not mention. That distinction is the whole design:
# 73 of the last 400 commit bodies legitimately contain the string "claude"
# (`cli/CLAUDE.md`, `claude_agent_sdk`, `harness.claude`, "the Claude harness",
# `claude-sonnet-5`), so a gate that failed on the NAME would red-flag a large
# share of honest commits and be switched off within a week. Zero of those 400
# subjects name an assistant; the real violation is a precise trailer:
#
#     Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
#
# So three rules, each aimed at a claim of authorship:
#   A. a `Co-Authored-By:` trailer naming an assistant or a vendor no-reply address
#   B. the robot-emoji "Generated with" footer these tools append
#   C. an attribution phrase ("generated with", "written by", ...) on the same line
#      as a BARE assistant name -- bare meaning not part of an identifier, so
#      `claude_agent_sdk`, `CLAUDE.md`, `claude-sonnet-5` and `harness.claude`
#      never trip it.
#
# Usage:
#   scripts/check-commit-messages.sh [<range>]   # default: origin/main..HEAD
#   scripts/check-commit-messages.sh --self-test # prove the matcher is not vacuous
set -euo pipefail

# A bare assistant name: not preceded by [._/-] and not followed by [._-], so
# every technical identifier form is excluded by construction.
BARE_ASSISTANT='(^|[^A-Za-z0-9_./-])(claude|codex|copilot|chatgpt|gemini|devin)([^A-Za-z0-9_.-]|$)'
ATTRIBUTION_PHRASE='(generated|authored|written|created|assisted|produced)[[:space:]]+(with|by)'
COAUTHOR_AI='^[[:space:]]*co-authored-by:.*(claude|codex|copilot|chatgpt|gemini|devin|anthropic|openai|noreply@anthropic\.com)'
ROBOT_FOOTER=$'\xf0\x9f\xa4\x96'  # the robot emoji these tools append

# Echo the reason a line violates the convention, or nothing when it is clean.
offending_reason() {
    local line="$1"
    if printf '%s' "$line" | grep -qiE "$COAUTHOR_AI"; then
        echo "AI Co-Authored-By trailer"
        return
    fi
    if printf '%s' "$line" | grep -qF "$ROBOT_FOOTER"; then
        echo "robot-emoji AI generation footer"
        return
    fi
    if printf '%s' "$line" | grep -qiE "$ATTRIBUTION_PHRASE" &&
        printf '%s' "$line" | grep -qiE "$BARE_ASSISTANT"; then
        echo "AI authorship attribution"
        return
    fi
}

self_test() {
    local failures=0

    # MUST be flagged: real attribution, including the exact trailer that landed.
    local bad=(
        "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
        "co-authored-by: Codex <noreply@openai.com>"
        "${ROBOT_FOOTER} Generated with [Claude Code](https://claude.com/claude-code)"
        "Generated with Claude"
        "This patch was written by GitHub Copilot"
    )
    # MUST NOT be flagged: real lines from this repo's history and its vocabulary.
    local good=(
        "Update ARCHITECTURE.md section 8, apps/ui/README.md, and apps/ui/CLAUDE.md"
        "(harness.claude -> curie_runner.plugin -> claude_agent_sdk), which is what pinned"
        "Rebuild _KNOWN_BUILTIN_TOOLS from the tool registry in the Claude harness"
        "pin claude-agent-sdk==0.2.110 and claude-sonnet-5"
        "the spans generated with claude_agent_sdk stay schema-clean"
        "Co-Authored-By: Alex Rao <arao@curietech.net>"
    )

    local line reason
    for line in "${bad[@]}"; do
        reason="$(offending_reason "$line")"
        if [[ -z "$reason" ]]; then
            echo "self-test FAIL: should have been flagged but was not: $line" >&2
            failures=$((failures + 1))
        fi
    done
    for line in "${good[@]}"; do
        reason="$(offending_reason "$line")"
        if [[ -n "$reason" ]]; then
            echo "self-test FAIL: legitimate line flagged as '$reason': $line" >&2
            failures=$((failures + 1))
        fi
    done

    if ((failures)); then
        echo "commit-message gate self-test: $failures case(s) wrong" >&2
        exit 1
    fi
    echo "commit-message gate self-test: ${#bad[@]} caught, ${#good[@]} correctly ignored"
}

if [[ "${1:-}" == "--self-test" ]]; then
    self_test
    exit 0
fi

range="${1:-origin/main..HEAD}"
mapfile -t commits < <(git log --format=%H "$range" 2>/dev/null || true)
if ((${#commits[@]} == 0)); then
    echo "no commits in range '$range'; nothing to check"
    exit 0
fi

violations=0
for sha in "${commits[@]}"; do
    while IFS= read -r line; do
        reason="$(offending_reason "$line")"
        if [[ -n "$reason" ]]; then
            if ((violations == 0)); then
                echo "AGENTS.md forbids naming an AI assistant in commit messages and" >&2
                echo "forbids Co-Authored-By lines referencing AI (#962). Offending:" >&2
                echo "" >&2
            fi
            printf '  %s (%s)\n    %s: %s\n' \
                "$(git log -1 --format=%h "$sha")" \
                "$(git log -1 --format=%s "$sha")" \
                "$reason" "$line" >&2
            violations=$((violations + 1))
        fi
    done < <(git log -1 --format=%B "$sha")
done

if ((violations)); then
    echo "" >&2
    echo "Amend or rebase to drop the line(s) above, then force-push the branch." >&2
    echo "Check locally before pushing: scripts/check-commit-messages.sh" >&2
    exit 1
fi

echo "commit messages clean: ${#commits[@]} commit(s) in $range carry no AI attribution"
