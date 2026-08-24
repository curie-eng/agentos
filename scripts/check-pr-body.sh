#!/usr/bin/env bash
# Reject PR bodies that contain a literal "\\n" escape. GitHub displays that
# escape as text rather than a line break, so a following `Closes #<issue>` is
# not parsed as a closing keyword.
#
# Usage:
#   scripts/check-pr-body.sh <body-file>
#   scripts/check-pr-body.sh --self-test
set -euo pipefail

SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"

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

    echo "PR body check passed: no literal \\n escape sequences"
}

self_test() {
    local temp_dir real_newline_body literal_escape_body

    temp_dir="$(mktemp -d "${TMPDIR:-/tmp}/curie-pr-body-check.XXXXXX")"
    trap 'rm -rf -- "$temp_dir"' RETURN
    real_newline_body="$temp_dir/real-newline.md"
    literal_escape_body="$temp_dir/literal-escape.md"

    printf 'Describe a fix.\n\nCloses #1713\n' >"$real_newline_body"
    printf 'Describe a fix.\\n\\nCloses #1713\n' >"$literal_escape_body"

    if ! bash "$SCRIPT_PATH" "$real_newline_body" >/dev/null; then
        echo "PR body check self-test failed: real newlines were rejected" >&2
        return 1
    fi
    if bash "$SCRIPT_PATH" "$literal_escape_body" >/dev/null 2>&1; then
        echo "PR body check self-test failed: literal \\n was accepted" >&2
        return 1
    fi

    echo "PR body check self-test passed: real newlines accepted, literal \\n rejected"
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
