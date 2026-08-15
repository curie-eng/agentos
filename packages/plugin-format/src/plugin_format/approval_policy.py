"""Normalize the manifest's grantable approval-policy gates (#558).

The operator opt-in ``grantableViaPolicy`` marks a gate whose policy-gate
approval MAY mint a one-shot grant for the tool the gate names (its ``gate``
field, MANIFEST-supplied, never model-supplied). ``grantable_routes`` is the
SINGLE normalization shared by the deploy-time validator
(``plugin_format.validate``) and the runtime loader
(``curie_runner.approval.resolve_approval_policy``). Sharing one helper makes
the two paths identical *by construction* -- the #453/#544 lesson that a
validator and a runtime loader normalizing separately can silently disagree and
ship a fail-open.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import yaml
from pydantic import ValidationError

from .connectors import CONNECTORS_FILE, validate_connectors
from .manifest import resolve_manifest
from .models import ApprovalGate, McpConfig, PluginManifest


def grantable_routes(
    gates: list[ApprovalGate],
) -> tuple[dict[str, str], set[str]]:
    """Resolve the grantable ``{route: tool}`` map and the ambiguous routes.

    For each gate with ``grantableViaPolicy`` truthy AND a non-empty stripped
    ``gate`` AND a non-empty stripped ``route``, accumulate ``route`` -> the set
    of tools claiming it (``tool = gate.gate.strip()``, ``route =
    gate.route.strip()``). ``.strip()`` and case-SENSITIVE comparison mirror
    ``load_approval_policy`` so a config that validates green at deploy resolves
    identically at runtime (#453).

    Returns ``(resolved, ambiguous)``:

    - ``resolved`` maps each route whose tool-set holds exactly ONE distinct tool
      to that tool. A route named twice by the SAME tool is a duplicate, not a
      conflict: still one distinct tool, still resolved.
    - ``ambiguous`` is the set of routes claimed by MORE than one distinct tool.
      Such a route is excluded from ``resolved`` (arms no grant) and reported so
      the deploy validator can reject it, rather than validating green while
      arming nothing (the #453 shape).

    Non-grantable gates and gates with a blank ``gate`` or ``route`` are ignored.
    """

    tools_by_route: dict[str, set[str]] = {}
    for gate in gates:
        if not gate.grantableViaPolicy:
            continue
        tool = gate.gate.strip()
        route = gate.route.strip()
        if not tool or not route:
            continue
        tools_by_route.setdefault(route, set()).add(tool)

    resolved: dict[str, str] = {}
    ambiguous: set[str] = set()
    for route, tools in tools_by_route.items():
        if len(tools) == 1:
            resolved[route] = next(iter(tools))
        else:
            ambiguous.add(route)
    return resolved, ambiguous


# --- Operator gate-name normalization (#703): shared by the deploy validator ----
# and the runtime loader, so an operator-supplied gate name and a manifest gate
# name resolve to the SAME effective runtime form by construction (the #453/#544
# validator/runtime-drift lesson).


def effective_tool_prefix(bundle_name: str, server: str) -> str:
    """The live tool-name prefix the SDK plugin-namespacing produces (#703).

    The SAME template ``validate.py`` builds ``expected_prefixes`` from and
    ``effective_operator_gate`` matches against -- ONE definition shared by the
    deploy-time validator and the runtime loader so the prefix format cannot
    drift between them (the #453/#544 lesson).
    """

    return f"mcp__plugin_{bundle_name}_{server}__"


def connector_tool_prefix(server: str) -> str:
    """The live tool-name prefix a ``connectors.yaml`` server produces (#1495).

    A connector is NOT a plugin-loaded MCP server. The runner mounts it directly
    on ``ClaudeAgentOptions.mcp_servers`` (``curie_runner.connectors``,
    ADR-0086), on the same channel as Curie's own platform servers ``curie`` and
    ``curie-state``, so the SDK names its tools ``mcp__<server>__<tool>`` with NO
    ``plugin_<bundle>_`` infix -- exactly like
    ``approval.APPROVAL_TOOL_NAME``. Giving connectors the plugin prefix would
    arm a literal the runtime never produces: a gate that validates green and
    silently never fires, the #453 fail-open shape.

    One definition, shared by the deploy validator and the runtime loader, for
    the same anti-drift reason as ``effective_tool_prefix``.
    """

    return f"mcp__{server}__"


def _longest_matching_server(
    name: str, servers: set[str], prefix_of: Callable[[str], str]
) -> str | None:
    """The declared server whose ``prefix_of(server)`` matches ``name``, or ``None``.

    A server matches when ``name`` starts with ``prefix_of(server)`` AND has a
    non-empty remainder after it (``len(name) > len(prefix)``). When more than
    one declared server matches (one server name is a prefix of another), the
    LONGEST server name wins the tie -- the more specific match.
    """

    best_server: str | None = None
    for s in servers:
        prefix = prefix_of(s)
        if name.startswith(prefix) and len(name) > len(prefix):
            if best_server is None or len(s) > len(best_server):
                best_server = s
    return best_server


def _mcp_server_names(obj: object) -> set[str] | None:
    """The set of server names an mcp declaration object names, or ``None``.

    Accepts both a full config object (``{"mcpServers": {...}}``) and a bare
    servers map (``{name: server}``), matching ``validate._validate_mcp_object``'s
    payload wrapping so the name derivation is identical on both sides. ``None``
    means the declaration failed to validate (unreadable), which poisons the
    declared-server union.
    """

    payload = obj if isinstance(obj, dict) and "mcpServers" in obj else {"mcpServers": obj}
    try:
        config = McpConfig.model_validate(payload)
    except ValidationError:
        return None
    return set(config.mcpServers)


def declared_mcp_server_names(root: str | Path) -> set[str] | None:
    """The MCP server names a bundle declares, or ``None`` when unknowable.

    Reads the manifest's inline ``mcpServers`` object AND the root ``.mcp.json``,
    returning the union of every server name across both. ``None`` is the poison
    value ``validate._validate_mcp`` uses: a declaration existed but could not be
    read (invalid JSON, a config that failed to validate, or the path-string form
    the real loader ignores), so the declared-server set is unknowable and a gate
    cross-check must fail closed rather than assert against a partial set. An empty
    set is the distinct fact that a declaration was read and named no servers.
    """

    root = Path(root)
    servers: set[str] = set()
    unreadable = False

    manifest_path = resolve_manifest(root)
    if manifest_path is not None:
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest = PluginManifest.model_validate(data)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError, ValidationError):
            # The manifest itself is unreadable, so its declared servers are
            # unknowable -- poison the whole set.
            return None
        declared = manifest.mcpServers
        if isinstance(declared, dict):
            result = _mcp_server_names(declared)
            if result is None:
                unreadable = True
            else:
                servers |= result
        elif isinstance(declared, str):
            # The path-string form parses but the real loader ignores it, so the
            # servers never register: unknowable, not empty.
            unreadable = True

    root_mcp = root / ".mcp.json"
    if root_mcp.is_file():
        try:
            data = json.loads(root_mcp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            unreadable = True
        else:
            result = _mcp_server_names(data)
            if result is None:
                unreadable = True
            else:
                servers |= result

    if unreadable:
        return None
    return servers


def connector_server_names(root: str | Path) -> set[str] | None:
    """The MCP server names the bundle's ``connectors.yaml`` supplies, or ``None`` (#1495).

    The THIRD source of a bundle's tool surface, alongside the manifest's inline
    ``mcpServers`` and the root ``.mcp.json`` that ``declared_mcp_server_names``
    reads. It is a separate function, not another branch of that one, because a
    connector's live tool name is namespaced DIFFERENTLY
    (``connector_tool_prefix``, no ``plugin_<bundle>_`` infix): folding the two
    name sets together would build the wrong prefix for one of them.

    Parsing goes through ``connectors.validate_connectors`` -- the same parser the
    deploy validator and the runner's connector mount use -- so the names a gate
    may be namespaced to are exactly the names Curie will mount.

    ``None`` is the same poison value ``declared_mcp_server_names`` returns: a
    ``connectors.yaml`` exists but could not be read or did not validate, so the
    connector-server set is unknowable and a gate cross-check must fail closed
    rather than assert against a partial set. ``_validate_connectors`` already
    errors on that file, so the gate check stays silent rather than stacking a
    second, misleading error. An empty set is the distinct fact that the bundle
    declares no connectors.
    """

    path = Path(root) / CONNECTORS_FILE
    if not path.is_file():
        return set()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    parsed, errors = validate_connectors(data)
    if parsed is None or errors:
        # A rejected connectors.yaml is never mounted, so its names are not a
        # tool surface a gate may be namespaced to.
        return None
    return set(parsed.connectors)


def effective_operator_gate(
    bundle_name: str | None,
    servers: set[str] | None,
    name: str,
    *,
    connector_servers: set[str] | None,
) -> str | None:
    """Map an operator gate name to its effective runtime tool name (#703).

    The SDK plugin-prefixes a bundle MCP tool to
    ``mcp__plugin_<bundle>_<server>__<tool>``. An operator who writes the natural
    shorthand ``mcp__<server>__<tool>`` in ``CURIE_APPROVAL_REQUIRED_TOOLS`` must
    have it rewritten to that effective form, or the gate arms a literal that the
    runtime name never matches (a silent fail-open). Returns:

    - the rewritten effective name when ``name`` is ``mcp__<server>__<tool>`` and
      ``<server>`` is a declared bundle server. ``<server>`` is resolved by
      MATCHING the shorthand against the declared servers (a server name may itself
      contain ``__``, so splitting at the first ``__`` would misparse
      ``mcp__foo__bar__do`` as server ``foo``); the longest matching server name
      wins when one is a prefix of another. The effective prefix is built exactly
      as ``validate.py`` constructs ``expected_prefixes``;
    - ``name`` verbatim for an ``mcp__<connector>__<tool>`` naming a connector the
      bundle declares in ``connectors.yaml`` (#1495). A connector rides the SDK's
      ``mcp_servers`` map directly, so that shorthand IS already the live runtime
      name: it is verified against the declared connectors and returned unchanged,
      never rewritten to the plugin form the runtime never produces. Both the
      connector rule and the plugin-shorthand rule test the SAME ``mcp__<server>__``
      prefix shape, so ONE name can match both sets; the two candidates compete on
      SPECIFICITY (the longest matched server name wins) rather than on which check
      runs first (#1564);
    - ``name`` verbatim only for a built-in with no ``mcp__`` prefix (armed by raw
      name), OR for an already ``mcp__plugin_``-prefixed name that MATCHES an
      expected prefix ``mcp__plugin_<bundle>_<server>__`` for a declared server
      (with a non-empty tool remainder), mirroring ``validate.py``'s
      ``expected_prefixes`` check -- an already-prefixed name is NOT trusted blindly
      (a typo'd ``mcp__plugin_wrongbundle_wrongserver__tool`` would arm a literal the
      runtime never matches, a fail-open);
    - ``None`` when ``name`` is ``mcp__``-shaped and cannot be verified against a
      declared server or connector: an undeclared server, no declared servers,
      ``servers`` or ``connector_servers`` being ``None`` (unknowable), a falsy
      ``bundle_name`` (cannot construct the plugin prefix to verify), an empty tool
      remainder, an already-prefixed name matching no expected
      prefix, or an AMBIGUOUS name matching a connector and a declared MCP server at
      EQUAL specificity (#1564) -- the two live forms differ and there is no
      principled winner, so refuse rather than pick one silently. The operator
      override is never deploy-validated, so this runtime
      check is its sole defense -- "cannot verify" fails CLOSED, not through. The
      caller fails closed on ``None``.
    """

    # A built-in tool (Bash, Write, ...) carries no mcp__ prefix: armed by raw
    # name, never rewritten and never server-checked.
    if not name.startswith("mcp__"):
        return name
    # Every mcp__-shaped name is verified against the declared-server sets. Without
    # them (an unreadable MCP or connectors declaration) nothing can be verified, so
    # fail closed -- this runtime check is the operator override's only defense.
    if servers is None or connector_servers is None:
        return None
    # A connectors.yaml server is mounted straight onto the SDK's mcp_servers map
    # (ADR-0086), so mcp__<connector>__<tool> is ALREADY the live runtime name; a
    # plugin server's mcp__<server>__<tool> shorthand must be REWRITTEN to
    # mcp__plugin_<bundle>_<server>__<tool>. Both rules test the SAME
    # mcp__<server>__ prefix shape (connector_tool_prefix vs the shorthand template
    # below), so ONE name can match both sets -- with connector `git` and MCP server
    # `git__hub` both declared, mcp__git__hub__create_pr matches each. Compute BOTH
    # candidates and arbitrate on SPECIFICITY: the longest matched server name wins,
    # exactly as _longest_matching_server arbitrates WITHIN one set. Deciding by
    # which check runs first would let the connector shadow the plugin server and
    # arm the un-rewritten literal, which the runtime never produces -- the #453
    # fail-open, in reverse (#1564).
    connector = _longest_matching_server(name, connector_servers, connector_tool_prefix)
    shorthand = _longest_matching_server(name, servers, lambda s: f"mcp__{s}__")
    # Equal specificity means the connector and the MCP server carry the SAME name
    # (equal-length prefixes of one name under one template are the same string),
    # and their live forms differ. There is no principled winner, so refuse rather
    # than pick one silently: the caller turns None into a loud boot error (#520).
    # validate._reject_connector_name_collisions already rejects this at deploy, but
    # the operator env knob is never deploy-validated, which is what this defends.
    if connector is not None and shorthand is not None and len(connector) == len(shorthand):
        return None
    # The connector won on specificity (or is the only match): return the name
    # unchanged (#1495). Resolved BEFORE the already-prefixed literal branch below,
    # because a connector may legally be named `plugin` and its mcp__plugin__<tool>
    # would otherwise fall into that branch and be rejected. The RFC 1123 argument
    # ("a connector name never contains `_`") is why the literal branch cannot steal
    # a connector name -- it says nothing about the shorthand branch, which shares
    # the connector's exact prefix shape and needs the contest above.
    if connector is not None and (shorthand is None or len(connector) > len(shorthand)):
        return name
    # The plugin form needs the bundle name to construct the prefix to verify
    # against; without it nothing can be verified, so fail closed.
    if not bundle_name:
        return None
    # Already the effective plugin-namespaced form: verify it matches an expected
    # prefix mcp__plugin_<bundle>_<server>__ for a declared server (with a non-empty
    # tool remainder), exactly as validate.py asserts. Do NOT trust it verbatim.
    if name.startswith("mcp__plugin_"):
        matched = _longest_matching_server(
            name, servers, lambda s: effective_tool_prefix(bundle_name, s)
        )
        return name if matched is not None else None
    # An mcp__<server>__<tool> shorthand: <server> was resolved above by matching
    # against the declared servers rather than splitting at the first __ (a server
    # name may contain __), preferring the longest match; require a non-empty tool
    # remainder.
    if shorthand is None:
        return None
    tool = name[len(f"mcp__{shorthand}__") :]
    return f"mcp__plugin_{bundle_name}_{shorthand}__{tool}"
