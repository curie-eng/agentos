"""Inspect the standard MCP tool annotations used by the approval-tool mount.

``request_approval`` can only be useful when the session has an action it may
eventually perform. MCP already carries the relevant capability metadata on
``Tool.annotations.readOnlyHint``; probing that live surface avoids inventing a
second bundle-format declaration that can drift from what a server publishes.
The annotation is only a hint and is not used as an authorization decision:
permission gates and tool execution are unchanged. It controls whether Curie's
non-authoritative, model-invoked generic pager is advertised.

Only an entirely observed, explicitly read-only surface proves that the generic
approval tool should be omitted. A complete surface with zero MCP tools also
proves omission: there is no MCP action for a human to unlock. Built-in Claude
tools are deliberately outside this MCP capability decision; an explicit
approval gate on one is handled separately by the boot path and retains the
pager. An absent hint, an uninspectable declaration, and any probe failure are
all treated as potentially write-capable, preserving the existing tool on
unknown surfaces rather than silently removing a capability.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from plugin_format import PluginManifest, resolve_manifest

logger = logging.getLogger(__name__)

_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_PROBE_TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class McpToolCapabilityProbe:
    """The conservative capability conclusion for one session's MCP surface."""

    complete: bool
    has_potential_write_tool: bool
    tool_count: int
    failures: tuple[str, ...] = ()


def _expand(value: str, env: Mapping[str, str]) -> str:
    """Apply the ``${NAME}`` substitutions Claude applies to MCP config values."""

    return _VARIABLE.sub(lambda match: env.get(match.group(1), match.group(0)), value)


def _bundle_server_configs(plugin_dir: Path) -> list[tuple[str, dict[str, Any]]]:
    """Read both plugin MCP declaration surfaces without collapsing duplicates.

    Every declared server must be inspectable here. In particular, the plugin
    format accepts an ``mcpServers`` path string even though Curie's runtime
    loader does not follow it. Ignoring that or a malformed server entry would
    turn an unknown surface into a false all-read-only conclusion, so these
    shapes raise and the caller conservatively retains ``request_approval``.
    """

    def append_payload(payload: object, source: str) -> None:
        if not isinstance(payload, dict):
            raise ValueError(f"{source} MCP declaration is not an object")
        servers = payload.get("mcpServers", payload)
        if not isinstance(servers, dict):
            raise ValueError(f"{source} mcpServers declaration is not an object")
        for name, config in servers.items():
            if not isinstance(config, dict):
                raise ValueError(f"{source} MCP server {name!r} is not an object")
            configs.append((str(name), dict(config)))

    configs: list[tuple[str, dict[str, Any]]] = []
    manifest_path = resolve_manifest(plugin_dir)
    if manifest_path is not None:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = PluginManifest.model_validate(raw)
        if isinstance(manifest.mcpServers, str):
            raise ValueError(
                "plugin manifest mcpServers path strings cannot be inspected at runtime"
            )
        if manifest.mcpServers is not None:
            append_payload(manifest.mcpServers, "plugin manifest")

    root_config = plugin_dir / ".mcp.json"
    if root_config.is_file():
        raw = json.loads(root_config.read_text(encoding="utf-8"))
        append_payload(raw, ".mcp.json")
    return configs


@asynccontextmanager
async def _server_streams(
    config: Mapping[str, Any],
    *,
    plugin_dir: Path | None,
    inherited_env: Mapping[str, str],
) -> AsyncIterator[tuple[Any, Any]]:
    """Open one stdio, SSE, or streamable-HTTP MCP transport."""

    interpolation_env = dict(inherited_env)
    if plugin_dir is not None:
        interpolation_env["CLAUDE_PLUGIN_ROOT"] = str(plugin_dir)

    command = config.get("command")
    if isinstance(command, str) and command:
        configured_env = config.get("env")
        child_env = dict(interpolation_env)
        if isinstance(configured_env, Mapping):
            child_env.update(
                {
                    str(key): _expand(str(value), interpolation_env)
                    for key, value in configured_env.items()
                }
            )
        args = config.get("args")
        parameters = StdioServerParameters(
            command=_expand(command, interpolation_env),
            args=[_expand(str(arg), interpolation_env) for arg in args]
            if isinstance(args, list)
            else [],
            env=child_env,
            cwd=plugin_dir,
        )
        async with stdio_client(parameters) as streams:
            yield streams
        return

    url = config.get("url")
    if not isinstance(url, str) or not url:
        raise ValueError("MCP server declares neither a command nor a URL")
    expanded_url = _expand(url, interpolation_env)
    raw_headers = config.get("headers")
    headers = (
        {
            str(key): _expand(str(value), interpolation_env)
            for key, value in raw_headers.items()
        }
        if isinstance(raw_headers, Mapping)
        else None
    )
    if config.get("type") == "sse":
        async with sse_client(expanded_url, headers=headers) as streams:
            yield streams
        return

    async with create_mcp_http_client(headers=headers) as http_client:
        async with streamable_http_client(expanded_url, http_client=http_client) as streams:
            read_stream, write_stream, _session_id = streams
            yield read_stream, write_stream


