#!/usr/bin/env python3
"""Assert every example bundle's write path stays gated, and its ceilings agree.

Two failures this catches, both observed rather than imagined.

**A write connector enabled without its gate.** The sre-bot bundle ships its
write connector commented out precisely because a gate naming an undeclared
connector fails bundle validation for everyone who never enables it -- so
enabling the connector and declaring the gate are two edits, in two files, and
nothing made them happen together. On a real install they did not: the connector
was enabled, the gate was not, and a tool able to roll production Deployments ran
for four and a half days with nothing in front of it. Uncommenting is the easy
half; the gate is the half that gets forgotten, and forgetting is silent.

**Two ceilings that drifted apart.** The connector's `K8S_WRITE_ALLOWLIST` and
the Role's `resourceNames` are both allowlists of what may be written, in
different files, edited by people thinking about different things. On the same
install they disagreed for four days after the target cluster changed: RBAC
permitted three workloads the allowlist forbade, and the allowlist permitted one
that did not exist there. That direction was harmless -- the intersection was
empty -- but the reverse is not. An allowlist entry with no matching
`resourceName` is a 403 *after* a human approved the call, which spends the
approval and delivers nothing.

A bundle with no write connector declared passes both checks trivially, which is
the shipped state and stays cheap.
"""

import json
import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent


def bundles() -> list[pathlib.Path]:
    return sorted(p.parent.parent for p in ROOT.glob("examples/*/.claude-plugin/plugin.json"))


def declared_gates(bundle: pathlib.Path) -> set[str]:
    manifest = json.loads((bundle / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    return {
        g["gate"].strip()
        for g in (manifest.get("approvalPolicy") or {}).get("gates", [])
        if g.get("gate")
    }


def write_tools(bundle: pathlib.Path, connector: str) -> set[str]:
    """Tool names a connector's own source declares as NOT read-only.

    Source-derived rather than runtime-derived on purpose: this gate runs on a
    checkout, with no cluster and no image. It is deliberately conservative --
    it only knows about connectors this repository carries the source for, which
    is exactly the set an example bundle can enable.
    """
    src = bundle / "connectors" / connector / "server.py"
    if not src.is_file():
        return set()
    text = src.read_text(encoding="utf-8")
    if "readOnlyHint=False" not in text.replace(" ", ""):
        return set()
    # Tools are the @mcp.tool-decorated defs; the annotation set is per-file
    # here, so a file that declares a write annotation declares write tools.
    return set(re.findall(r"@mcp\.tool\([^)]*\)\s*\ndef\s+(\w+)", text))


def check(bundle: pathlib.Path) -> list[str]:
    problems: list[str] = []
    conn_file = bundle / "connectors.yaml"
    if not conn_file.is_file():
        return problems
    loaded = yaml.safe_load(conn_file.read_text(encoding="utf-8")) or {}
    connectors = loaded.get("connectors") or {}
    gates = declared_gates(bundle)

    for name, spec in connectors.items():
        tools = write_tools(bundle, name)
        for tool in sorted(tools):
            expected = f"mcp__{name}__{tool}"
            if expected not in gates:
                problems.append(
                    f"{bundle.name} declares connector {name!r}, whose {tool} is not "
                    f"read-only, but no approvalPolicy gate names {expected}. Enabling the "
                    f"connector and declaring the gate are two edits; this is the one that "
                    f"gets forgotten."
                )

        # Ceilings, only where both halves exist.
        allow_raw = (spec.get("env") or {}).get("K8S_WRITE_ALLOWLIST")
        role_file = bundle / "manifests" / "write-role.yaml"
        if allow_raw is None or not role_file.is_file():
            continue
        allow = {e.strip() for e in str(allow_raw).split(",") if e.strip()}
        rbac: set[str] = set()
        for doc in yaml.safe_load_all(role_file.read_text(encoding="utf-8")):
            if doc and doc.get("kind") == "Role":
                ns = doc["metadata"]["namespace"]
                for rule in doc.get("rules", []):
                    rbac.update(f"{ns}/{n}" for n in (rule.get("resourceNames") or []))
        # A placeholder allowlist in a shipped-off example is not a drift.
        if any(c in str(allow_raw) for c in "<>"):
            continue
        for t in sorted(allow - rbac):
            problems.append(
                f"{bundle.name} allows {t} in K8S_WRITE_ALLOWLIST with no matching "
                f"resourceName in manifests/write-role.yaml -- the tool accepts it and the "
                f"API server 403s, after a human approved the call."
            )
        for t in sorted(rbac - allow):
            problems.append(
                f"{bundle.name} grants {t} in manifests/write-role.yaml with no matching "
                f"allowlist entry -- permitted, unreachable through the tool, and unexplained."
            )
    return problems


def main() -> int:
    found = bundles()
    if not found:
        print("no bundles under examples/; an empty match would pass vacuously", file=sys.stderr)
        return 1
    problems: list[str] = []
    for b in found:
        p = check(b)
        status = "ok" if not p else f"{len(p)} problem(s)"
        print(f"  {b.name:<28} {status}")
        problems += p
    if problems:
        print("", file=sys.stderr)
        for problem in problems:
            print(f"FAIL: {problem}", file=sys.stderr)
        return 1
    print("every example bundle's write path is gated and its ceilings agree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
