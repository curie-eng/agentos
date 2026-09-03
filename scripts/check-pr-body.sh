#!/usr/bin/env bash
# Two guards on a pull request body.
#
# 1. Reject a literal "\\n" escape. GitHub displays that escape as text rather
#    than a line break, so a following `Closes #<issue>` is not parsed as a
#    closing keyword.
# 2. Reject AI attribution, delegating to check-commit-messages.sh
#    --message-file so both surfaces share ONE matcher. AGENTS.md forbids
#    attribution in commit messages and pull request bodies alike, but only the
#    commit half was enforced, so agent-authored bodies kept arriving with the
#    robot-emoji footer and were merged (#2225). The body is the half a human
#    reviewer sees first and the half no rebase can rewrite.
#
# Usage:
#   scripts/check-pr-body.sh <body-file>
#   scripts/check-pr-body.sh --self-test
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_PATH="$SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")"
COMMIT_GATE="$SCRIPT_DIR/check-commit-messages.sh"

usage() {
    echo "Usage: scripts/check-pr-body.sh <body-file>" >&2
    echo "       scripts/check-pr-body.sh --self-test" >&2
}

check_body_file() {
    local body_file="$1"
    local grep_status

    if [[ ! -f "$body_file" ]]; then
        echo "PR body check failed: '$body_file' is not a regular file" >&2
        return 1
    fi
    if [[ ! -r "$body_file" ]]; then
        echo "PR body check failed: '$body_file' is not readable" >&2
        return 1
    fi

    # Single quotes deliberately preserve the two literal bytes (backslash and
    # n). Real line-feed bytes do not match this fixed string.
    if LC_ALL=C grep -F -q '\n' -- "$body_file"; then
        echo "PR body check failed: found a literal \\n escape sequence." >&2
        echo "Replace each literal \\n with a real newline before opening or editing the PR." >&2
        return 1
    else
        grep_status=$?
        if ((grep_status != 1)); then
            echo "PR body check failed: could not read '$body_file' while checking for literal \\n escapes" >&2
            return 1
        fi
    fi

    if [[ ! -x "$COMMIT_GATE" && ! -r "$COMMIT_GATE" ]]; then
        echo "PR body check failed: cannot read '$COMMIT_GATE'" >&2
        return 1
    fi
    if ! bash "$COMMIT_GATE" --message-file "$body_file" >/dev/null; then
        echo "PR body check failed: the body claims AI authorship." >&2
        echo "AGENTS.md forbids AI attribution in commit messages and PR bodies alike." >&2
        return 1
    fi

    echo "PR body check passed: real newlines, no AI attribution"
}

self_test() {
    local temp_dir real_newline_body literal_escape_body attributed_body footer_no_newline_body

    temp_dir="$(mktemp -d "${TMPDIR:-/tmp}/curie-pr-body-check.XXXXXX")"
    trap 'rm -rf -- "$temp_dir"' RETURN
    real_newline_body="$temp_dir/real-newline.md"
    literal_escape_body="$temp_dir/literal-escape.md"

    attributed_body="$temp_dir/attributed.md"
    footer_no_newline_body="$temp_dir/footer-no-newline.md"

    printf 'Describe a fix.\n\nCloses #1713\n' >"$real_newline_body"
    printf 'Describe a fix.\\n\\nCloses #1713\n' >"$literal_escape_body"
    # The exact footer that reached #2225, and the same footer with no trailing
    # newline, which is the shape a body pasted from a tool actually has.
    printf 'Describe a fix.\n\n\xf0\x9f\xa4\x96 Generated with [Claude Code](https://claude.com/claude-code)\n' \
        >"$attributed_body"
    printf 'Describe a fix.\n\n\xf0\x9f\xa4\x96 Generated with [Claude Code](https://claude.com/claude-code)' \
        >"$footer_no_newline_body"

    if ! bash "$SCRIPT_PATH" "$real_newline_body" >/dev/null; then
        echo "PR body check self-test failed: real newlines were rejected" >&2
        return 1
    fi
    if bash "$SCRIPT_PATH" "$literal_escape_body" >/dev/null 2>&1; then
        echo "PR body check self-test failed: literal \\n was accepted" >&2
        return 1
    fi

    if bash "$SCRIPT_PATH" "$attributed_body" >/dev/null 2>&1; then
        echo "PR body check self-test failed: AI attribution footer was accepted" >&2
        return 1
    fi
    if bash "$SCRIPT_PATH" "$footer_no_newline_body" >/dev/null 2>&1; then
        echo "PR body check self-test failed: unterminated AI attribution footer was accepted" >&2
        return 1
    fi

    echo "PR body check self-test passed: clean body accepted, literal \\n and AI attribution rejected"
}

if [[ "${1:-}" == "--self-test" ]]; then
    if (($# != 1)); then
        usage
        exit 2
    fi
    self_test
    exit 0
fi

if (($# != 1)); then
    usage
    exit 2
fi

check_body_file "$1"
