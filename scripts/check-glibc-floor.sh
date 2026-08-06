#!/usr/bin/env bash
#
# Assert a Linux binary's glibc symbol floor (#1341).
#
#   check-glibc-floor.sh <binary> <max-allowed>      e.g. dist/curie-... 2.28
#
# glibc is backward compatible, not forward: a binary that references
# GLIBC_2.39 will not start on a host with 2.34, and the failure is total --
#
#   /lib64/libc.so.6: version `GLIBC_2.39' not found (required by curie)
#
# no subcommand runs, so a cluster becomes unoperable by its own CLI. That is
# what shipped, because both Linux targets built against the runner's headers
# and `ubuntu-latest` moved to Ubuntu 24.04 (glibc 2.39) while Amazon Linux
# 2023 -- the OS the deployment runbook provisions -- is on 2.34.
#
# Nothing caught it. Every runner that executes these artifacts is also
# `ubuntu-latest`, so CI and downstream eval lanes were green the whole time.
# The floor is therefore asserted on the artifact rather than trusted to the
# build environment: this script is the only thing standing between a runner
# image bump and a silently unusable release.
set -euo pipefail

BIN="${1:?usage: check-glibc-floor.sh <binary> <max-allowed-glibc>}"
MAX="${2:?usage: check-glibc-floor.sh <binary> <max-allowed-glibc>}"

[ -f "$BIN" ] || { echo "FAIL: no such binary: $BIN" >&2; exit 1; }

# Versioned symbol references live in .gnu.version_r and survive stripping, so
# this works on the shipped artifact rather than needing an unstripped copy.
#
# Deliberately no `mapfile` / `${arr[-1]}`: both are bash 4+, and macOS still
# ships 3.2. A guard that only runs in CI is half a guard.
refs="$(strings "$BIN" | grep -oE 'GLIBC_2\.[0-9]+' | sort -uV || true)"

if [ -z "$refs" ]; then
  # A static or musl binary references none. That is stricter than the floor,
  # not a failure.
  echo "OK: $BIN references no versioned glibc symbols (static or musl)"
  exit 0
fi

highest="$(printf '%s\n' "$refs" | tail -1)"
worst="$(printf '%s\nGLIBC_%s\n' "$highest" "$MAX" | sort -uV | tail -1)"

if [ "$worst" != "GLIBC_$MAX" ]; then
  cat >&2 <<MSG
FAIL: $BIN requires $highest, above the GLIBC_$MAX floor.

  references: $(printf "%s " $refs)

This binary will not start on a host with an older glibc, and the error names
the loader rather than curie, so it reads like a corrupt download. Amazon Linux
2023 is 2.34; RHEL/Alma 8 and Debian 10 are 2.28.

Most likely cause: the build stopped going through \`cargo zigbuild --target
<target>.<glibc>\` and fell back to the runner's own libc headers. Check the
\`glibc:\` key in the release matrix.
MSG
  exit 1
fi

echo "OK: $BIN requires at most $highest (floor GLIBC_$MAX)"
