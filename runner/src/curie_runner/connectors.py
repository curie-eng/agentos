"""Mount the connectors a bundle declared, so the author writes no URL.

ADR-0086 decided the bundle declares connectors and Curie derives, among other
things, "the URL it writes into the agent's MCP configuration". This is that
write. The author declares intent in ``connectors.yaml``; the URL of the Service
Curie created never appears in the repository.

That matters because the URL is unknowable to the author. It is
``<release>-<agent>-mcp-<connector>.<namespace>.svc.cluster.local``, and the
release name, the agent name, and the namespace are all install-time facts
assigned by whoever ran ``cluster up`` and ``cluster deploy``. Since #1116 the
name is agent-scoped, so a hand-written URL is now *guaranteed* wrong for at
least one of two agents sharing a bundle.

Derivation reuses ``plugin_format.connector_render.mcp_entry`` -- the same
function the API calls when it renders the Service. One function, so the URL the
agent dials and the Service that exists cannot disagree. Re-deriving the format
here would be a second copy of that rule, free to drift, and the failure would
be a connection refused at turn time.

The entries ride ``ClaudeAgentOptions.mcp_servers``, alongside Curie's approval
and state servers -- platform-supplied servers, which is exactly what these are.
Nothing rewrites ``.mcp.json``; the bundle stays the read-only artifact that was
deployed.

Name collisions cannot reach here: ``plugin_format`` rejects a bundle that
declares one name in both ``connectors.yaml`` and its MCP config (#1118), so
there is no precedence to resolve at runtime.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from plugin_format.connector_render import mcp_entry, unhosted_mcp_entry
from plugin_format.connectors import ConnectorsFile, validate_connectors

logger = logging.getLogger(__name__)

CONNECTORS_FILE = "connectors.yaml"


def _read(plugin_dir: str | Path) -> ConnectorsFile | None:
    path = Path(plugin_dir) / CONNECTORS_FILE
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        # Deploy already validated this file, so reaching here means the bundle
        # changed underneath us. Log and mount nothing rather than fail the
        # turn: the agent loses the connector's tools, which is visible, where a
        # crashed boot loses the whole session.
        logger.warning("connectors.yaml unreadable, mounting no connectors: %s", exc)
        return None
    parsed, errors = validate_connectors(data)
    if errors or parsed is None:
        logger.warning(
            "connectors.yaml did not validate, mounting no connectors: %s",
            "; ".join(code for code, _ in errors),
        )
        return None
    return parsed


def derive_mcp_servers(
    plugin_dir: str | Path | None,
    *,
    release: str | None,
    agent: str | None,
    namespace: str | None,
) -> dict[str, Any]:
    """The MCP server entries for this bundle's declared connectors.

    Empty when the bundle declares none, or when the connector scope is absent.
    An absent scope is not a degraded boot: the ``skill`` tier hosts nothing, so
    there is no Service to point at and a declared connector is correctly not
    exercisable there (#1093). A hosted connector declared with no scope is
    logged, because on a cluster that combination means the worker stopped
    sending the scope and the agent has silently lost its tools.
    """

    if plugin_dir is None:
        return {}
    declared = _read(plugin_dir)
    if declared is None or not declared.connectors:
        return {}

    if not (release and agent and namespace):
        # The scope is emitted as a set or not at all (BootEnv, ACI 0.2.8), so
        # this is "no scope", never a partial one.
        stranded = sorted(
            name
            for name, spec in declared.connectors.items()
            if spec.is_hosted and not spec.unhosted_url
        )
        if stranded:
            logger.info(
                "no connector scope on the boot env and no unhosted_url; these are "
                "declared but not exercisable in this tier (%s)",
                ", ".join(stranded),
            )
        # A remote connector carries its own absolute url, and a hosted one may
        # declare where to reach it when Curie is not hosting it (#1160). Both
        # stay mountable here; a hosted connector with neither mounts nothing,
        # which is the honest answer rather than a URL resolving nowhere.
        entries = {}
        for name, spec in sorted(declared.connectors.items()):
            entry = unhosted_mcp_entry(spec)
            if entry is not None:
                entries[name] = entry
        return entries

    return {
        name: mcp_entry(release, agent, namespace, name, spec)
        for name, spec in sorted(declared.connectors.items())
    }
