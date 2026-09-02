#!/usr/bin/env python3
"""Build the nil-unsafe chart mutants used by the released-upgrade CI rung."""

from __future__ import annotations

import re
import sys
from pathlib import Path


def remove_placement_nil_guard(path: Path) -> None:
    text = path.read_text()
    block_re = re.compile(
        r'{{-?\s*define "curie\.placement\.class"\s*-?}}.*?{{-?\s*end\s*-?}}',
        re.DOTALL,
    )
    match = block_re.search(text)
    if match is None or "| default dict" not in match.group(0):
        raise SystemExit(
            "nil-unsafe negative control could not find the placement nil guard"
        )
    mutated = match.group(0).replace("| default dict", "", 1)
    path.write_text(text[: match.start()] + mutated + text[match.end() :])


def restore_nil_unsafe_managed_secret(path: Path) -> None:
    text = path.read_text()
    start_marker = '{{- define "curie.managedSecret" -}}'
    end_marker = '{{/* ---- Shared first-party-app environment fragments ---- */}}'
    start = text.find(start_marker)
    end = text.find(end_marker, start)
    if start == -1 or end == -1:
        raise SystemExit("nil-unsafe negative control could not find curie.managedSecret")

    nil_unsafe = r'''{{- define "curie.managedSecret" -}}
{{- if eq (toString .root.Values.security.allowDevDefaults) "true" -}}
{{- .value -}}
{{- else if ne (toString .value) (toString .default) -}}
{{- .value -}}
{{- else if hasKey .existingData .key -}}
{{- index .existingData .key | b64dec -}}
{{- else if .hex -}}
{{- randAlphaNum 32 | sha256sum -}}
{{- else -}}
{{- randAlphaNum 32 -}}
{{- end -}}
{{- end -}}'''
    path.write_text(text[:start] + nil_unsafe + "\n\n" + text[end:])


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(
            "usage: make-upgrade-mutants.py PLACEMENT_HELPERS MANAGED_SECRET_HELPERS"
        )
    remove_placement_nil_guard(Path(sys.argv[1]))
    restore_nil_unsafe_managed_secret(Path(sys.argv[2]))


if __name__ == "__main__":
    main()