async def _probe_server(
    config: Mapping[str, Any],
    *,
    plugin_dir: Path | None,
    inherited_env: Mapping[str, str],
) -> tuple[int, bool]:
    """Return ``(tool_count, has_potential_write_tool)`` for one MCP server."""

    count = 0
    has_potential_write = False
    with anyio.fail_after(_PROBE_TIMEOUT_SECONDS):
        async with _server_streams(
            config, plugin_dir=plugin_dir, inherited_env=inherited_env
        ) as (read_stream, write_stream):
            async with ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=timedelta(seconds=_PROBE_TIMEOUT_SECONDS),
            ) as session:
                await session.initialize()
                cursor: str | None = None
                while True:
                    result = await session.list_tools(cursor=cursor)
                    count += len(result.tools)
                    if any(
                        tool.annotations is None
                        or tool.annotations.readOnlyHint is not True
                        for tool in result.tools
                    ):
                        has_potential_write = True
                    cursor = result.nextCursor
                    if not cursor:
                        break
    return count, has_potential_write


async def probe_mcp_tool_capability(
    plugin_dir: str | Path | None,
    derived_servers: Mapping[str, Mapping[str, Any]],
    inherited_env: Mapping[str, str] | None = None,
) -> McpToolCapabilityProbe:
    """Probe every bundle/connector MCP server and conservatively classify it.

    ``derived_servers`` is the connector map already produced for the SDK. A
    failed or unreadable source is an unknown surface, which keeps
    ``request_approval`` mounted by returning ``has_potential_write_tool=True``.
    """

    root = Path(plugin_dir) if plugin_dir is not None else None
    env = {**os.environ, **dict(inherited_env or {})}
    failures: list[str] = []
    observations: list[tuple[int, bool]] = []

    try:
        declared = _bundle_server_configs(root) if root is not None else []
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("cannot inspect bundle MCP declarations; keeping approval tool: %s", exc)
        return McpToolCapabilityProbe(
            complete=False,
            has_potential_write_tool=True,
            tool_count=0,
            failures=("bundle-config",),
        )

    work: list[tuple[str, Mapping[str, Any], Path | None]] = [
        (name, config, root) for name, config in declared
    ]
    for name, config in derived_servers.items():
        if not isinstance(config, Mapping):
            failures.append(str(name))
            logger.warning(
                "cannot inspect derived MCP declaration server=%s; keeping approval tool",
                name,
            )
            continue
        work.append((str(name), config, None))

    async def inspect(name: str, config: Mapping[str, Any], cwd: Path | None) -> None:
        try:
            observations.append(
                await _probe_server(config, plugin_dir=cwd, inherited_env=env)
            )
        except Exception as exc:
            failures.append(name)
            logger.warning(
                "MCP tool-capability probe failed server=%s; keeping approval tool: %s",
                name,
                exc,
            )

    async with anyio.create_task_group() as task_group:
        for name, config, cwd in work:
            task_group.start_soon(inspect, name, config, cwd)

    tool_count = sum(count for count, _write in observations)
    has_write = bool(failures) or any(write for _count, write in observations)
    return McpToolCapabilityProbe(
        complete=not failures,
        has_potential_write_tool=has_write,
        tool_count=tool_count,
        failures=tuple(sorted(set(failures))),
    )
